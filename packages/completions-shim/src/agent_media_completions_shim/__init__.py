"""OpenAI /v1/chat/completions over a live Claude Code session.

Makes a running agent-media Claude session answer as an OpenAI chat model, so
Open WebUI — Call mode included — can converse WITH the agent: inject the user
turn, wait for the reply to land in the session transcript, stream it back.

This is the synchronous counterpart to intake-owui's fire-and-forget Pipe. Use
THIS as OWUI's "model" (an OpenAI connection → this server); use the tts-shim
as OWUI's voice. It reuses the canvas HTTP surface end to end — /input to
inject, /agents for session state, /peek to read the reply — so there's no
second capture path to keep in sync.

VOICE COLLISION (read this): the target session already speaks via agent-media
(room-routed). In Call mode OWUI + the tts-shim voice the reply on the device —
so without care you get DOUBLE speech. The fix: MUTE the target session (a muted
pane still renders its canvas figure and records history, it just doesn't speak
aloud). MEDIA_COMPLETIONS_MUTE=1 (default) runs `media mute-pane --pane <p> on`
for you. Dedicate a session to OWUI, and keep the tts-shim's MEDIA_SHIM_CANVAS
OFF for this model so the session's own hook owns the canvas (no double figure).

Config (env):
  MEDIA_COMPLETIONS_PORT     listen port (default 8783 — shim 8782, canvas 8781)
  MEDIA_COMPLETIONS_BIND     bind address (default 127.0.0.1)
  MEDIA_COMPLETIONS_CANVAS   canvas base URL (default http://127.0.0.1:8781)
  MEDIA_COMPLETIONS_TARGET   session to drive: "tmux:<pane>" | "amux:<name>" |
                             "<tmux-session-name>"  (REQUIRED)
  MEDIA_COMPLETIONS_TOKEN    amux token for /input (X-Auth-Token); blank if the
                             canvas runs MEDIA_VISUAL_TRUST_TAILNET=1
  MEDIA_COMPLETIONS_TIMEOUT  max seconds to wait for a reply (default 180)
  MEDIA_COMPLETIONS_SETTLE   seconds the transcript must be stable + idle before
                             the reply is considered complete (default 2.0)
  MEDIA_COMPLETIONS_MUTE     "1" (default) → ensure the target pane is muted
  MEDIA_COMPLETIONS_API_KEY  if set, require `Authorization: Bearer <key>`
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PORT = 8783
POLL_S = 0.6


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _canvas() -> str:
    return _env("MEDIA_COMPLETIONS_CANVAS", "http://127.0.0.1:8781").rstrip("/")


def _target() -> str:
    return _env("MEDIA_COMPLETIONS_TARGET")


def _media_bin() -> str:
    return shutil.which("media") or str(Path.home() / ".local" / "bin" / "media")


def _get(path: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(_canvas() + path, timeout=8) as r:
            return json.loads(r.read() or b"null")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _post(path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    tok = _env("MEDIA_COMPLETIONS_TOKEN")
    if tok:
        headers["X-Auth-Token"] = tok
    req = urllib.request.Request(_canvas() + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def _resolve_pane(target: str) -> str | None:
    """The tmux pane id to read replies from. tmux:<pane> is direct; amux:/plain
    names are matched against /agents by session/name."""
    if target.startswith("tmux:"):
        return target[len("tmux:"):]
    want = target[len("amux:"):] if target.startswith("amux:") else target
    data = _get("/agents")
    agents = data.get("agents") if isinstance(data, dict) else []
    for a in agents or []:
        if want in (a.get("session"), a.get("name")) and a.get("pane"):
            return a.get("pane")
    return None


def _peek_turns(pane: str) -> list[str]:
    data = _get("/peek?pane=" + urllib.parse.quote(pane))
    return (data.get("turns") if isinstance(data, dict) else None) or []


def _state(pane: str) -> str:
    data = _get("/agents")
    for a in (data.get("agents") if isinstance(data, dict) else []) or []:
        if a.get("pane") == pane:
            return a.get("state") or ""
    return ""


def _new_turns(before_last: str | None, after: list[str]) -> list[str]:
    """The turns added since `before_last` (the last turn seen pre-injection).
    Handles /peek's sliding window: match the last occurrence of before_last."""
    if before_last is None:
        return after
    for i in range(len(after) - 1, -1, -1):
        if after[i] == before_last:
            return after[i + 1:]
    return after[-1:] if after else []   # scrolled off the window — best effort


def _ensure_muted(pane: str, _cache: set = set()) -> None:
    if _env("MEDIA_COMPLETIONS_MUTE", "1") != "1" or pane in _cache:
        return
    try:
        subprocess.run([_media_bin(), "mute-pane", "--pane", pane, "on"],
                       timeout=8, capture_output=True)
        _cache.add(pane)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("completions: could not mute %s: %s", pane, e)


# strip [[visual:]]/markdown so the OWUI thread + its TTS get clean prose (the
# session's own hook still draws the figure on the canvas).
def _clean(text: str) -> str:
    try:
        from agent_media_core.intake._visual import extract_visual_markers
        text = extract_visual_markers(text)[0]
    except Exception:  # noqa: BLE001
        pass
    try:
        from agent_media_core.intake._text import strip_markdown
        return strip_markdown(text).strip()
    except Exception:  # noqa: BLE001
        return text.strip()


def converse(user_text: str) -> str:
    """Inject `user_text` into the target session, wait for the reply, return it
    (marker/markdown-stripped). Raises RuntimeError on any failure."""
    target = _target()
    if not target:
        raise RuntimeError("MEDIA_COMPLETIONS_TARGET is not set")
    pane = _resolve_pane(target)
    if not pane:
        raise RuntimeError(f"could not resolve a pane for target {target!r}")
    _ensure_muted(pane)

    before = _peek_turns(pane)
    before_last = before[-1] if before else None

    code, _ = _post("/input", {"text": user_text, "target": target})
    if code in (401, 403):
        raise RuntimeError("canvas /input rejected the token (MEDIA_COMPLETIONS_TOKEN)")
    if code == 0:
        raise RuntimeError("could not reach the canvas /input endpoint")

    timeout = float(_env("MEDIA_COMPLETIONS_TIMEOUT", "180"))
    settle = float(_env("MEDIA_COMPLETIONS_SETTLE", "2.0"))
    deadline = time.time() + timeout
    seen_new, stable_since, last_len = False, None, len(before)

    while time.time() < deadline:
        time.sleep(POLL_S)
        turns = _peek_turns(pane)
        new = _new_turns(before_last, turns)
        if new:
            seen_new = True
        idle = _state(pane) not in ("working",)
        if seen_new and idle:
            if len(turns) == last_len:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= settle:
                    return _clean("\n\n".join(new))
            else:
                stable_since = None
        last_len = len(turns)

    # timed out — return whatever landed rather than nothing
    return _clean("\n\n".join(_new_turns(before_last, _peek_turns(pane))))


# ---- OpenAI wire format -----------------------------------------------------

def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p] or ([text.strip()] if text.strip() else [])


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-media-completions-shim/0.1"

    def log_message(self, fmt: str, *a: object) -> None:
        if _env("MEDIA_COMPLETIONS_DEBUG") == "1":
            log.info("%s - " + fmt, self.address_string(), *a)

    def _authorized(self) -> bool:
        key = _env("MEDIA_COMPLETIONS_API_KEY")
        if not key:
            return True
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
        return got == key

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(200, {"status": "ok", "target": _target()})
        elif self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "agent-media", "object": "model", "owned_by": "agent-media"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/v1/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        if not self._authorized():
            self._json(401, {"error": {"message": "invalid api key"}})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": {"message": "invalid JSON body"}})
            return

        user = ""
        for m in reversed(body.get("messages") or []):
            if m.get("role") == "user":
                c = m.get("content")
                user = c if isinstance(c, str) else "\n".join(
                    p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"
                ) if isinstance(c, list) else ""
                break
        if not user.strip():
            self._json(400, {"error": {"message": "no user message"}})
            return

        model = body.get("model") or "agent-media"
        try:
            reply = converse(user)
        except RuntimeError as e:
            self._json(502, {"error": {"message": str(e)}})
            return
        if not reply:
            reply = "(the agent produced no reply before the timeout)"

        if body.get("stream"):
            self._stream(reply, model)
        else:
            self._json(200, {
                "id": "chatcmpl-am", "object": "chat.completion",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": reply}}],
            })

    def _stream(self, reply: str, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def chunk(delta: dict, finish=None) -> None:
            payload = {"id": "chatcmpl-am", "object": "chat.completion.chunk",
                       "created": int(time.time()), "model": model,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()

        try:
            chunk({"role": "assistant"})
            # sentence-granular so Call-mode TTS can start speaking sooner
            for i, s in enumerate(_sentences(reply)):
                chunk({"content": ("" if i == 0 else " ") + s})
            chunk({}, finish="stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="media-completions-shim")
    ap.add_argument("--port", type=int,
                    default=int(_env("MEDIA_COMPLETIONS_PORT", str(DEFAULT_PORT))))
    ap.add_argument("--bind", default=_env("MEDIA_COMPLETIONS_BIND", "127.0.0.1"))
    args = ap.parse_args()
    if not _target():
        log.warning("MEDIA_COMPLETIONS_TARGET not set — requests will 502 until it is")

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    log.info("completions-shim on http://%s:%d/v1  (target=%s, canvas=%s)",
             args.bind, args.port, _target() or "UNSET", _canvas())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
