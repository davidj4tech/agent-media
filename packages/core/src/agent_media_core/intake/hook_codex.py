"""Codex (OpenAI CLI) intake adapter.

Codex pipes the assistant response text into the hook on stdin. We
strip markdown, wrap as an Event(source=CODEX), and submit through the
shared pipeline.

Replaces the bash hook at
``packages/audio-relay/src/agent_audio_relay/shell/hooks/codex-tts-hook.sh``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from ..types import Event, Priority, Source
from ._text import strip_markdown
from .submit import submit_event


log = logging.getLogger(__name__)


def _load_env_file() -> None:
    """Mirror hook_claude_code._load_env_file. Codex doesn't need
    secrets for the default edge engine, but a user may have overridden
    to openai/realtime — same env file gives the same keys.
    """
    candidates = [
        os.environ.get("RELAY_ENV_FILE") or "",
        str(Path.home() / ".config" / "agent-audio-relay.env"),
    ]
    for path in candidates:
        if not path:
            continue
        try:
            with open(path, "r") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):]
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
            return
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("hook-codex: failed to read %s: %s", path, e)
            return


def main() -> int:
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    if os.environ.get("CODEX_TTS_ENABLED", "1") == "0":
        return 0

    _load_env_file()

    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return 0

    cleaned = strip_markdown(raw)
    if not cleaned:
        return 0

    # Codex-specific overrides take precedence over the generic ones,
    # so the user can pin a different voice/engine per source.
    engine = (os.environ.get("CODEX_TTS_ENGINE")
              or os.environ.get("MEDIA_RENDER_ENGINE"))
    voice = (os.environ.get("CODEX_TTS_VOICE")
             or os.environ.get("MEDIA_RENDER_VOICE"))

    submit_event(
        Event(
            text=cleaned,
            source=Source.CODEX,
            priority=Priority.NORMAL,
            engine=engine,
            voice=voice,
            metadata={"kind": "stop"},
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
