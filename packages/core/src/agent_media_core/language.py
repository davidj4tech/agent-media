"""The app's language, site-wide (David, 27 Sep 2026).

One choice for every paired device, kept here like the voice: Settings'
Voice section offers that language's voices, and the reply hook reads it
(intake/heard.py). A lower-case code, "en" unless set.
"""

from __future__ import annotations

import json
import os

from ._paths import state_dir

FILE_NAME = "language.json"
DEFAULT = "en"

#: The ones we know by name; the voice list names the rest.
NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "zh": "Chinese (Simplified)",
    "de": "German", "it": "Italian", "pt": "Portuguese", "ja": "Japanese",
    "ko": "Korean", "nl": "Dutch", "sv": "Swedish", "ru": "Russian", "ar": "Arabic",
    "hi": "Hindi",
}


def current() -> str:
    """The chosen language's code, read fresh; "en" when unset or unreadable."""
    try:
        data = json.loads((state_dir() / FILE_NAME).read_text())
        code = data.get("language") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — never raises
        return DEFAULT
    return code.strip().lower() if isinstance(code, str) and code.strip() else DEFAULT


def set_language(code: str) -> None:
    path = state_dir() / FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps({"language": code.strip().lower()}))
    tmp.replace(path)
