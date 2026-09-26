"""The app's language, site-wide (David, 27 Sep 2026).

One choice for every paired device, kept here like the voice: Settings'
Voice section offers that language's voices, and the reply hook reads it
(intake/heard.py). A lower-case code, "en" unless set.

With its country (David, 27 Sep 2026: English alone has voices from about
fourteen countries, and a list of all of them was "really busy"): the
locale, "en-AU", is what Settings picks and what Voice shows the voices of;
the language, "en", is what replies are asked for in.
"""

from __future__ import annotations

import json
import os

from ._paths import state_dir

FILE_NAME = "language.json"
DEFAULT = "en"
#: The country a language means when none was chosen with it.
DEFAULT_LOCALES = {"en": "en-AU", "es": "es-ES", "fr": "fr-FR", "zh": "zh-CN",
                   "de": "de-DE", "it": "it-IT", "sv": "sv-SE", "ja": "ja-JP",
                   "pt": "pt-BR", "ko": "ko-KR", "nl": "nl-NL", "ru": "ru-RU",
                   "ar": "ar-SA", "hi": "hi-IN"}

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


def locale() -> str:
    """The chosen language with its country ("en-AU"), read fresh; the
    language's usual country when only a language was chosen."""
    try:
        data = json.loads((state_dir() / FILE_NAME).read_text())
        loc = data.get("locale") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — never raises
        loc = None
    if isinstance(loc, str) and loc.strip():
        return normalise(loc)
    lang = current()
    return DEFAULT_LOCALES.get(lang, lang)


def normalise(code: str) -> str:
    """ "EN_au" -> "en-AU"; a bare language stays lower case."""
    parts = code.strip().replace("_", "-").split("-")
    return "-".join([parts[0].lower()] + [p.upper() if len(p) == 2 else p for p in parts[1:]])


def set_language(code: str) -> None:
    """Choose a language ("fr") or a language with its country ("fr-CA")."""
    loc = normalise(code)
    lang = loc.split("-", 1)[0]
    if "-" not in loc:
        loc = DEFAULT_LOCALES.get(lang, lang)
    path = state_dir() / FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps({"language": lang, "locale": loc}))
    tmp.replace(path)
