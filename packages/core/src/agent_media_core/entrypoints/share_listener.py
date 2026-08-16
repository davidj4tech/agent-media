"""The doorway a share sheet knocks on: loopback HTTP, one endpoint.

The companion app cannot run `media`. mpv's IPC sockets, the yt-dlp cache and
the CLI itself all live inside `com.termux`'s private UID sandbox, and no other
app on the phone can reach into it — that constraint is why the mpv bridges
exist, and it applies here for the same reason. So the app carries the shared
text across the sandbox boundary over loopback TCP, and everything downstream
of that happens here, in Python, in the repo, under test.

    POST /share   body: a URL, shared text, or {"text": "..."}
                  -> {"ok":true,"channel":"book","content_type":"podcast",...}
    GET  /        -> the same JSON shape, minus a verdict: a health probe.

**It answers before it plays.** Classification takes a `yt-dlp -J` round trip
(seconds); acquisition takes a full download (minutes, for a DJ set on a
mobile connection). The sharer should not watch a spinner for either, so the
verdict returns as soon as it is known and the dispatch runs on a background
thread. The toast says what will happen; the phone then goes and does it.

Bound to 127.0.0.1 and nothing else, with no auth — the same boundary
`mpv-music-bridge-local` and the app's own `StatusServer` sit behind, on a
single-user phone. Do not widen it: unlike those two, this endpoint *starts
playback*, so anything that can reach it can drive the speakers.
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import socketserver
import sys
import threading

from .. import share as sharemod

log = logging.getLogger(__name__)

# Seconds for the metadata probe. Deliberately short: the verdict is blocking
# the sharer's toast, and a probe that slow has already failed in practice.
PROBE_TIMEOUT_S = 20.0

MAX_BODY = 8192  # a shared link plus its prose; anything larger is not a share


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "agent-media-share/1"

    # The default logger writes every request to stderr, which under runit is
    # the service log. A share is worth a line; the request line is not.
    def log_message(self, fmt, *args) -> None:  # noqa: A003
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = (json.dumps(payload) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # the activity gave up; the dispatch still runs

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] not in ("/", "/health"):
            self._send(404, {"ok": False, "error": "no such path"})
            return
        self._send(200, {"ok": True, "service": "media-share"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/share":
            self._send(404, {"ok": False, "error": "no such path"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(400, {"ok": False, "error": "empty or oversized body"})
            return
        raw = self.rfile.read(length).decode("utf-8", "replace")
        text, channel, where = _parse_body(raw)
        try:
            url, verdict = sharemod.share(text, channel=channel,
                                          probe_timeout=PROBE_TIMEOUT_S)
        except sharemod.ShareError as e:
            log.info("share rejected: %s", e)
            self._send(422, {"ok": False, "error": str(e)})
            return
        except Exception as e:  # noqa: BLE001 — a share must never 500 silently
            log.warning("share failed: %s", e)
            self._send(500, {"ok": False, "error": str(e)})
            return
        log.info("share: %s", verdict.line())
        self._send(200, {"ok": True, "url": url, "channel": verdict.channel,
                         "content_type": verdict.content_type,
                         "title": verdict.title, "reason": verdict.reason,
                         "line": verdict.line()})
        threading.Thread(target=_play, args=(url, verdict, where),
                         daemon=True, name="share-dispatch").start()


def _parse_body(raw: str) -> tuple[str, str, str]:
    """(text, channel override, where override) from the request body.

    JSON when it parses as an object, otherwise the body IS the shared text —
    so `curl -d 'https://…' 127.0.0.1:8771/share` works from a phone shell
    without quoting a JSON document on a soft keyboard.
    """
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError:
            return raw, "", ""
        if isinstance(obj, dict):
            return (str(obj.get("text") or obj.get("url") or ""),
                    str(obj.get("channel") or ""),
                    str(obj.get("where") or ""))
    return raw, "", ""


def _play(url: str, verdict: sharemod.Verdict, where: str) -> None:
    try:
        rc = sharemod.dispatch(url, verdict, where=where)
        if rc:
            log.warning("share dispatch exited %d for %s", rc, url)
    except Exception as e:  # noqa: BLE001 — a dead thread must still say why
        log.warning("share dispatch failed for %s: %s", url, e)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None) -> int:
    from ..intake._env import load_env_file
    load_env_file("media-share")
    ap = argparse.ArgumentParser(prog="media-share", description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=sharemod.DEFAULT_PORT)
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with _Server(("127.0.0.1", a.port), Handler) as srv:
        log.info("media-share: 127.0.0.1:%d/share", a.port)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
