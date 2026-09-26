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

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .._paths import state_dir

SUFFIX = ".tts"
OVERRIDE_NAME = "speech-voice.json"
MODES = ("phone", "server")

#: Google's Australian voices, as the app's Settings offers them.
VOICES = (
    {"name": "en-au-x-aua-network", "label": "A · online"},
    {"name": "en-au-x-aub-network", "label": "B · online"},
    {"name": "en-au-x-auc-network", "label": "C · online"},
    {"name": "en-au-x-aud-network", "label": "D · online"},
    {"name": "en-au-x-aua-local", "label": "A · offline"},
    # Microsoft's voice, the one red5 renders with, asked for by the phone
    # itself from Australia (about 0.7 s to first audio against ~2 s through
    # red5, 27 Sep 2026). Not an official service, so every clip also names
    # a Google voice for the phone to fall back to (see tts_uri).
    {"name": "edge:en-AU-NatashaNeural", "label": "Natasha · Microsoft"},
)

#: The Google voice a Microsoft-voiced clip falls back to on the phone.
FALLBACK_VOICE = "en-au-x-aua-network"

#: Characters per second of Google's voices at 1.0, measured on p8a
#: (26 Sep 2026: 14.2-16.0 across three sentences). An estimate only — the
#: measured starts (clip_starts_s) take over as each sentence plays.
CHARS_PER_S = 15.0
LEAD_S = 0.2


def _key(prefix: str, target_name: str) -> str:
    return f"{prefix}_{target_name.upper().replace('-', '_')}"


def _override_path() -> Path:
    return state_dir() / OVERRIDE_NAME


def overrides() -> dict:
    try:
        data = json.loads(_override_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def override_for(target_name: str) -> Optional[dict]:
    """The app's choice for `target_name`, or None to follow the env."""
    entry = overrides().get(target_name)
    if isinstance(entry, dict) and entry.get("mode") in MODES:
        return entry
    return None


def set_override(target_name: str, mode: str, voice: Optional[str] = None) -> None:
    """Render `target_name`'s replies on the device ("phone") or here
    ("server"), in `voice` on the device when given."""
    if mode not in MODES:
        raise ValueError(f"not a voice mode: {mode!r}")
    data = overrides()
    data[target_name] = {"mode": mode, "voice": voice or None}
    path = _override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=0, sort_keys=True))
    tmp.replace(path)


def env_renders_on_device(target_name: str) -> bool:
    return os.environ.get(_key("MEDIA_SPEECH_RENDER", target_name), "").strip().lower() == "device"


def renders_on_device(target_name: str) -> bool:
    entry = override_for(target_name)
    if entry:
        return entry["mode"] == "phone"
    return env_renders_on_device(target_name)


def voice_for(target_name: str) -> Optional[str]:
    entry = override_for(target_name)
    if entry and entry.get("voice"):
        return entry["voice"]
    return os.environ.get(_key("MEDIA_SPEECH_DEVICE_VOICE", target_name)) or None


def can_render(target_name: str) -> bool:
    """Whether `target_name`'s player can be handed words: Sasonica's can,
    and any target configured or switched to do so."""
    return (target_name == "sasonica"
            or bool(os.environ.get(_key("MEDIA_SPEECH_RENDER", target_name)))
            or override_for(target_name) is not None)


def server_voice() -> str:
    """The voice this host renders in, for display (the per-engine voice,
    as intake/submit.py resolves it, else the engine's own default)."""
    engine = (os.environ.get("MEDIA_RENDER_ENGINE")
              or os.environ.get("CLAUDE_TTS_ENGINE") or "edge")
    return (os.environ.get(f"MEDIA_RENDER_VOICE_{engine.upper().replace('-', '_')}")
            or os.environ.get("MEDIA_RENDER_VOICE")
            or "server default")


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
        if voice.startswith("edge:"):
            fallback = os.environ.get("MEDIA_SPEECH_DEVICE_FALLBACK_VOICE") or FALLBACK_VOICE
            uri += f"&fallback={quote(fallback, safe='')}"
    return uri
