"""What a podcast client talks to.

    GET  /                     the feeds, as subscribable URLs (text/plain)
    GET  /feed/<name>.xml      the feed, generated from the spool on each read
    GET  /ep/<name>/<file>     one episode, with byte ranges

Deliberately its own server rather than more routes on the canvas
(`packages/visual/canvas.py`), whose shape this otherwise copies. The two
have different audiences and therefore different doors: the canvas is a page
*we* wrote, authenticated by a token in the browser's localStorage, and this
is read by somebody else's podcast client, which will never send a header we
choose. Sharing a process would mean one auth path bent to fit both, and the
bent one would be this one — the one guarding recordings of private
conversations.

## The token has to be in the URL

A podcast client subscribes to a URL and re-fetches it forever; there is no
place in that flow to put a credential. So a capability URL:

    http://red5:8782/feed/talks.xml?k=<token>

and the token is carried into every enclosure the feed lists. Any other
arrangement produces a feed that loads while every episode 401s, which is the
classic way this breaks — the client shows the episode list and fails only on
download, which reads as "the server is broken", not "the auth is wrong".

`X-Agent-Media-Token` and HTTP Basic (password field) are accepted too, for
`curl` and for clients that support Basic. **The enclosures only carry `?k=`
when the request that asked for the feed did**: a client that got in with a
header will send that header again, and baking the secret into URLs it did
not need is how a token ends up in somebody's sync log.

## Fail closed off loopback

Binding anything but loopback without a token is a startup failure, not a
warning — the precedent is `share_listener`, but the reason is the other way
round. That endpoint refuses because it *starts playback*; this one refuses
because it *hands out recordings*. A listener that came up anyway would be an
open archive of private speech that reported itself healthy.

Even with a token: tailnet only. Never the public interface.

## Generated per request, not served from disk

`media feed write` leaves a `feed.xml` in the spool, and this does not read
it. Generating from the sidecars costs a directory scan and removes the entire
class of bug where the XML and the episodes disagree — a client that syncs
between a publish and a rewrite would otherwise cache a listing that is
missing the episode it came for, and podcast clients are slow to re-check.

## The host is whatever the client used

Enclosure URLs are absolute and get baked into a subscriber's database on
first sync, so the wrong host is not something you correct — it is something
you re-subscribe out of. Rather than make that a setting nobody can get right
in advance, the base URL is taken from the request's own `Host` header:
whatever address reached the feed is an address that reaches the episodes.
`MEDIA_FEED_BASE_URL` overrides, for a reverse proxy that terminates TLS.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import logging
import os
import re
import socketserver
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import feed as feedmod

log = logging.getLogger("media-feed")

DEFAULT_PORT = 8782
LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}

#: An episode filename as `publish` writes it: 16 hex from the guid hash, and
#: an audio extension. Anything else is not ours, and a name that is not ours
#: is not a name we go looking for on disk.
_EP_FILE = re.compile(r"^[0-9a-f]{16}\.[a-z0-9]{1,5}$")

TOKEN = ""
CHUNK = 64 * 1024


def _loopback(bind: str) -> bool:
    return bind in LOOPBACK


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "agent-media-feed/1"
    protocol_version = "HTTP/1.1"      # ranges and keep-alive both want this

    def log_message(self, fmt, *args) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # --- auth --------------------------------------------------------------

    def _offered(self) -> tuple[str, bool]:
        """The token this request carried, and whether it came in the URL."""
        q = parse_qs(urlsplit(self.path).query)
        if q.get("k"):
            return q["k"][0], True
        hdr = (self.headers.get("X-Agent-Media-Token") or "").strip()
        if hdr:
            return hdr, False
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("basic "):
            try:
                raw = base64.b64decode(auth.split(None, 1)[1]).decode("utf-8", "replace")
                return raw.split(":", 1)[-1], False
            except (ValueError, IndexError):
                return "", False
        return "", False

    def _authorised(self) -> bool:
        if not TOKEN:
            return True
        offered, _ = self._offered()
        # compare_digest, not ==: the comparison is against a secret and the
        # loop is over a network-controlled string.
        return bool(offered) and hmac.compare_digest(offered, TOKEN)

    def _url_token(self) -> str:
        """The token to bake into enclosure URLs — see the module docstring."""
        if not TOKEN:
            return ""
        offered, in_url = self._offered()
        return TOKEN if (in_url and offered) else ""

    def _base_url(self) -> str:
        override = (os.environ.get("MEDIA_FEED_BASE_URL", "") or "").strip()
        if override:
            return override.rstrip("/")
        host = (self.headers.get("Host") or "").strip()
        return f"http://{host}" if host else "http://localhost"

    # --- plumbing ----------------------------------------------------------

    def _head(self, code: int, ctype: str, length: int, extra=()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()

    def _fail(self, code: int, msg: str) -> None:
        body = (msg + "\n").encode()
        # WWW-Authenticate makes `curl -u` and the Basic-capable clients offer
        # a prompt instead of showing a bare 401 body.
        extra = [("WWW-Authenticate", 'Basic realm="agent-media"')] if code == 401 else []
        self._head(code, "text/plain; charset=utf-8", len(body), extra)
        self._write(body)

    def _write(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # A client that stops a download mid-episode is normal traffic,
            # not an error worth a stack trace in the service log.
            pass

    # --- routes ------------------------------------------------------------

    def do_HEAD(self) -> None:      # noqa: N802
        self._route(body=False)

    def do_GET(self) -> None:       # noqa: N802
        self._route(body=True)

    def _route(self, *, body: bool) -> None:
        if not self._authorised():
            self._fail(401, "unauthorised")
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.txt"):
            self._index(body)
            return
        m = re.fullmatch(r"/feed/([a-z0-9_-]+)\.xml", path)
        if m:
            self._feed(m.group(1), body)
            return
        m = re.fullmatch(r"/ep/([a-z0-9_-]+)/([^/]+)", path)
        if m:
            self._episode(m.group(1), m.group(2), body)
            return
        self._fail(404, "no such thing here")

    def _index(self, body: bool) -> None:
        """Every feed as a URL you can paste into a client."""
        base, tok = self._base_url(), self._url_token()
        lines = []
        for name in feedmod.feeds():
            n = len(feedmod.episodes(name))
            url = f"{base}/feed/{name}.xml" + (f"?k={tok}" if tok else "")
            lines.append(f"{url}\t{n} episode{'' if n == 1 else 's'}")
        out = ("\n".join(lines) + "\n").encode() if lines else b"no feeds\n"
        self._head(200, "text/plain; charset=utf-8", len(out))
        if body:
            self._write(out)

    def _feed(self, name: str, body: bool) -> None:
        if not feedmod.valid_name(name) or not feedmod.feed_dir(name).is_dir():
            self._fail(404, "no such feed")
            return
        xml = feedmod.feed_xml(name, feedmod.episodes(name),
                               base_url=self._base_url(),
                               token=self._url_token()).encode("utf-8")
        # Clients poll the feed far more often than it changes; an ETag turns
        # most of that into a 304 and costs one hash of a small document.
        etag = '"%s"' % hashlib.sha256(xml).hexdigest()[:16]
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._head(200, "application/rss+xml; charset=utf-8", len(xml),
                   [("ETag", etag), ("Cache-Control", "no-cache")])
        if body:
            self._write(xml)

    def _episode(self, name: str, filename: str, body: bool) -> None:
        if not feedmod.valid_name(name) or not _EP_FILE.fullmatch(filename):
            self._fail(404, "no such episode")
            return
        path = feedmod.feed_dir(name) / filename
        # Belt and braces over the regex: the file must really be a child of
        # the feed directory, not a link out of it.
        try:
            if not path.is_file() or path.resolve().parent != \
                    feedmod.feed_dir(name).resolve():
                raise OSError
            size = path.stat().st_size
        except OSError:
            self._fail(404, "no such episode")
            return

        ctype = feedmod.mime_for(filename)
        start, end = self._range(size)
        if start is None and end == -1:                     # unsatisfiable
            self._head(416, ctype, 0, [("Content-Range", f"bytes */{size}")])
            return
        if start is None:                                   # whole file
            self._head(200, ctype, size)
            if body:
                self._send_range(path, 0, size - 1)
            return
        self._head(206, ctype, end - start + 1,
                   [("Content-Range", f"bytes {start}-{end}/{size}")])
        if body:
            self._send_range(path, start, end)

    def _range(self, size: int):
        """Parse one byte range. Returns (start, end) inclusive, (None, None)
        for no range, (None, -1) for an unsatisfiable one.

        Ranges are not optional for audio: a client resuming an interrupted
        download, or seeking within an episode it has not finished fetching,
        asks for one — and a server that answers 200 with the whole file makes
        both of those start again from zero.

        Multiple ranges are answered as the whole file. No podcast client asks
        for them, and multipart/byteranges is a lot of surface to carry for a
        caller that does not exist.
        """
        raw = (self.headers.get("Range") or "").strip()
        if not raw.startswith("bytes=") or "," in raw:
            return None, None
        spec = raw[len("bytes="):].strip()
        try:
            if spec.startswith("-"):                        # last N bytes
                n = int(spec[1:])
                if n <= 0:
                    return None, -1
                return max(0, size - n), size - 1
            first, _, last = spec.partition("-")
            start = int(first)
            end = int(last) if last else size - 1
        except ValueError:
            return None, None
        if start >= size or start < 0 or end < start:
            return None, -1
        return start, min(end, size - 1)

    def _send_range(self, path: Path, start: int, end: int) -> None:
        remaining = end - start + 1
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            log.warning("media-feed: read failed on %s (%s)", path, e)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(bind: str, port: int) -> _Server:
    """A bound server, not yet serving. Tests drive it; `main` runs it."""
    return _Server((bind, port), Handler)


def main(argv=None) -> int:
    from ..intake._env import load_env_file
    load_env_file("media-feed")

    ap = argparse.ArgumentParser(prog="media-feed",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("MEDIA_FEED_PORT") or DEFAULT_PORT))
    ap.add_argument("--bind", default=os.environ.get("MEDIA_FEED_BIND", "127.0.0.1"),
                    help="address to listen on (default 127.0.0.1). Anything "
                         "else needs MEDIA_FEED_TOKEN.")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    global TOKEN
    TOKEN = (os.environ.get("MEDIA_FEED_TOKEN") or "").strip()
    if not _loopback(a.bind) and not TOKEN:
        log.error("media-feed: refusing to bind %s with no MEDIA_FEED_TOKEN — "
                  "this endpoint hands out recordings of private "
                  "conversations. Set it in ~/.config/agent-media.env, or "
                  "bind 127.0.0.1.", a.bind)
        return 2

    with serve(a.bind, a.port) as srv:
        log.info("media-feed: %s:%d — %s%s", a.bind, a.port,
                 ", ".join(feedmod.feeds()) or "no feeds yet",
                 " (token required)" if TOKEN else "")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
