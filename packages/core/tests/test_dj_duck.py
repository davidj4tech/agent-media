"""Ducking like a DJ (route/dj.py): fades, and a depth that fits the reply.

David, 20 Oct 2026: "instead of just plain ducking when speech is being read
… it works more like a DJ would and adjusts the music to match the
situation". Off unless MEDIA_DJ_DUCK is set; every test here uses fake sinks
and a fake snapserver, so nothing reaches a real player.
"""

import pytest

from agent_media_core.route import coordinator as coord_mod
from agent_media_core.route import dj
from agent_media_core.route.policy import InterruptionStrategy as S
from agent_media_core.state import StateStore
from agent_media_core.types import ContentType as CT


@pytest.fixture
def state(tmp_path):
    return StateStore(tmp_path / "state.db")


@pytest.fixture(autouse=True)
def _dj_on(monkeypatch):
    for var in ("MEDIA_DUCK_ROOMS_STREAM", "MEDIA_DUCK_VOLUME",
                "AAR_MOPIDY_DUCK_VOLUME", "MEDIA_MUSIC_VOLUME",
                "MEDIA_DJ_LONG_S", "MEDIA_DJ_SHORT_S", "MEDIA_DJ_DIP_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEDIA_DJ_DUCK", "1")
    # Short fades: the shape is under test, not the length.
    monkeypatch.setenv("MEDIA_DJ_FADE_DOWN_MS", "80")
    monkeypatch.setenv("MEDIA_DJ_FADE_UP_MS", "120")
    monkeypatch.setenv("MEDIA_DJ_BEAT_MS", "0")


class FakeMusic:
    fades = True

    def __init__(self, volume=130, uri="yt:https://youtu.be/x"):
        self.volume = volume
        self.uri = uri
        self.vols = []
        self.paused = False
        self.events = []

    def now_playing_uri(self, target):
        return self.uri

    def current_volume(self, target):
        return self.volume

    def nominal_volume(self, target):
        return 130

    def position(self, target):
        return 61_000

    def duck(self, target, level):
        self.volume = level
        self.vols.append(level)

    def unduck(self, target, restore=100):
        self.volume = restore
        self.vols.append(restore)

    def pause(self, target):
        self.paused = True
        self.events.append(("pause", self.volume))

    def resume(self, target):
        self.paused = False
        self.events.append(("resume", self.volume))


def _coord(state, music):
    c = coord_mod.Coordinator(music=music, state=state)
    c._probe_book_active = lambda: False
    return c


# ---- the cue ------------------------------------------------------------

@pytest.mark.parametrize("ct,kw,strategy,level,reason", [
    (CT.MUSIC, {"est_s": 10}, S.DUCK, 10, "talk"),
    (CT.MUSIC, {}, S.DUCK, 10, "talk"),                 # unknown length
    (CT.MUSIC, {"est_s": 2}, S.DUCK, 25, "short"),
    (CT.MUSIC, {"est_s": 90}, S.PAUSE, 0, "long"),
    (CT.MUSIC, {"est_s": 10, "ask": True}, S.PAUSE, 0, "question"),
    (CT.DJ_SET, {"est_s": 90}, S.DUCK, 10, "talk"),     # never stop a mix
    (CT.DJ_SET, {"ask": True}, S.DUCK, 10, "talk"),
    (CT.AMBIENT, {"est_s": 2}, S.DUCK, 4, "ambient"),
    (CT.AUDIOBOOK, {"est_s": 2}, S.PAUSE, 0, "longform"),
    (CT.MUSIC, {"est_s": 2, "priority": "urgent"}, S.DUCK, 10, "urgent"),
])
def test_the_cue_fits_the_moment(ct, kw, strategy, level, reason):
    cue = dj.plan_cue(ct, **kw)
    assert (cue.strategy, cue.level, cue.reason) == (strategy, level, reason)


def test_urgent_comes_in_fast(monkeypatch):
    monkeypatch.setenv("MEDIA_DJ_FADE_DOWN_MS", "400")
    assert dj.plan_cue(CT.MUSIC, priority="urgent").fade_down_ms == 150
    assert dj.plan_cue(CT.MUSIC).fade_down_ms == 400


def test_estimate_knows_nothing_of_no_text():
    assert dj.estimate_seconds("") is None
    assert dj.estimate_seconds("word " * 200) > 45


# ---- the fader ----------------------------------------------------------

def test_fade_steps_and_lands_exactly():
    seen = []
    dj.fade(seen.append, 130, 10, 100, step_ms=10)
    assert seen[-1] == 10
    assert len(seen) > 3
    assert all(a >= b for a, b in zip(seen, seen[1:]))


def test_no_time_is_one_step():
    seen = []
    dj.fade(seen.append, 130, 10, 0)
    assert seen == [10]


# ---- the coordinator ------------------------------------------------------

def test_a_reply_fades_down_and_back_up(state):
    music = FakeMusic(volume=130)
    c = _coord(state, music)

    c.before_speech(est_s=10)
    assert music.volume == 10
    down = list(music.vols)
    assert len(down) > 2 and any(10 < v < 130 for v in down)
    # The debt is the plain duck's, so a killed fade is still paid.
    assert state.get_music_duck() == {"level": 10, "baseline": 130,
                                      "target": "local"}

    music.vols.clear()
    c.after_speech()
    assert music.volume == 130
    assert any(10 < v < 130 for v in music.vols)
    assert all(a <= b for a, b in zip(music.vols, music.vols[1:]))
    assert state.get_music_duck() is None
    assert state.get_now_playing("music") is None


def test_a_short_notice_only_dips(state):
    music = FakeMusic(volume=100)
    c = _coord(state, music)
    c.before_speech(est_s=2)
    assert music.volume == 25
    c.after_speech()
    assert music.volume == 100


def test_a_question_fades_out_and_stops_then_comes_back(state):
    music = FakeMusic(volume=100)
    c = _coord(state, music)

    c.before_speech(ask=True, est_s=6)
    assert music.paused
    # Faded all the way out before stopping, so the stop is silent.
    assert music.events == [("pause", 0)]

    c.after_speech()
    assert not music.paused
    # Resumed silent, then faded in.
    assert music.events[-1] == ("resume", 0)
    assert music.volume == 100
    assert state.get_music_duck() is None


def test_a_backend_that_ducks_itself_is_not_stepped(state):
    music = FakeMusic(volume=100)
    music.fades = False
    c = _coord(state, music)
    c.before_speech(est_s=10)
    assert music.vols == [10]
    c.after_speech()
    assert music.vols == [10, 100]


def test_a_duck_already_in_force_is_never_raised_to_fade_it(state):
    music = FakeMusic(volume=10)
    state.set_music_duck({"level": 10, "baseline": 130, "target": "local"})
    c = _coord(state, music)
    c.before_speech(est_s=10)
    assert max(music.vols) == 10
    c.after_speech()
    assert music.volume == 130


def test_off_is_the_plain_duck(state, monkeypatch):
    monkeypatch.delenv("MEDIA_DJ_DUCK")
    music = FakeMusic(volume=130)
    c = _coord(state, music)
    c.before_speech(ask=True, est_s=90)
    assert music.vols == [10] and not music.paused
    c.after_speech()
    assert music.vols == [10, 130]


def test_the_rooms_fade_together(state, monkeypatch):
    monkeypatch.setenv("MEDIA_DUCK_ROOMS_STREAM", "am-music")
    clients = {"hpo-music": 100, "p8a-music": 80}
    calls = []

    def clients_on_stream(stream_id, timeout=4.0, audible_only=True):
        return [{"id": cid, "percent": pct} for cid, pct in clients.items()]

    def set_client_volume(client_id, percent, muted=None, timeout=4.0):
        calls.append((client_id, percent))
        clients[client_id] = percent

    monkeypatch.setattr(coord_mod.snapcast, "clients_on_stream", clients_on_stream)
    monkeypatch.setattr(coord_mod.snapcast, "set_client_volume", set_client_volume)
    monkeypatch.setattr(coord_mod.snapcast, "client_exists", lambda cid: True)

    class Silent:
        def now_playing_uri(self, target):
            return None

    c = _coord(state, Silent())
    c.before_speech(est_s=10)
    assert clients == {"hpo-music": 10, "p8a-music": 10}
    assert any(10 < v < 100 for cid, v in calls if cid == "hpo-music")

    calls.clear()
    c.after_speech()
    assert clients == {"hpo-music": 100, "p8a-music": 80}
    assert any(10 < v < 80 for cid, v in calls if cid == "p8a-music")
    assert state.get_rooms_duck() is None


# ---- a player that fades itself (Sasonica's DjCue) -----------------------

def test_the_cue_on_the_wire():
    assert dj.plan_cue(CT.MUSIC, est_s=2).wire() == (
        "level=0.25;down=80;up=120;beat=0;stop=0")
    assert dj.plan_cue(CT.MUSIC, ask=True).wire().endswith("stop=1")
    assert dj.plan_cue(CT.MUSIC, ask=True).wire().startswith("level=0.00;")


def test_the_cue_is_sent_before_the_reply_and_cleared_after(state, monkeypatch):
    sent = []
    monkeypatch.setattr(coord_mod._speech, "set_duck_cue",
                        lambda cue, target: sent.append(cue))
    music = FakeMusic(volume=100)
    c = _coord(state, music)
    c.before_speech(est_s=2)
    c._flag_writer.submit(lambda: None).result()
    assert sent == ["level=0.25;down=80;up=120;beat=0;stop=0"]
    c.after_speech()
    c._flag_writer.submit(lambda: None).result()
    assert sent[-1] == ""


def test_a_player_that_fades_itself_is_left_alone(state, monkeypatch):
    monkeypatch.setattr(coord_mod._speech, "set_duck_cue", lambda cue, target: None)
    music = FakeMusic(volume=100)
    music.fades = False
    music.self_ducks = True
    c = _coord(state, music)
    c.before_speech(ask=True, est_s=6)
    # Neither paused nor ducked from here: the app fades and stops itself.
    assert not music.paused and music.vols == []
    c.after_speech()
    assert not music.events
    assert music.volume == 100
