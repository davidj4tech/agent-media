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


def test_the_player_gets_the_popups_listening_keys():
    for action in ("para-", "para+", "prev", "replay", "speed+", "speed0", "vol-", "mute"):
        assert action in canvas._APP_SPEECH_ACTIONS
    assert canvas.ctl_argv("speech", "prev", 3) == ["replay-prev", "--idx", "3"]
    assert canvas.ctl_argv("speech", "replay", 2) == ["replay", "2"]
    # A transcript line's own turn, by history id — not an index, not clamped.
    assert canvas.ctl_argv("speech", "replay-id", 48213) == ["replay", "--id", "48213"]


def test_speed_and_mute_ride_along(monkeypatch):
    _as(monkeypatch)
    ok, out = reply.speech_now("tok", {"speaking": True, "speed": 1.6, "muted": True})
    assert ok and out["speed"] == 1.6 and out["muted"] is True


def test_session_states_name_the_pane_class(monkeypatch, tmp_path):
    _as(monkeypatch)
    (tmp_path / f"{SID}.json").write_text(
        '{"session": "%s", "folder": "/srv/Conversations/agent-media/Filters"}' % SID)
    monkeypatch.setattr(reply, "_manifest_dir", lambda: tmp_path)
    monkeypatch.setattr(reply, "live_sessions", lambda: {SID: "%3"})
    monkeypatch.setattr(reply, "_capture_pane", lambda pane: "✻ Thinking… (esc to interrupt)")
    monkeypatch.setattr(reply, "_STATES_CACHE", (0.0, []))
    ok, out = reply.session_states("tok")
    assert ok
    assert out["sessions"] == [{"session": SID, "tail": "agent-media/Filters", "state": "working"}]


def test_session_states_are_swept_once_per_ttl(monkeypatch):
    _as(monkeypatch)
    sweeps = []
    monkeypatch.setattr(reply, "_live_states", lambda: sweeps.append(1) or [])
    monkeypatch.setattr(reply, "_STATES_CACHE", (0.0, []))
    for _ in range(3):
        reply.session_states("tok")
    assert len(sweeps) == 1


def test_session_states_refuse_a_stranger(monkeypatch):
    _as(monkeypatch, user=None, status=401)
    ok, _ = reply.session_states("")
    assert not ok


def test_a_share_from_the_app_is_gated_and_dispatched(monkeypatch):
    from agent_media_core import share as sharemod
    from agent_media_core.entrypoints import share_listener

    _as(monkeypatch, user=None, status=401)
    ok, out = reply.share_from_app("https://example.com/x", "", "")
    assert not ok and out.get("status") in (401, 403)

    _as(monkeypatch)
    verdict = sharemod.Verdict(channel="music", content_type="music", title="A song", reason="short")
    monkeypatch.setattr(sharemod, "share", lambda text, channel="", probe_timeout=0: ("https://example.com/x", verdict))
    played = []
    monkeypatch.setattr(share_listener, "_play", lambda url, v, where: played.append((url, v.channel)))
    ok, out = reply.share_from_app("look https://example.com/x", "banana", "tok")
    assert ok and out["channel"] == "music" and out["title"] == "A song"
    import time
    for _ in range(50):
        if played:
            break
        time.sleep(0.01)
    assert played == [("https://example.com/x", "music")]


def test_nothing_shared_is_said_so(monkeypatch):
    _as(monkeypatch)
    ok, out = reply.share_from_app("   ", "", "tok")
    assert not ok and out["error"] == "nothing shared"
