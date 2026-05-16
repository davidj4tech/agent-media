"""aar-tts-render — compatibility shim. Engine logic lives in
``agent_media_core.render``; this module exposes the original CLI and
``render_text``/``_default_openai_python`` import surface so tts-stream
and existing scripts keep working unchanged.

Engines: edge, openai, qwen, realtime (new — first-class).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agent_media_core.render import (
    EDGE_DEFAULT_VOICE,
    OPENAI_DEFAULT_MODEL,
    OPENAI_DEFAULT_VOICE,
    QWEN_DEFAULT_BASE_URL,
    QWEN_DEFAULT_LANG,
    QWEN_DEFAULT_MODEL,
    QWEN_DEFAULT_VOICE,
    REALTIME_DEFAULT_MODEL,
    REALTIME_DEFAULT_VOICE,
    default_openai_python as _default_openai_python,  # re-export
    render_text,  # re-export
)

__all__ = ["render_text", "_default_openai_python", "main"]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aar-tts-render",
        description="Render TTS audio from stdin text to a file.",
    )
    p.add_argument("--engine", required=True,
                   choices=["edge", "openai", "qwen", "realtime"])
    p.add_argument("--out", required=True, type=Path,
                   help="Output audio file path (.mp3 for edge/openai, "
                        ".wav for qwen/realtime)")
    p.add_argument("--voice", default=None,
                   help="Engine-specific voice (overrides per-engine default)")
    p.add_argument("--edge-voice",
                   default=os.environ.get("RELAY_EDGE_VOICE", EDGE_DEFAULT_VOICE))
    p.add_argument("--edge-bin",
                   default=os.environ.get("RELAY_EDGE_TTS_BIN", "edge-tts"))
    p.add_argument("--openai-voice",
                   default=os.environ.get("RELAY_OPENAI_VOICE", OPENAI_DEFAULT_VOICE))
    p.add_argument("--openai-model",
                   default=os.environ.get("RELAY_OPENAI_MODEL", OPENAI_DEFAULT_MODEL))
    p.add_argument("--openai-python",
                   default=os.environ.get("RELAY_OPENAI_PYTHON", "python3"))
    p.add_argument("--qwen-voice",
                   default=os.environ.get("RELAY_QWEN_VOICE", QWEN_DEFAULT_VOICE))
    p.add_argument("--qwen-model",
                   default=os.environ.get("RELAY_QWEN_MODEL", QWEN_DEFAULT_MODEL))
    p.add_argument("--qwen-lang",
                   default=os.environ.get("RELAY_QWEN_LANG", QWEN_DEFAULT_LANG))
    p.add_argument("--qwen-base-url",
                   default=os.environ.get("DASHSCOPE_BASE_URL", QWEN_DEFAULT_BASE_URL))
    p.add_argument("--realtime-voice",
                   default=os.environ.get("RELAY_REALTIME_VOICE", REALTIME_DEFAULT_VOICE))
    p.add_argument("--realtime-model",
                   default=os.environ.get("RELAY_REALTIME_MODEL", REALTIME_DEFAULT_MODEL))
    p.add_argument("--realtime-python",
                   default=os.environ.get("MEDIA_REALTIME_PYTHON")
                           or os.environ.get("CLAUDE_TTS_REALTIME_PYTHON")
                           or "")
    p.add_argument("--no-fallback", dest="fallback", action="store_false",
                   help="Don't fall back to edge on engine failure")
    p.set_defaults(fallback=True)
    return p


def main() -> None:
    args = _build_parser().parse_args()
    text = sys.stdin.read()
    if not text.strip():
        print("aar-tts-render: empty stdin", file=sys.stderr)
        sys.exit(2)

    if args.engine == "openai":
        args.openai_python = _default_openai_python(args.openai_python)

    args.out.parent.mkdir(parents=True, exist_ok=True)

    def _fallback_log(engine: str, err: str) -> None:
        print(f"aar-tts-render: {engine} failed ({err or 'no stderr'}); "
              "falling back to edge", file=sys.stderr)

    ok, err = render_text(
        text, args.out,
        engine=args.engine,
        voice=args.voice,
        edge_voice=args.edge_voice,
        edge_bin=args.edge_bin,
        openai_voice=args.openai_voice,
        openai_model=args.openai_model,
        openai_python=args.openai_python,
        qwen_voice=args.qwen_voice,
        qwen_model=args.qwen_model,
        qwen_lang=args.qwen_lang,
        qwen_base_url=args.qwen_base_url,
        realtime_voice=args.realtime_voice,
        realtime_model=args.realtime_model,
        realtime_python=args.realtime_python or None,
        fallback_to_edge=args.fallback,
        on_fallback=_fallback_log,
    )
    if not ok:
        print(f"aar-tts-render: {args.engine} failed: {err or 'unknown error'}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
