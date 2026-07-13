"""OpenAI-compatible TTS shim → agent-media voice (+ canvas).

A tiny stdlib HTTP server exposing the slice of the OpenAI audio API that chat
front-ends (Open WebUI, SillyTavern) call for text-to-speech:

  POST /v1/audio/speech   {"model","input","voice","response_format","speed"}
                          → audio bytes, rendered through agent-media's engines
  GET  /v1/models         → advertises the "agent-media" voice model
  GET  /healthz           → liveness

Why it exists: it makes OWUI's TTS — including hands-free **Call mode** — speak
in agent-media's voice instead of the browser's. Call mode's turn-taking works
because we return the full audio, so OWUI knows when the utterance ends and
reopens the mic. Markers (`[[visual:]]`/`[[reveal:]]`) and markdown are stripped
so they aren't read aloud, and — unlike the room-routed speech path — the audio
is returned to the *caller's tab*, making OWUI/SillyTavern a per-device sink.
Set MEDIA_SHIM_CANVAS=1 to also fire the figure onto the shared canvas.

Config (env):
  MEDIA_SHIM_PORT     listen port (default 8782 — clip 8780, canvas 8781)
  MEDIA_SHIM_BIND     bind address (default 127.0.0.1 — same-host OWUI)
  MEDIA_SHIM_API_KEY  if set, require it as `Authorization: Bearer <key>`
  MEDIA_SHIM_CANVAS   "1" → also spawn the canvas visual for each utterance
  MEDIA_RENDER_ENGINE the agent-media engine to render with (default edge)
  MEDIA_RENDER_VOICE  fallback voice when the request doesn't name a real one
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_media_core.render import render_text

from . import personas

log = logging.getLogger(__name__)

DEFAULT_PORT = 8782

# OpenAI's canned voice names carry no meaning for our engines — treat them as
# "use the configured default" rather than force-feeding them to edge/qwen.
_OPENAI_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer", "ash",
                  "ballad", "coral", "sage", "verse"}

# response_format → (suffix, content-type). edge emits mp3 regardless; a plugin
# engine may honour the suffix. Unknown formats fall back to mp3.
_FORMATS = {
    "mp3":  (".mp3",  "audio/mpeg"),
    "opus": (".opus", "audio/ogg"),
    "aac":  (".aac",  "audio/aac"),
    "flac": (".flac", "audio/flac"),
    "wav":  (".wav",  "audio/wav"),
    "pcm":  (".pcm",  "audio/pcm"),
}


def _api_key() -> str:
    return (os.environ.get("MEDIA_SHIM_API_KEY") or "").strip()


def _engine() -> str:
    return (os.environ.get("MEDIA_RENDER_ENGINE") or "edge").strip() or "edge"


def _canvas_on() -> bool:
    if (os.environ.get("MEDIA_SHIM_CANVAS") or "").strip() == "1":
        return True
    try:
        from agent_media_core.intake._visual import visual_enabled
        return visual_enabled()
    except Exception:  # noqa: BLE001
        return False


def _resolve_voice(requested: str | None) -> str | None:
    v = (requested or "").strip()
    if not v or v.lower() in _OPENAI_VOICES:
        return (os.environ.get("MEDIA_RENDER_VOICE") or "").strip() or None
    return v


def _prepare_text(raw: str) -> tuple[str, str]:
    """(spoken, raw_clean): strip visual markers, then markdown, for TTS.
    Returns the marker-clean raw too, so the canvas gets the full reply."""
    raw = raw or ""
    try:
        from agent_media_core.intake._visual import extract_visual_markers
        clean, hint, _pre, _post = extract_visual_markers(raw)
    except Exception:  # noqa: BLE001
        clean, hint = raw, ""
    try:
        from agent_media_core.intake._text import strip_markdown
        spoken = strip_markdown(clean)
    except Exception:  # noqa: BLE001
        spoken = clean
    return spoken.strip(), (clean.strip(), hint)


def _canvas_url() -> str:
    return (os.environ.get("MEDIA_SHIM_CANVAS_URL") or "http://127.0.0.1:8781").rstrip("/")


def _canvas_session(voice_raw: str) -> str:
    """Scene-continuity key for the canvas. Per-persona by default: each
    SillyTavern character (distinct voice) evolves its own artwork. Pin all
    surfaces to one scene with MEDIA_SHIM_SESSION."""
    env = (os.environ.get("MEDIA_SHIM_SESSION") or "").strip()
    return env or (voice_raw or "").strip() or "owui"


def _maybe_canvas(raw_clean: str, spoken: str, hint: str, session: str) -> None:
    if not _canvas_on():
        return
    try:
        from agent_media_core.intake._visual import spawn_visual
        spawn_visual(raw_clean, spoken, session=session, hint=hint)
    except Exception as e:  # noqa: BLE001
        log.debug("shim: canvas spawn skipped: %s", e)


def _synth(text: str, voice: str | None, fmt: str) -> tuple[bytes, str]:
    suffix, ctype = _FORMATS.get(fmt, _FORMATS["mp3"])
    tmp = Path(tempfile.mkdtemp(prefix="amc-tts-")) / f"out{suffix}"
    try:
        ok, err = render_text(text, tmp, engine=_engine(), voice=voice)
        if not ok or not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError(err or "render produced no audio")
        return tmp.read_bytes(), ctype
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-media-tts-shim/0.1"

    def log_message(self, fmt: str, *args: object) -> None:  # quiet by default
        if (os.environ.get("MEDIA_SHIM_DEBUG") or "") == "1":
            log.info("%s - " + fmt, self.address_string(), *args)

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        key = _api_key()
        if not key:
            return True
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
        return got == key

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
        elif self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "agent-media", "object": "model", "owned_by": "agent-media"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/v1/audio/speech"):
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

        raw = str(body.get("input") or "")
        if not raw.strip():
            self._json(400, {"error": {"message": "no input text"}})
            return
        fmt = str(body.get("response_format") or "mp3").lower()
        voice = _resolve_voice(body.get("voice"))
        spoken, (raw_clean, hint) = _prepare_text(raw)
        if not spoken:
            self._json(400, {"error": {"message": "nothing speakable after cleaning"}})
            return

        try:
            audio, ctype = _synth(spoken, voice, fmt)
        except Exception as e:  # noqa: BLE001
            log.warning("shim: render failed: %s", e)
            self._json(502, {"error": {"message": f"tts render failed: {e}"}})
            return

        # Canvas: a persona with a portrait shows its FACE (unless the reply
        # carries an explicit [[visual:]] figure); everything else falls back to
        # the generated-figure path.
        persona = str(body.get("voice") or "")
        session = _canvas_session(persona)
        if _canvas_on() and not hint and personas.push(persona, spoken, session, _canvas_url()):
            pass  # persona portrait shown
        else:
            _maybe_canvas(raw_clean, spoken, hint, session)

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="media-tts-shim")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("MEDIA_SHIM_PORT", DEFAULT_PORT)))
    ap.add_argument("--bind",
                    default=os.environ.get("MEDIA_SHIM_BIND", "127.0.0.1"))
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    log.info("tts-shim: listening on http://%s:%d/v1  (engine=%s, canvas=%s)",
             args.bind, args.port, _engine(), _canvas_on())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
