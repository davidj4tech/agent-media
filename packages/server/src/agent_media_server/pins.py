"""Keep-open pins: sessions the idle reaper must never close.

`POST /session/pin {"session", "pinned": bool}` sets one; `/targets` and
`/conversations` rows carry `pinned`. A pin is about the reaper only — it does
not stop you closing the session yourself, and it survives the session ending
(pin a thread, close it, resume it next week: still pinned).

Stored in `<state_dir>/pinned.json` as `{"<session>": <pinned at, epoch>}`
(`_jsonmap.JsonMap`).
"""

from __future__ import annotations

import time

from . import auth, sessions
from ._jsonmap import JsonMap

PINS = JsonMap("pinned.json")


def pinned() -> dict[str, float]:
    return {k: v for k, v in PINS.all().items() if isinstance(v, (int, float))}


def is_pinned(session: str) -> bool:
    return session in pinned()


def set_pinned(session: str, flag: bool) -> bool:
    """Pin or unpin. True when that changed anything."""
    if flag:
        def fn(rows: dict) -> bool:
            if session in rows:
                return False
            rows[session] = round(time.time(), 3)
            return True
        return PINS.update(fn)
    return PINS.drop(session)


def session_pin(session: str, flag, bearer: str) -> tuple[bool, dict]:
    """`POST /session/pin {"session", "pinned"}`. Gated like `/session/archive`,
    and refused the same ways: 400 for a bad id or a non-boolean, 404 for a
    session nothing knows. `pinned` defaults to true."""
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    if flag is None:
        flag = True
    if not isinstance(flag, bool):
        return False, {"error": "pinned must be true or false", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if not (sessions.live_sessions().get(session) or sessions.session_exists(session)
            or sessions._folder_for_session(session)):
        return False, {"error": f"no such session {session[:8]}", "status": 404}
    try:
        set_pinned(session, flag)
    except OSError as e:
        return False, {"error": f"could not save the pin ({e})", "status": 500}
    return True, {"session": session, "pinned": flag}


def session_priority(session: str, flag, bearer: str, level=None) -> tuple[bool, dict]:
    """`POST /session/priority {"session", "level"}`: the thread's speech
    level — `interrupt`, `auto`, `normal` or `quiet`
    (`agent_media_core.speak_priority`), or `default` to clear its own so it
    follows the default. The older `{"priority": bool}` is auto / normal and
    still accepted. Refused exactly as `/session/pin` is; with neither given,
    auto. Answers the level it now has, and `own`: whether that is its own."""
    from agent_media_core import speak_priority

    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    if level is None:
        if flag is None:
            flag = True
        if not isinstance(flag, bool):
            return False, {"error": "priority must be true or false", "status": 400}
        level = "auto" if flag else "normal"
    if level not in speak_priority.LEVELS and level != "default":
        return False, {"error": "level must be interrupt, auto, normal, quiet or default",
                       "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if not (sessions.live_sessions().get(session) or sessions.session_exists(session)
            or sessions._folder_for_session(session)):
        return False, {"error": f"no such session {session[:8]}", "status": 404}
    try:
        if level == "default":
            speak_priority.clear_level(session)
        else:
            speak_priority.set_level(session, level)
    except OSError as e:
        return False, {"error": f"could not save the level ({e})", "status": 500}
    level = speak_priority.level_of(session)
    return True, {"session": session, "level": level,
                  "own": session in speak_priority.levels(),
                  "priority": level in speak_priority.SPEAKS}


def speech_default(bearer: str, level=None) -> tuple[bool, dict]:
    """`GET /speech/default` and `POST /speech/default {"level"}`: the speech
    level of every thread with none of its own (`speak_priority.default_level`).
    The server's, so every device's. With `level` None, only read."""
    from agent_media_core import speak_priority

    if level is not None and level not in speak_priority.LEVELS:
        return False, {"error": "level must be interrupt, auto, normal or quiet",
                       "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if level is not None:
        try:
            speak_priority.set_default(level)
        except OSError as e:
            return False, {"error": f"could not save the default ({e})", "status": 500}
    return True, {"level": speak_priority.default_level()}


def speech_voice(bearer: str, mode=None, voice=None) -> tuple[bool, dict]:
    """`GET /speech/voice` and `POST /speech/voice {"mode", "voice"?}`: whether
    the current speech target's replies are rendered on the phone, by
    Android's TextToSpeech or Microsoft, or here (render/device.py), and in
    which voice. The server's, so every device's. With `mode` None, only
    read."""
    from agent_media_core import audio_targets
    from agent_media_core.render import device

    if mode is not None and mode not in device.MODES:
        return False, {"error": "mode must be phone or server", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    languages = device.languages()
    voices = [v for lang in languages for a in lang["accents"] for v in a["voices"]]
    chosen = device.find_voice(voice, voices) if voice is not None else None
    if voice is not None and not chosen:
        return False, {"error": "no such voice", "status": 400}
    if chosen and mode is not None and mode not in chosen["where"]:
        return False, {"error": f"{chosen['label']} is only on the phone: "
                                "choose a Microsoft voice for the server", "status": 400}
    target = audio_targets.speech_default()
    if mode is not None:
        if not device.can_render(target):
            return False, {"error": f"{target} cannot render words", "status": 409}
        try:
            device.set_override(target, mode, voice or device.voice_for(target))
        except OSError as e:
            return False, {"error": f"could not save the voice ({e})", "status": 500}
    return True, {"target": target,
                  "mode": "phone" if device.renders_on_device(target) else "server",
                  "voice": device.voice_for(target),
                  "can_phone": device.can_render(target),
                  "voices": voices,
                  "languages": languages,
                  "server_voice": device.server_voice(target)}


def settings_language(bearer: str, language=None) -> tuple[bool, dict]:
    """`GET /settings/language` and `POST /settings/language {"language"}`: the
    app's language, site-wide (language.py): one the voices come in, or
    one known by name. With `language` None, only read."""
    from agent_media_core import language as site
    from agent_media_core.render import device

    user, err = auth.gate(bearer)
    if not user:
        return False, err
    # Those the voices come in, and those known by name (offline, the
    # built-in voices are English only), known names first.
    names = {lang["code"]: lang["name"] for lang in device.languages()}
    names.update(site.NAMES)
    languages = [{"code": code, "name": name} for code, name in
                 sorted(names.items(), key=lambda kv: (kv[0] != "en", kv[1].lower()))]
    # Each language with each country it has voices for — what Settings
    # picks, so Voice shows one country's voices, not a language's dozen.
    locales = [{"code": acc["locale"], "language": lang["code"],
                "name": f'{lang["name"]} ({acc["name"]})'}
               for lang in device.languages() for acc in lang["accents"]]
    if language is not None:
        want = site.normalise(str(language))
        known = {loc["code"] for loc in locales} | {lang["code"] for lang in languages}
        if want not in known:
            return False, {"error": "no such language", "status": 400}
        try:
            site.set_language(want)
        except OSError as e:
            return False, {"error": f"could not save the language ({e})", "status": 500}
    return True, {"language": site.current(), "locale": site.locale(),
                  "languages": languages, "locales": locales}
