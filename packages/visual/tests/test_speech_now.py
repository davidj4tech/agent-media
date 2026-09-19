"""/speech/now and /speech/ctl: the app's speech bar, named and gated."""

from __future__ import annotations

from agent_media_visual import canvas, reply

SID = "6c73498c-02c1-4846-8350-a82006973571"
ROOT = {"username": "david", "type": "root"}


def _as(monkeypatch, user=ROOT, status=200):
    monkeypatch.setattr(reply, "abs_identity", lambda bearer: (user, status))
    reply._NOW_CACHE.clear()


def test_live_speech_is_named(monkeypatch):
    _as(monkeypatch)
    monkeypatch.setattr(reply, "sessions_index",
                        lambda: [{"session": SID, "title": "Sasonica music", "live": True}])
    monkeypatch.setattr(reply, "item_for_session", lambda s, b: ("li_42", True))
    ok, out = reply.speech_now("tok", {"speaking": True, "session": SID,
                                       "sentence": "Hello.", "pos": 3, "dur": 9})
    assert ok
    assert out["live"] and out["speaking"] and not out["paused"]
    assert (out["title"], out["item"], out["sentence"]) == ("Sasonica music", "li_42", "Hello.")


def test_paused_speech_is_still_live(monkeypatch):
    _as(monkeypatch)
    monkeypatch.setattr(reply, "sessions_index", lambda: [])
    monkeypatch.setattr(reply, "item_for_session", lambda s, b: (None, False))
    ok, out = reply.speech_now("tok", {"speaking": False, "paused": True, "session": SID})
    assert ok and out["live"] and out["paused"] and out["session"] == SID


def test_quiet_names_nothing(monkeypatch):
    _as(monkeypatch)
    called = []
    monkeypatch.setattr(reply, "item_for_session", lambda s, b: called.append(s))
    ok, out = reply.speech_now("tok", {"speaking": False, "session": SID})
    assert ok and not out["live"] and out["session"] is None and not called


def test_an_unready_item_is_not_offered(monkeypatch):
    _as(monkeypatch)
    monkeypatch.setattr(reply, "sessions_index", lambda: [])
    monkeypatch.setattr(reply, "item_for_session", lambda s, b: ("li_42", False))
    _, out = reply.speech_now("tok", {"speaking": True, "session": SID})
    assert out["item"] is None


def test_the_title_is_asked_once_per_reply(monkeypatch):
    _as(monkeypatch)
    asks = []
    monkeypatch.setattr(reply, "sessions_index", lambda: asks.append(1) or [])
    monkeypatch.setattr(reply, "item_for_session", lambda s, b: (None, False))
    for _ in range(3):
        reply.speech_now("tok", {"speaking": True, "session": SID})
    assert len(asks) == 1


def test_a_stranger_is_refused(monkeypatch):
    _as(monkeypatch, user=None, status=401)
    ok, out = reply.speech_now("", {"speaking": True, "session": SID})
    assert not ok and out.get("status") in (401, 403)
    assert not reply.may_control_speech("")[0]


def test_the_bar_gets_only_listener_verbs():
    for action in canvas._APP_SPEECH_ACTIONS:
        assert canvas.ctl_argv("speech", action, 1) is not None
    assert "mute-keep" not in canvas._APP_SPEECH_ACTIONS
    assert "goto" not in canvas._APP_SPEECH_ACTIONS
