"""Render engines: edge, openai, qwen, realtime (built-in) + third-party.

All four built-ins are first-class: `render_text(..., engine="<name>", ...)`
works for any of them. An unknown engine name is resolved against third-party
engines registered via the `agent_media.render_engines` entry-point group
(see ../extensions.py and docs/EXTENSIONS.md), so packages can add engines
without core importing them. Fallback to edge on non-edge failure is on by
default (caller can disable) and applies to third-party engines too.

This is a port + extension of the original `aar-tts-render` Python and
the TypeScript realtime path from `pi-tts-extension.ts`. Behaviour is
preserved for edge/openai/qwen; realtime is new on the Python side.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Optional


# Default voices and models — kept identical to the old aar-tts-render
# so the migration shim's behaviour matches.
EDGE_DEFAULT_VOICE = "en-US-AriaNeural"
OPENAI_DEFAULT_VOICE = "marin"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini-tts"
QWEN_DEFAULT_VOICE = "Cherry"
QWEN_DEFAULT_MODEL = "qwen3-tts-flash-2025-11-27"
QWEN_DEFAULT_LANG = "English"
QWEN_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/api/v1"
REALTIME_DEFAULT_MODEL = "gpt-realtime"
REALTIME_DEFAULT_VOICE = "marin"


def _render_edge(text: str, outfile: Path, *, voice: str, edge_bin: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [edge_bin, "--text", text, "--voice", voice, "--write-media", str(outfile)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    err = proc.stderr.decode(errors="replace").strip()
    ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
    return ok, err


def _render_openai(text: str, outfile: Path, *, voice: str, model: str,
                   python_bin: str) -> tuple[bool, str]:
    script = (
        "import os\n"
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "with client.audio.speech.with_streaming_response.create(\n"
        "    model=os.environ['TTS_MODEL'],\n"
        "    voice=os.environ['TTS_VOICE'],\n"
        "    input=os.environ['TTS_TEXT'],\n"
        ") as r:\n"
        "    r.stream_to_file(os.environ['TTS_OUTFILE'])\n"
    )
    env = {**os.environ, "TTS_MODEL": model, "TTS_VOICE": voice,
           "TTS_TEXT": text, "TTS_OUTFILE": str(outfile)}
    proc = subprocess.run(
        [python_bin, "-c", script],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    err = proc.stderr.decode(errors="replace").strip()
    ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
    return ok, err


def _render_qwen(text: str, outfile: Path, *, voice: str, model: str, language: str,
                 base_url: str, api_key: str) -> tuple[bool, str]:
    if not api_key:
        return False, "DASHSCOPE_API_KEY not set"
    url = f"{base_url.rstrip('/')}/services/aigc/multimodal-generation/generation"
    payload = json.dumps({
        "model": model,
        "input": {"text": text, "voice": voice, "language_type": language},
    }).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except Exception as e:  # noqa: BLE001
        return False, f"qwen http: {e}"
    try:
        data = json.loads(body)
    except ValueError as e:
        return False, f"qwen json: {e}"
    audio_url = (
        (data.get("output") or {}).get("audio", {}).get("url")
        or (data.get("output") or {}).get("url")
        or data.get("url")
    )
    if not audio_url:
        return False, f"qwen response missing audio url: {json.dumps(data)[:200]}"
    try:
        with urllib.request.urlopen(audio_url, timeout=60) as resp:
            outfile.write_bytes(resp.read())
    except Exception as e:  # noqa: BLE001
        return False, f"qwen download: {e}"
    if not outfile.exists() or outfile.stat().st_size == 0:
        return False, "qwen download produced empty file"
    return True, ""


def _render_realtime(text: str, outfile: Path, *, voice: str, model: str,
                     python_bin: str) -> tuple[bool, str]:
    """Run the realtime WebSocket flow in a subprocess so the caller's
    Python doesn't need `websockets`. `python_bin` should point to a venv
    that has the package — usually `~/.local/share/aar-realtime-venv/bin/python`.
    """
    script_path = Path(__file__).with_name("_realtime_subprocess.py")
    cfg = json.dumps({"text": text, "model": model, "voice": voice,
                      "outfile": str(outfile)})
    proc = subprocess.run(
        [python_bin, str(script_path)],
        input=cfg.encode(),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    err = proc.stderr.decode(errors="replace").strip()
    ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
    return ok, err


def default_openai_python(current: str) -> str:
    """If `current` doesn't have the `openai` module, look for one that
    does in the usual pipx venv locations. Returns `current` unchanged
    if no better option is found.
    """
    pipx_root = Path(os.environ.get("PIPX_HOME", Path.home() / ".local" / "pipx"))
    for c in (current,
              str(pipx_root / "venvs" / "openai" / "bin" / "python3"),
              str(pipx_root / "venvs" / "llm" / "bin" / "python3")):
        if not c:
            continue
        try:
            r = subprocess.run([c, "-c", "import openai"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2)
            if r.returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return current


KNOWN_ENGINES = ("edge", "openai", "qwen", "realtime")


def render_text(
    text: str,
    outfile: Path,
    *,
    engine: str,
    voice: Optional[str] = None,
    edge_voice: str = EDGE_DEFAULT_VOICE,
    edge_bin: str = "edge-tts",
    openai_voice: str = OPENAI_DEFAULT_VOICE,
    openai_model: str = OPENAI_DEFAULT_MODEL,
    openai_python: str = "python3",
    qwen_voice: str = QWEN_DEFAULT_VOICE,
    qwen_model: str = QWEN_DEFAULT_MODEL,
    qwen_lang: str = QWEN_DEFAULT_LANG,
    qwen_base_url: str = QWEN_DEFAULT_BASE_URL,
    qwen_api_key: Optional[str] = None,
    realtime_voice: str = REALTIME_DEFAULT_VOICE,
    realtime_model: str = REALTIME_DEFAULT_MODEL,
    realtime_python: Optional[str] = None,
    fallback_to_edge: bool = True,
    on_fallback: Optional[Callable[[str, str], None]] = None,
) -> tuple[bool, str]:
    """Render `text` to `outfile` via `engine`. Returns (ok, err).

    On non-edge engine failure with `fallback_to_edge=True`, falls back
    to edge. `on_fallback(engine, err)` is called so callers can log the
    original engine's error.
    """
    if engine == "edge":
        return _render_edge(text, outfile,
                            voice=voice or edge_voice, edge_bin=edge_bin)
    if engine == "openai":
        ok, err = _render_openai(
            text, outfile,
            voice=voice or openai_voice, model=openai_model,
            python_bin=openai_python,
        )
    elif engine == "qwen":
        ok, err = _render_qwen(
            text, outfile,
            voice=voice or qwen_voice, model=qwen_model, language=qwen_lang,
            base_url=qwen_base_url,
            api_key=qwen_api_key if qwen_api_key is not None
                else os.environ.get("DASHSCOPE_API_KEY", ""),
        )
    elif engine == "realtime":
        py = realtime_python or os.environ.get("MEDIA_REALTIME_PYTHON") \
            or os.environ.get("CLAUDE_TTS_REALTIME_PYTHON") or sys.executable
        ok, err = _render_realtime(
            text, outfile,
            voice=voice or realtime_voice, model=realtime_model, python_bin=py,
        )
    else:
        # Not a built-in: look for a third-party engine registered via the
        # `agent_media.render_engines` entry-point group (see extensions.py).
        from ..extensions import get_render_engine
        ext = get_render_engine(engine)
        if ext is None:
            return False, f"unknown engine: {engine}"
        try:
            ok, err = ext(text, outfile, voice=voice)
        except Exception as e:  # noqa: BLE001 — isolate plugin faults; fall back below
            ok, err = False, f"engine {engine!r} raised: {e}"
    if not ok and fallback_to_edge:
        if on_fallback is not None:
            on_fallback(engine, err)
        return _render_edge(text, outfile, voice=edge_voice, edge_bin=edge_bin)
    return ok, err
