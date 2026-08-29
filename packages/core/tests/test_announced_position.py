"""A position is only true at the moment it was read.

The phone lane mirrors the remote player's position into now_playing so a
status redraw is a local file read rather than a ~600ms bridge round trip. The
poll behind that mirror lands every one to three seconds, so taking the stored
reading as the current one makes the bar stall for the length of the gap and
then jump — measured against the player, 3.3s held for three seconds while the
audio ran on to 6.1s.
"""

from __future__ import annotations

import os

import pytest

from agent_media_core import cli


TOTAL = 11.47


def _row(monkeypatch, **extras):
    ex = {"total_duration_s": TOTAL, "writer_pid": os.getpid()}
    ex.update(extras)
    monkeypatch.setattr(cli, "_now_speaking", lambda: {"extras": ex})
    return ex


def test_the_reading_is_carried_on_by_the_clock(monkeypatch):
    import time
    _row(monkeypatch, live_pos_s=3.3, live_pos_at=time.time() - 2.0)
    idle, pos, dur, *_ = cli._announced_timeline()
    assert not idle
    assert pos == pytest.approx(5.3, abs=0.2), "the bar stalled between polls"
    assert dur == TOTAL


def test_a_pause_freezes_it(monkeypatch):
    import time
    _row(monkeypatch, live_pos_s=3.3, live_pos_at=time.time() - 2.0,
         live_pause=True)
    _idle, pos, *_ = cli._announced_timeline()
    assert pos == pytest.approx(3.3), (
        "a paused reply must not read on through the silence")


def test_speed_is_respected(monkeypatch):
    import time
    _row(monkeypatch, live_pos_s=2.0, live_pos_at=time.time() - 2.0,
         live_speed=1.5)
    _idle, pos, *_ = cli._announced_timeline()
    assert pos == pytest.approx(5.0, abs=0.2)


def test_it_never_runs_past_the_end(monkeypatch):
    import time
    _row(monkeypatch, live_pos_s=10.0, live_pos_at=time.time() - 600)
    _idle, pos, *_ = cli._announced_timeline()
    assert pos == TOTAL, "a bar past 100% reads as a fault"


def test_a_reading_with_no_moment_is_taken_as_it_stands(monkeypatch):
    """Rows written before the stamp existed, and any writer that does not
    record one: the old behaviour is still the safe one."""
    _row(monkeypatch, live_pos_s=3.3)
    _idle, pos, *_ = cli._announced_timeline()
    assert pos == 3.3


def test_a_pause_and_resume_does_not_lose_the_place(monkeypatch, tmp_path):
    """The held time is taken off the reading's age, exactly as it is taken off
    play_started_at — otherwise a pause silently advances the bar."""
    import time
    from agent_media_core.intake.submit import stamp_speech_pause

    class _State:
        def __init__(self, ex):
            self.ex = ex
            self.written = None

        def get_now_playing(self, _sink):
            return {"uri": "u", "started_at": 0.0, "target": "app",
                    "extras": self.ex}

        def set_now_playing(self, _sink, **kw):
            self.written = kw["extras"]

    # The reading was taken six seconds ago; one second later the reply was
    # paused, and it has stood paused for the five seconds since. So one
    # second of it has been spoken since the reading, not six.
    now = time.time()
    ex = {"total_duration_s": TOTAL, "live_pos_s": 3.3,
          "live_pos_at": now - 6.0}
    st = _State(ex)
    stamp_speech_pause(st, True)                    # pause
    st.ex = st.written
    st.ex["paused_at"] = now - 5.0
    stamp_speech_pause(st, False)                   # resume, five seconds on

    monkeypatch.setattr(cli, "_now_speaking",
                        lambda: {"extras": dict(st.written,
                                                writer_pid=os.getpid())})
    _idle, pos, *_ = cli._announced_timeline()
    assert pos == pytest.approx(4.3, abs=0.3), (
        "the five seconds paused were counted as five seconds spoken")
