"""Conversation voice API: fake catalog, no audio or device calls."""
import io
import json
import os

import pytest

from agent_media_core import audio_targets, session_voice as store
from agent_media_core.render import device
from agent_media_server import app, auth, session_voice as V, sessions

SID = "11111111-2222-3333-4444-555555555555"
MOLLY = "edge:en-NZ-MollyNeural"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(key, raising=False)
    for key in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setattr(auth, "gate", lambda b: ({"id": "u"}, {}))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    monkeypatch.setattr(device, "edge_voices", device._builtin_edge_voices)
    monkeypatch.setattr(audio_targets, "speech_default", lambda: "sasonica")
    device.set_override("sasonica", "phone", "edge:en-AU-NatashaNeural")


def test_read_set_and_clear_with_current_default():
    ok, data = V.setting(SID, "t")
    assert ok and data["voice"] is None and data["default_voice"] == "edge:en-AU-NatashaNeural"
    ok, data = V.setting(SID, "t", body={"voice": MOLLY})
    assert ok and data["voice"] == data["effective_voice"] == MOLLY
    assert store.voice_for(SID) == MOLLY
    assert device.voice_for("sasonica") == "edge:en-AU-NatashaNeural"
    device.set_override("sasonica", "server", "edge:en-GB-RyanNeural")
    ok, data = V.setting(SID, "t", body={"voice": None})
    assert ok and data["voice"] is None and data["effective_voice"] == "edge:en-GB-RyanNeural"


@pytest.mark.parametrize("body", [{}, {"voice": ""}, {"voice": 1}, {"voice": []},
                                 {"voice": "made-up"}, {"voice": "en-au-x-aua-network"}])
def test_only_catalog_voices_or_null_are_accepted(body):
    assert V.setting(SID, "t", body=body)[1]["status"] == 400
    assert store.voice_for(SID) is None


def test_auth_session_and_write_failure(monkeypatch):
    assert V.setting("bad", "t")[1]["status"] == 400
    monkeypatch.setattr(auth, "gate", lambda b: (None, {"status": 403}))
    assert V.setting(SID, "t")[1]["status"] == 403
    monkeypatch.setattr(auth, "gate", lambda b: ({"id": "u"}, {}))
    def fail(*a): raise OSError("cannot write")
    monkeypatch.setattr(store, "set_voice", fail)
    assert V.setting(SID, "t", body={"voice": MOLLY})[1]["status"] == 500
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    monkeypatch.setattr(sessions, "session_exists", lambda s: False)
    monkeypatch.setattr(sessions, "_folder_for_session", lambda s: "")
    from agent_media_server import matrix
    monkeypatch.setattr(matrix, "room", lambda s: None)
    assert V.setting(SID, "t")[1]["status"] == 404


def test_http_routes_and_cors(monkeypatch):
    from types import SimpleNamespace
    replies = []
    monkeypatch.setattr(app, "_json", lambda h, status, body: replies.append((status, body)))
    raw = json.dumps({"session": SID, "voice": MOLLY}).encode()
    h = SimpleNamespace(path="/session/voice", rfile=io.BytesIO(raw),
                        headers={"Authorization": "Bearer t", "Content-Length": str(len(raw))})
    assert app._post(h, "/session/voice")
    assert replies[-1][0] == 200 and replies[-1][1]["voice"] == MOLLY
    h.path += f"?session={SID}"
    assert app._get(h, "/session/voice")
    assert replies[-1][1]["voice"] == MOLLY
    assert "/session/voice" in app.CORS_PATHS
