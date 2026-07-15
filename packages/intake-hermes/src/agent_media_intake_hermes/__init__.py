"""Hermes Agent intake — shell-hook JSON adapter.

Hermes fires shell hooks (configured in ``~/.hermes/config.yaml``) at
lifecycle points, piping a JSON payload on **stdin** and reading an optional
JSON directive on **stdout** (see the Hermes "Event Hooks" docs). This adapter
wires the ``post_llm_call`` event — which carries the assistant's final reply
for the turn — into agent-media so Hermes speaks aloud, exactly like the
Claude Code / Codex intake hooks.

Wire it in ``~/.hermes/config.yaml``::

    hooks:
      post_llm_call:
        - command: "media-hook-hermes"
          timeout: 15

The payload for ``post_llm_call`` carries the reply under ``assistant_response``
(Hermes canonical). We also accept ``response_text`` (``transform_llm_output``)
and ``extra.assistant_response`` for forward-compatibility. Playback is
detached (fork + setsid) so the hook returns to Hermes immediately and the
speech outlives the short hook timeout — the same technique the Claude Code
Stop hook uses.

Env overrides (checked before the generic ``MEDIA_RENDER_*`` vars):
  HERMES_TTS_ENABLED=0            disable this hook
  HERMES_TTS_ENGINE=<name>        force a render engine for Hermes turns
  HERMES_TTS_VOICE[_<ENGINE>]     per-source voice override
"""

from __future__ import annotations

import json
import os
import sys

from agent_media_core.intake import strip_markdown, submit_event
from agent_media_core.types import Event, Priority, Source


# Payload keys that may carry the assistant's final reply, most-preferred first.
# post_llm_call -> assistant_response; transform_llm_output -> response_text.
_TEXT_KEYS = ("assistant_response", "response_text", "response", "text")


def _extract_text(payload: dict) -> str:
    """Pull the assistant reply out of a Hermes hook payload."""
    for key in _TEXT_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    # Event-specific kwargs land under `extra`.
    extra = payload.get("extra")
    if isinstance(extra, dict):
        for key in _TEXT_KEYS:
            val = extra.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return ""


def _resolve_engine_voice() -> tuple[str | None, str | None]:
    """HERMES_-prefixed engine/voice overrides, mirroring the stdin-hook logic."""
    engine = os.environ.get("HERMES_TTS_ENGINE") or os.environ.get("MEDIA_RENDER_ENGINE")
    voice = None
    if engine:
        voice = os.environ.get(
            f"HERMES_TTS_VOICE_{engine.upper().replace('-', '_')}")
    voice = voice or os.environ.get("HERMES_TTS_VOICE")
    return engine, voice


def _play_detached(event: Event) -> None:
    """Render + play in a session-detached child so the hook returns at once.

    Hermes waits on the hook subprocess up to its (short) timeout, so a
    blocking ``submit_event`` would either truncate long replies or stall the
    agent. Fork once and ``setsid``: the child leads a new session, reparents
    to init when we exit, and plays the whole reply. ``MEDIA_HOOK_NO_DETACH=1``
    forces inline play (tests/debug); a fork failure also falls back to inline.
    """
    if os.environ.get("MEDIA_HOOK_NO_DETACH"):
        submit_event(event)
        return
    try:
        pid = os.fork()
    except OSError:
        submit_event(event)  # no fork available → inline, bounded by hook timeout
        return
    if pid > 0:
        return  # parent: return immediately; child reparents to init on exit
    # --- child ---
    try:
        os.setsid()
    except OSError:
        pass
    # Detach stdio so Hermes's hook pipe sees EOF and the reader unblocks.
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                pass
    except OSError:
        pass
    try:
        submit_event(event)
    except Exception:  # noqa: BLE001 — detached; nothing to report to
        pass
    finally:
        os._exit(0)


def main() -> int:
    # Global + per-source kill switches (match the stdin-hook convention).
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    if os.environ.get("HERMES_TTS_ENABLED", "1") == "0":
        _emit_noop()
        return 0

    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return 0

    payload: dict = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except (json.JSONDecodeError, ValueError):
            # Not JSON — treat the whole stdin as the text to speak (lenient).
            payload = {"text": raw}

    cleaned = strip_markdown(_extract_text(payload))
    if not cleaned:
        _emit_noop()
        return 0

    engine, voice = _resolve_engine_voice()
    session = str(payload.get("session_id") or "")

    _play_detached(Event(
        text=cleaned,
        source=Source.HERMES,
        priority=Priority.NORMAL,
        engine=engine,
        voice=voice,
        metadata={"kind": "stop", "session": session},
    ))

    _emit_noop()
    return 0


def _emit_noop() -> None:
    """Emit an empty JSON directive so Hermes reads a clean no-op on stdout."""
    try:
        sys.stdout.write("{}\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
