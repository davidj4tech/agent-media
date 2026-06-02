"""Tests for the remote (Android) resume settle delay in after_speech."""

from agent_media_core.route import coordinator as coord_mod


class _FakeState:
    """Minimal StateStore stand-in: after_speech only needs get_now_playing."""

    def get_now_playing(self, sink):
        return None


def _coord():
    # Pass truthy fakes so __init__ skips the real SinkMusic / StateStore.
    return coord_mod.Coordinator(music=object(), state=_FakeState())


def test_settle_seconds_default(monkeypatch):
    monkeypatch.delenv("MEDIA_REMOTE_RESUME_SETTLE_MS", raising=False)
    monkeypatch.delenv("MEDIA_SNAPCAST_LATENCY_MS", raising=False)
    # default snapcast latency 500 + 400 margin = 900ms
    assert coord_mod._remote_resume_settle_s() == 0.9


def test_settle_seconds_explicit_override(monkeypatch):
    monkeypatch.setenv("MEDIA_REMOTE_RESUME_SETTLE_MS", "0")
    assert coord_mod._remote_resume_settle_s() == 0.0


def test_settle_seconds_tracks_snapcast_latency(monkeypatch):
    monkeypatch.delenv("MEDIA_REMOTE_RESUME_SETTLE_MS", raising=False)
    monkeypatch.setenv("MEDIA_SNAPCAST_LATENCY_MS", "1000")
    assert coord_mod._remote_resume_settle_s() == 1.4


def test_after_speech_settles_then_resumes_android(monkeypatch):
    calls = []
    monkeypatch.setattr(coord_mod.time, "sleep",
                        lambda s: calls.append(("sleep", s)))
    monkeypatch.setattr(coord_mod._android, "resume",
                        lambda h: calls.append(("resume", h)))
    monkeypatch.setattr(coord_mod, "_remote_resume_settle_s", lambda: 0.9)

    c = _coord()
    c._android_paused = ["p8ar"]
    c.after_speech()

    # Settle must come before the resume, and the host list is cleared.
    assert calls == [("sleep", 0.9), ("resume", "p8ar")]
    assert c._android_paused == []


def test_after_speech_no_android_no_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(coord_mod.time, "sleep",
                        lambda s: calls.append(("sleep", s)))
    monkeypatch.setattr(coord_mod._android, "resume",
                        lambda h: calls.append(("resume", h)))

    c = _coord()  # _android_paused defaults to []
    c.after_speech()

    assert calls == []  # no settle delay when nothing to resume
