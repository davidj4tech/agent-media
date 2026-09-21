"""Where the audio goes: the app's picker for speech and music.

server-contract.md §6.9. The choice itself lives in core
(`agent_media_core.audio_targets`), where `media speech-target` and the
speech path read it; this module is the two routes in front of it.

  GET  /audio/targets → {"ok", "channels": {"speech": {...}, "music": {...}}}
  POST /audio/target  {"channel": "speech"|"music", "target": name|null}
                      → {"ok", "channel", <that channel's block>}

Speech: `current` is where the next reply plays (the override, else the env
default); a reply already speaking is not moved. Music: `current` is where it
is playing, when that is known without asking a player; a choice sets the
`--where` for the next untargeted play only.

Nothing here opens a connection. The options are read from the env, the
state dir and a socket's existence, and cached for a few seconds because the
app polls this beside /speech/now.
"""

from __future__ import annotations

import sys
import threading
import time

from . import auth

CHANNELS = ("speech", "music")

#: `{channel: (at, block)}` — the options are env and file reads, cheap, but
#: the picker is open while the bar polls, and nothing in them changes faster
#: than a person can tap. A POST drops the cache.
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_S = 5.0
_LOCK = threading.Lock()


def _block(channel: str) -> dict:
    from agent_media_core import audio_targets

    if channel == "speech":
        return audio_targets.speech_block()
    from agent_media_core.state import StateStore

    try:
        intent = StateStore().get_music_intent()
    except Exception:  # noqa: BLE001 — no store is "not known", not an error
        intent = None
    return audio_targets.music_block(intent)


def channel_block(channel: str) -> dict:
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(channel)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    block = _block(channel)
    with _LOCK:
        _CACHE[channel] = (now, block)
    return block


def _reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def targets(bearer: str) -> tuple[bool, dict]:
    """`GET /audio/targets` — gated like /speech/now."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return True, {"channels": {c: channel_block(c) for c in CHANNELS}}


def set_target(body: dict, bearer: str) -> tuple[bool, dict]:
    """`POST /audio/target` — gated like /speech/ctl: moving the voice is a
    listener's control, the same as pausing it."""
    ok, err = auth.may_control_speech(bearer)
    if not ok:
        return False, err
    channel = str(body.get("channel") or "")
    if channel not in CHANNELS:
        return False, {"error": "unknown channel", "status": 400}
    target = body.get("target")
    if target is not None and not isinstance(target, str):
        return False, {"error": "target must be a name or null", "status": 400}
    from agent_media_core import audio_targets

    setter = (audio_targets.set_speech_override if channel == "speech"
              else audio_targets.set_music_pref)
    try:
        setter(target or None)
    except ValueError as e:
        return False, {"error": str(e), "status": 400}
    except OSError as e:
        return False, {"error": f"could not save: {e}", "status": 500}
    _reset_cache()
    print(f"audio/target: {channel} -> {target or '(default)'}", file=sys.stderr)
    return True, {"channel": channel, **channel_block(channel)}


def speech_target_now(live: bool) -> str | None:
    """Where the speech bar's voice is: the live reply's own target while one
    is speaking or paused, else where the next one will play."""
    try:
        from agent_media_core import audio_targets

        if live:
            from agent_media_core.state import StateStore

            name = (StateStore().get_now_playing("speech") or {}).get("target")
            if name:
                return str(name)
        return audio_targets.speech_default()
    except Exception:  # noqa: BLE001 — the bar must answer without it
        return None
