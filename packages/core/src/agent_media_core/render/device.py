"""Speech the listening device renders in its own voice.

A target with ``MEDIA_SPEECH_RENDER_<TARGET>=device`` (Sasonica, whose
player has Android's TextToSpeech behind it) is sent the words instead of
audio. Each sentence still becomes a clip *file* here — a ``.tts`` file
holding the sentence — so everything that follows a reply by its clip paths
(history, the transcript's line ids, replay, the stream lane's lead) works
unchanged; only the address handed to the player differs:
``tts:<clip name>?text=<sentence>[&voice=<name>]`` (see ``tts_uri``), which
the device renders on arrival and caches.

No audio exists on this host for such a turn. That is deliberate (David,
26 Sep 2026: clips on demand): a replay goes back to the device, which
renders it again.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

SUFFIX = ".tts"

#: Characters per second of Google's voices at 1.0, measured on p8a
#: (26 Sep 2026: 14.2-16.0 across three sentences). An estimate only — the
#: measured starts (clip_starts_s) take over as each sentence plays.
CHARS_PER_S = 15.0
LEAD_S = 0.2


def _key(prefix: str, target_name: str) -> str:
    return f"{prefix}_{target_name.upper().replace('-', '_')}"


def renders_on_device(target_name: str) -> bool:
    return os.environ.get(_key("MEDIA_SPEECH_RENDER", target_name), "").strip().lower() == "device"


def voice_for(target_name: str) -> Optional[str]:
    return os.environ.get(_key("MEDIA_SPEECH_DEVICE_VOICE", target_name)) or None


def is_clip(path: "str | Path") -> bool:
    return str(path).endswith(SUFFIX)


def write_clip(text: str, outfile: Path, **_ignored) -> tuple[bool, Optional[str]]:
    """The render_text stand-in: the clip is the sentence itself."""
    try:
        Path(outfile).write_text(text, encoding="utf-8")
    except OSError as e:
        return False, str(e)
    return True, None


def estimate_duration(path: "str | Path") -> float:
    """Seconds the device will take to say this clip at 1.0, or 0.0."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return 0.0
    return LEAD_S + len(text.strip()) / CHARS_PER_S


def tts_uri(path: "str | Path", voice: Optional[str] = None) -> str:
    """The address a device-rendering player is handed for a ``.tts`` clip."""
    p = Path(path)
    text = p.read_text(encoding="utf-8").strip()
    uri = f"tts:{p.stem}?text={quote(text, safe='')}"
    if voice:
        uri += f"&voice={quote(voice, safe='')}"
    return uri
