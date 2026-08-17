"""The doorway a share sheet knocks on: loopback HTTP, one endpoint.

The companion app cannot run `media`. mpv's IPC sockets, the yt-dlp cache and
the CLI itself all live inside `com.termux`'s private UID sandbox, and no other
app on the phone can reach into it — that constraint is why the mpv bridges
exist, and it applies here for the same reason. So the app carries the shared
text across the sandbox boundary over loopback TCP, and everything downstream
of that happens here, in Python, in the repo, under test.

    POST /share   body: a URL, shared text, or {"text": "..."}
                  -> {"ok":true,"channel":"book","content_type":"podcast",...}
    GET  /recent  ?limit=&channel= -> what played, newest first, for the app's
                  in-app list: uri, channel, content_type, label, ago.
    POST /play    body: {"uri", "channel", "content_type"} — replay a row from
                  that list. It does NOT classify: the row already knows.
    GET  /channels  one snapshot of speech/music/book — what the app's control
                  screen renders, normalised in `share_control`.
    GET  /chapters  the loaded track's chapters, 1-based. `?channel=` picks
                  music (the default) or book; anything else has none.
    POST /control   {"channel", "action", "arg"} — one whitelisted transport
                  verb. Not a passthrough: this endpoint presses buttons, it
                  does not run the CLI.
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
import time
from typing import Optional

from .. import share as sharemod
from . import share_control as control

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
        path, _, query = self.path.partition("?")
        if path == "/recent":
            self._send(200, {"ok": True, "rows": recent_rows(query)})
            return
        if path == "/channels":
            self._send(200, {"ok": True, "channels": control.channels()})
            return
        if path == "/chapters":
            from urllib.parse import parse_qs

            channel = (parse_qs(query or "").get("channel") or ["music"])[0]
            self._send(200, {"ok": True, "rows": control.chapters(channel)})
            return
        if path not in ("/", "/health"):
            self._send(404, {"ok": False, "error": "no such path"})
            return
        self._send(200, {"ok": True, "service": "media-share"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/play":
            self._replay()
            return
        if path == "/control":
            self._control()
            return
        if path != "/share":
            self._send(404, {"ok": False, "error": "no such path"})
            return
        raw = self._read_body()
        if raw is None:
            return
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

    def _read_body(self) -> Optional[str]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(400, {"ok": False, "error": "empty or oversized body"})
            return None
        return self.rfile.read(length).decode("utf-8", "replace")

    def _control(self) -> None:
        """One transport verb, from the whitelist. Synchronous on purpose.

        Unlike a share, a control is instant and its result is the only
        feedback there is: a surface that has just drawn a pause button needs
        to know whether it took, and it will poll `/channels` next anyway. So
        this one does not hand off to a thread.
        """
        raw = self._read_body()
        if raw is None:
            return
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        try:
            rc = control.control(str(obj.get("channel") or ""),
                                 str(obj.get("action") or ""),
                                 str(obj.get("arg") or ""))
        except control.ControlError as e:
            self._send(422, {"ok": False, "error": str(e)})
            return
        except Exception as e:  # noqa: BLE001 — a button press must not 500 bare
            log.warning("control failed: %s", e)
            self._send(500, {"ok": False, "error": str(e)})
            return
        self._send(200, {"ok": rc == 0, "rc": rc})

    def _replay(self) -> None:
        """Play something the caller already knows the channel for.

        `/share` classifies; this does not. A row from `/recent` carries the
        channel and content type it played under last time, and re-deriving
        them would be worse than pointless — it would mean a yt-dlp round trip
        to maybe reach a different answer than the one the listener is looking
        at. Replay is not a share, it is a repeat.
        """
        raw = self._read_body()
        if raw is None:
            return
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = {}
        uri = str(obj.get("uri") or "").strip() if isinstance(obj, dict) else ""
        if not uri:
            self._send(422, {"ok": False, "error": "nothing to play"})
            return
        channel = str(obj.get("channel") or "music")
        if channel not in ("music", "book"):
            self._send(422, {"ok": False, "error": f"no such channel: {channel}"})
            return
        verdict = sharemod.Verdict(
            channel=channel,
            content_type=str(obj.get("content_type") or "music"),
            reason="replayed from history",
            title=str(obj.get("title") or ""))
        log.info("replay: %s", verdict.line())
        self._send(200, {"ok": True, "uri": uri, "channel": verdict.channel,
                         "content_type": verdict.content_type,
                         "title": verdict.title, "line": verdict.line()})
        threading.Thread(target=_play,
                         args=(uri, verdict, str(obj.get("where") or "")),
                         daemon=True, name="replay-dispatch").start()


def recent_rows(query: str = "") -> list:
    """Rows for the app's list: what played, newest first.

    Presentation is shared with `media recent` rather than reimplemented —
    same label, same "3h" — because two renderings of one history drift, and
    the phone is the harder one to check.
    """
    from urllib.parse import parse_qs

    from ..cli import _ago, _recent_label
    from ..state import StateStore

    args = parse_qs(query or "")
    channel = (args.get("channel") or [""])[0]
    if channel not in ("music", "book", "speech", ""):
        channel = ""
    try:
        limit = max(1, min(100, int((args.get("limit") or ["20"])[0])))
    except ValueError:
        limit = 20

    now = time.time()
    rows = []
    for r in StateStore().recent_history(sink=channel or None, limit=limit):
        title = (r.get("text") or "").strip()
        rows.append({
            "uri": r.get("uri") or "",
            "channel": r.get("sink") or "",
            "content_type": r.get("content_type") or "",
            "label": title.splitlines()[0] if title
                     else _recent_label(r.get("uri") or ""),
            "ago": _ago(now - float(r.get("started_at") or now)),
            # The instant as well as the distance. `ago` is what a terminal
            # wants ("18m ago"); a list on a phone wants to group by day and
            # show a clock time, and "18m" cannot be turned back into either.
            # Zero when the store has no time for the row, which the reader
            # must treat as "unknown", not as 1970.
            "started_at": float(r.get("started_at") or 0.0),
        })
    return rows


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
