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

import asyncio
import json
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .._paths import state_dir

SUFFIX = ".tts"
OVERRIDE_NAME = "speech-voice.json"
MODES = ("phone", "server")

#: Google's Australian voices, which only the phone has (Android's
#: TextToSpeech), as the app's Settings offers them.
GOOGLE_VOICES = tuple(
    {"name": name, "label": f"Google {label}", "gender": "", "locale": "en-AU",
     "language": "English", "accent": "Australia", "where": ["phone"]}
    for name, label in (("en-au-x-aua-network", "A · online"),
                        ("en-au-x-aub-network", "B · online"),
                        ("en-au-x-auc-network", "C · online"),
                        ("en-au-x-aud-network", "D · online"),
                        ("en-au-x-aua-local", "A · offline")))

#: Microsoft's voices ("edge:<ShortName>") are its whole list, as edge_tts
#: fetches it, kept here for a day; the phone asks Microsoft itself (about
#: 0.7 s to first audio against ~2 s through red5, 27 Sep 2026) and red5
#: renders them with the edge engine. Not an official service, so a
#: phone-rendered clip also names a Google voice to fall back to (tts_uri).
EDGE_CACHE_NAME = "edge-voices.json"
EDGE_CACHE_TTL_S = 24 * 3600
#: How long a failed fetch is left before the next try (the stale list, or
#: the built-in one, serves meanwhile).
EDGE_RETRY_S = 600
EDGE_FETCH_TIMEOUT_S = 10.0

#: The built-in list, when neither Microsoft nor the cache can say: the
#: twelve offered before the whole list was (David, 27 Sep 2026: Australian,
#: British, the newest US ones, New Zealand and Irish).
_EDGE_BUILTIN = (
    ("en-AU-NatashaNeural", "Female", "Australia"),
    ("en-AU-WilliamMultilingualNeural", "Male", "Australia"),
    ("en-GB-SoniaNeural", "Female", "United Kingdom"),
    ("en-GB-RyanNeural", "Male", "United Kingdom"),
    ("en-US-AvaMultilingualNeural", "Female", "United States"),
    ("en-US-AndrewMultilingualNeural", "Male", "United States"),
    ("en-US-EmmaMultilingualNeural", "Female", "United States"),
    ("en-US-BrianMultilingualNeural", "Male", "United States"),
    ("en-NZ-MollyNeural", "Female", "New Zealand"),
    ("en-NZ-MitchellNeural", "Male", "New Zealand"),
    ("en-IE-EmilyNeural", "Female", "Ireland"),
    ("en-IE-ConnorNeural", "Male", "Ireland"),
)

_last_fetch_try = 0.0

#: The Google voice a Microsoft-voiced clip falls back to on the phone.
FALLBACK_VOICE = "en-au-x-aua-network"

#: Characters per second of Google's voices at 1.0, measured on p8a
#: (26 Sep 2026: 14.2-16.0 across three sentences). An estimate only — the
#: measured starts (clip_starts_s) take over as each sentence plays.
CHARS_PER_S = 15.0
LEAD_S = 0.2


def _edge_voice(raw: dict) -> Optional[dict]:
    """One of edge_tts's voices as the app is sent it, or None."""
    short = str(raw.get("ShortName") or "")
    locale = str(raw.get("Locale") or "")
    if not short or not locale:
        return None
    first = short.rsplit("-", 1)[-1]
    first = re.sub(r"(Multilingual)?Neural$", "", first) or first
    lang_code, _, region = locale.partition("-")
    # LocaleName, "English (Australia)", is one name per locale; FriendlyName
    # ("Microsoft Natasha Online (Natural) - English (Australia)") ends in
    # one too, but not always the same ("Hongkong", "(Preview)").
    named = str(raw.get("LocaleName") or "") or str(raw.get("FriendlyName") or "").rpartition(" - ")[2]
    m = re.match(r"^(.*?) \((.*?)\)", named)
    language, accent = (m.group(1), m.group(2)) if m else (lang_code, region or locale)
    return {"name": f"edge:{short}", "label": first, "gender": str(raw.get("Gender") or ""),
            "locale": locale, "language": language, "accent": accent,
            "where": ["phone", "server"]}


def _builtin_edge_voices() -> list[dict]:
    return [_edge_voice({"ShortName": short, "Gender": gender, "Locale": short[:5],
                         "LocaleName": f"English ({accent})"})
            for short, gender, accent in _EDGE_BUILTIN]


def _fetch_edge_voices() -> list[dict]:
    """Microsoft's list, raw, through edge_tts; raises when it cannot. On a
    thread of its own, so it runs whether or not the caller has a loop."""
    import edge_tts

    out: dict = {}

    def run() -> None:
        try:
            out["voices"] = asyncio.run(asyncio.wait_for(
                edge_tts.list_voices(), EDGE_FETCH_TIMEOUT_S))
        except BaseException as e:  # noqa: BLE001 — handed to the caller
            out["error"] = e

    t = threading.Thread(target=run, name="edge-voices", daemon=True)
    t.start()
    t.join(EDGE_FETCH_TIMEOUT_S + 2)
    if "voices" not in out:
        raise OSError(f"no voice list from Microsoft ({out.get('error', 'timed out')})")
    return out["voices"]


def edge_voices() -> list[dict]:
    """Microsoft's voices: the cached list while under a day old, else a
    fresh one (and cached), else the stale cache, else the built-in twelve."""
    global _last_fetch_try
    path = state_dir() / EDGE_CACHE_NAME
    try:
        cached = json.loads(path.read_text())
        stale = list(cached["voices"])
        fetched = float(cached.get("fetched") or 0)
    except (OSError, ValueError, KeyError, TypeError):
        stale, fetched = [], 0.0
    now = time.time()
    if stale and now - fetched < EDGE_CACHE_TTL_S:
        return stale
    if now - _last_fetch_try >= EDGE_RETRY_S:
        _last_fetch_try = now
        try:
            fresh = [v for v in map(_edge_voice, _fetch_edge_voices()) if v]
        except Exception:  # noqa: BLE001 — offline, or edge_tts moved
            fresh = []
        if fresh:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".tmp.{os.getpid()}")
                tmp.write_text(json.dumps({"fetched": now, "voices": fresh}))
                tmp.replace(path)
            except OSError:
                pass
            return fresh
    return stale or _builtin_edge_voices()


def voices() -> list[dict]:
    """Every voice on offer, grouped as `languages` orders them."""
    return [v for lang in languages() for accent in lang["accents"] for v in accent["voices"]]


def languages(all_voices: Optional[list] = None) -> list[dict]:
    """The voices by language, then accent (locale): English first, then by
    name; Australia first within English, the rest by name; Microsoft's
    voices, by name, before Google's in an accent.

    Google's aren't offered (David, 27 Sep 2026: "leave off the Google ones
    ... if they don't have names") — A to D mean nothing in a list of
    people. They stay the phone's fallback (FALLBACK_VOICE), and a stored
    choice of one still renders."""
    if all_voices is None:
        all_voices = edge_voices()
    by_locale: dict[str, list] = {}
    for v in all_voices:
        by_locale.setdefault(v["locale"], []).append(v)
    langs: dict[str, dict] = {}
    for locale, vs in by_locale.items():
        # Stable: Google's keep the order they are listed in.
        vs.sort(key=lambda v: ((0, v["label"].lower()) if v["name"].startswith("edge:")
                               else (1, "")))
        code = locale.split("-")[0]
        lang = langs.setdefault(code, {"code": code, "names": Counter(), "accents": []})
        lang["names"].update(v["language"] for v in vs)
        lang["accents"].append({"locale": locale,
                                "name": Counter(v["accent"] for v in vs).most_common(1)[0][0],
                                "voices": vs})
    out = []
    for lang in langs.values():
        name = lang.pop("names").most_common(1)[0][0]
        lang["accents"].sort(key=lambda a: (a["locale"] != "en-AU", a["name"].lower()))
        out.append({"code": lang["code"], "name": name, "accents": lang["accents"]})
    out.sort(key=lambda lang: (lang["code"] != "en", lang["name"].lower()))
    return out


def find_voice(name: str, all_voices: Optional[list] = None) -> Optional[dict]:
    """A voice by name: any on offer, or one of Google's — not offered, but a
    choice made before they were taken off the list is still a choice."""
    if all_voices is None:
        all_voices = voices() + [dict(v) for v in GOOGLE_VOICES]
    for v in all_voices:
        if v["name"] == name:
            return v
    return None


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
    ("server"), in `voice` when given (here, only a Microsoft one is
    honoured; see server_choice)."""
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


def server_choice(target_name: str) -> Optional[tuple[str, str]]:
    """(engine, voice) this host renders `target_name`'s replies in when the
    app chose the server and a Microsoft voice, else None (the env's)."""
    entry = override_for(target_name)
    voice = (entry or {}).get("voice") or ""
    if entry and entry["mode"] == "server" and voice.startswith("edge:"):
        return "edge", voice[len("edge:"):]
    return None


def server_voice(target_name: Optional[str] = None) -> str:
    """The voice this host renders in, for display: the app's choice for
    `target_name` (server_choice), else the per-engine voice, as
    intake/submit.py resolves it, else the engine's own default."""
    chosen = server_choice(target_name) if target_name else None
    if chosen:
        return chosen[1]
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
