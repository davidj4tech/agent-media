"""The Speech card's per-conversation voice, using the normal voice catalog."""
from agent_media_core import audio_targets, session_voice
from agent_media_core.render import device

from . import auth, sessions


def setting(session: str, bearer: str, *, body: dict | None = None) -> tuple[bool, dict]:
    session = session.strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    from . import matrix

    if not (sessions.live_sessions().get(session) or sessions.session_exists(session)
            or sessions._folder_for_session(session) or matrix.room(session) is not None):
        return False, {"error": "no such session", "status": 404}
    # Microsoft voices work on either side, so a thread keeps its voice
    # when the listener moves between phone and server-rendered playback.
    languages = device.languages()
    voices = [v for lang in languages for accent in lang["accents"] for v in accent["voices"]
              if v["name"].startswith("edge:")]
    if body is not None:
        if "voice" not in body:
            return False, {"error": "voice is required (null uses the default)", "status": 400}
        voice = body["voice"]
        if voice is not None and (not isinstance(voice, str) or
                                  not any(v["name"] == voice for v in voices)):
            return False, {"error": "choose one of the offered voices", "status": 400}
        try:
            session_voice.set_voice(session, voice)
        except OSError:
            return False, {"error": "could not save the voice", "status": 500}
    target = audio_targets.speech_default()
    phone = device.renders_on_device(target)
    default = device.voice_for(target) if phone else device.server_voice(target)
    if not phone and device.server_choice(target):
        default = "edge:" + default
    own = session_voice.voice_for(session)
    return True, {"session": session, "voice": own, "default_voice": default,
                  "effective_voice": own or default, "voices": voices, "languages": languages}
