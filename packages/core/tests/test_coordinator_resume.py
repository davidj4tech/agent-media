"""Tests for the remote (Android) resume settle delay in after_speech."""

import time as _time
import types

from agent_media_core.route import coordinator as coord_mod


def _capture_sleeps(monkeypatch, calls):
    """Record after_speech's settle delay without patching the *shared* time module.

    `coord_mod.time` IS the stdlib module object, so `setattr(coord_mod.time,
    "sleep", ...)` replaces time.sleep for every thread in the process — and
    this suite leaves worker threads running (the coordinator's own flag
    writer, sentence followers, sink retry loops). Whichever of them happened
    to be sleeping while these tests ran appended to `calls`, so the
    assertions below passed or failed on test *order*: green alone, red after
    the wrong neighbour. Rebinding the module NAME inside the coordinator
    keeps the patch where the assertion is. `time.time` is passed through
    because after_speech's caller uses it.
    """
    monkeypatch.setattr(coord_mod, "time", types.SimpleNamespace(
        sleep=lambda s: calls.append(("sleep", s)),
        time=_time.time, monotonic=_time.monotonic))


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
    _capture_sleeps(monkeypatch, calls)
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
    _capture_sleeps(monkeypatch, calls)
    monkeypatch.setattr(coord_mod._android, "resume",
                        lambda h: calls.append(("resume", h)))

    c = _coord()  # _android_paused defaults to []
    c.after_speech()

    assert calls == []  # no settle delay when nothing to resume
