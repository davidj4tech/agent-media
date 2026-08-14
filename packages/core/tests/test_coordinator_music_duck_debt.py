"""The music duck is a debt, recorded where nothing else can clear it.

Written from two failures observed on the phone on 2026-08-14, an hour apart:

  * a duck whose restore never ran left the music at 10 for two hours, because
    the duck was recorded only inside the shared now-playing row and the row was
    gone by the time after_speech looked for it; and
  * a restore that *did* run put the volume back to 45 — the policy's
    Mopidy-era default on a 0-100 dial — on a phone whose mpv runs at 130.

Both read to the listener as "music got quieter after Sam spoke and never came
back", which is also what c2db694 fixed a different cause of.
"""

import pytest

from agent_media_core.route import coordinator as coord_mod
from agent_media_core.state import StateStore


@pytest.fixture
def state(tmp_path):
    return StateStore(tmp_path / "state.db")


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    for var in ("MEDIA_DUCK_ROOMS_STREAM", "MEDIA_DUCK_VOLUME",
                "AAR_MOPIDY_DUCK_VOLUME", "MEDIA_MUSIC_VOLUME"):
        monkeypatch.delenv(var, raising=False)


class FakeMusic:
    """A music sink on the phone's dial: normal is 130, not 100."""

    NOMINAL = 130

    def __init__(self, volume=NOMINAL, uri="yt:https://youtu.be/x"):
        self.volume = volume
        self.uri = uri
        self.calls = []

    def now_playing_uri(self, target):
        return self.uri

    def current_volume(self, target):
        return self.volume

    def nominal_volume(self, target):
        return self.NOMINAL

    def duck(self, target, level):
        self.calls.append(("duck", level))
        self.volume = level

    def unduck(self, target, restore=100):
        self.calls.append(("unduck", restore))
        self.volume = restore


def _coord(state, music):
    c = coord_mod.Coordinator(music=music, state=state)
    # before_speech's other limbs are out of scope here: no book, no MPRIS, no
    # Android pause. Each is covered by its own test.
    c._probe_book_active = lambda: False
    return c


def test_duck_then_restore_returns_the_listener_s_volume(state):
    music = FakeMusic(volume=130)
    c = _coord(state, music)

    c.before_speech()
    assert music.volume == 10
    assert state.get_music_duck() == {"level": 10, "baseline": 130, "target": "local"}

    c.after_speech()
    assert music.volume == 130
    assert state.get_music_duck() is None


def test_a_lost_now_playing_row_no_longer_strands_the_duck(state):
    """The two-hour bug: something else cleared the shared row between the duck
    and the restore, and after_speech returned without paying the debt."""
    music = FakeMusic(volume=130)
    c = _coord(state, music)

    c.before_speech()
    state.clear_now_playing("music")          # the row is not ours alone

    c.after_speech()
    assert music.volume == 130, "the debt outlives the row it used to live in"
    assert state.get_music_duck() is None


def test_a_second_duck_keeps_the_original_baseline(state):
    """A re-duck with no intervening restore must not capture the ducked volume
    as the thing to restore — the same guard _rooms_duck has always had."""
    music = FakeMusic(volume=130)
    c = _coord(state, music)

    c.before_speech()
    c.before_speech()                          # mid-response, or after a strand
    assert state.get_music_duck()["baseline"] == 130

    c.after_speech()
    assert music.volume == 130


def test_the_restore_is_not_paid_twice(state):
    music = FakeMusic(volume=130)
    c = _coord(state, music)

    c.before_speech()
    c.after_speech()
    assert [call for call in music.calls if call[0] == "unduck"] == [("unduck", 130)]


def test_unreadable_volume_falls_back_to_the_backend_s_normal(state):
    """Not to the policy's 45: that is a Mopidy number on a different dial, and
    using it on the phone is an audible drop the listener must undo by hand."""
    music = FakeMusic(volume=130)
    music.current_volume = lambda target: None
    c = _coord(state, music)

    c.before_speech()
    assert state.get_music_duck()["baseline"] == 130
    c.after_speech()
    assert music.volume == 130


def test_a_volume_already_at_the_duck_level_is_not_the_baseline(state):
    """A stranded duck from an older build (or another ducker) must not become
    the value we restore to, or the music freezes quiet."""
    music = FakeMusic(volume=10)
    c = _coord(state, music)

    c.before_speech()
    assert state.get_music_duck()["baseline"] == 130
    c.after_speech()
    assert music.volume == 130


def test_no_backend_normal_keeps_the_policy_default(state):
    """Mopidy has no nominal_volume: the 0-100 dial keeps its old behaviour."""
    class Mopidyish(FakeMusic):
        nominal_volume = None                  # the attribute simply isn't there

    music = Mopidyish(volume=10)
    c = _coord(state, music)

    c.before_speech()
    assert state.get_music_duck()["baseline"] == 45
