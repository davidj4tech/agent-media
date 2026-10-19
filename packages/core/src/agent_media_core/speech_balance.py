"""How loud speech is next to music in the Sasonica app: one balance.

On the phone the app's speech player and its music player share Android's
media volume, so the volume keys move both. Each player also has its own
0-100 gain (sasonica-app Media3Speech, Media3Music), and that is where the
balance acts: -100..100, 0 (both full) until set.

- **above 0** speech is louder: the music plays at 100 - balance.
- **below 0** music is louder: speech plays at 100 + balance.

The phone's volume keys stay the master over both. Speech gets its level each
time a reply starts (sinks/speech.py `_start_cmds`) and after a duck; music
gets its when a track is loaded (sinks/music_sasonica.py `play`). Moving the
balance applies it at once too (`apply_now`), best effort.

Only the app's players (target `sasonica`): the rooms, the Termux players
and red5's speakers keep their own volumes. Kept in
`<state_dir>/speech-balance.json` as `{"balance", "at"}`; the server's, so
every device paired with it (`GET`/`POST /speech/balance`, the app's
Settings). David, 19 Oct 2026: "it would be nice to have a different volume
for speech and music".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ._paths import state_dir

NAME = "speech-balance.json"
#: The speech and music targets the balance sets (the app's own players).
TARGET = "sasonica"
LIMIT = 100


def _path() -> Path:
    return state_dir() / NAME


def _clamp(value) -> int:
    return max(-LIMIT, min(LIMIT, int(round(float(value)))))


def balance() -> int:
    """The balance, -100 (music louder) .. 100 (speech louder); 0 until set."""
    try:
        data = json.loads(_path().read_text())
        return _clamp(data["balance"])
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def set_balance(value) -> int:
    """Save the balance (clamped to ±100) and return it."""
    b = _clamp(value)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps({"balance": b, "at": time.time()}))
    tmp.replace(path)
    return b


def speech_level(b: int | None = None) -> int:
    """The app's speech gain, 0-100."""
    b = balance() if b is None else b
    return LIMIT + min(0, b)


def music_level(b: int | None = None) -> int:
    """The app's music gain, 0-100."""
    b = balance() if b is None else b
    return LIMIT - max(0, b)


def applies_to(target_name: str) -> bool:
    return target_name == TARGET


def apply_now() -> None:
    """Set both app players to the saved balance now. Best effort: an app
    not connected takes it at its next reply or track. Music that is ducked
    under a reply is left alone: the duck's own restore puts it back, and the
    next track takes the new level."""
    from .sinks import _mpv_ipc as ipc
    from .sinks.speech import _socket_for
    from .types import Target

    b = balance()
    try:
        if os.environ.get(f"MEDIA_SPEECH_SOCKET_{TARGET.upper()}"):
            ipc.set_property(_socket_for(Target(name=TARGET)), "volume",
                             float(speech_level(b)))
    except (ipc.MpvIpcError, OSError):
        pass
    try:
        from .sinks import music_sasonica
        if music_sasonica.configured() and not _music_ducked():
            ipc.set_property(music_sasonica.endpoint(), "volume",
                             float(music_level(b)))
    except (ipc.MpvIpcError, OSError):
        pass


def _music_ducked() -> bool:
    """A reply is being spoken, so the music may be ducked under it."""
    try:
        from .sinks.speech import SPEAKING_PROPERTY, _socket_for
        from .sinks import _mpv_ipc as ipc
        from .types import Target
        return bool(ipc.get_property(_socket_for(Target(name=TARGET)),
                                     SPEAKING_PROPERTY))
    except Exception:  # noqa: BLE001 — unknown is "not ducked"
        return False
