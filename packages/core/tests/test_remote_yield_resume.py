"""Remote (tcp://) playlist path: cross-host wait/claim and higher-priority
yield-and-resume.

Covers the two gaps left after the blind-hold fix:
  * ``_wait_and_claim_broker`` waits out another host that actively holds the
    shared broker, and takes over once it frees / expires.
  * the follow loop steps aside for a higher-priority speaker and then resumes
    the interrupted reply (reloads the playlist, jumps back to the current
    sentence) instead of abandoning it.
"""

from pathlib import Path

import pytest

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source, Target


PHONE = Target(name="phone")


@pytest.fixture(autouse=True)
def state_env(tmp_path, monkeypatch):
    """Isolate every state write — without this the fixture events (pane %7,
    'First sentence here…') land in the REAL state.db and poison anything
    that reads recent speech history (e.g. the canvas's reply-to-speaker)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


# --------------------------------------------------------------------------
# _wait_and_claim_broker
# --------------------------------------------------------------------------

class _ScriptedOwnerSink:
    """active_other_owner returns each scripted value in turn (last repeats);
    claim_broker succeeds and records the call."""

    def __init__(self, owner_seq):
        self._seq = list(owner_seq)
        self.claims = 0

    def active_other_owner(self, target):
        return self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]

    def claim_broker(self, target):
        # As the real claim does: it loses while another host holds it.
        if self.active_other_owner(target) is not None:
            return False
        self.claims += 1
        return True


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(S.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")


def test_wait_claims_immediately_when_free(monkeypatch):
    sink = _ScriptedOwnerSink([None])
    S._wait_and_claim_broker(sink, PHONE)
    assert sink.claims == 1


def test_an_uncontended_claim_reads_the_owner_once():
    """The claim reads the owner itself before writing. Reading it first as
    well was a whole extra round trip to the phone on every reply."""
    class _Counting(_ScriptedOwnerSink):
        reads = 0

        def active_other_owner(self, target):
            _Counting.reads += 1
            return super().active_other_owner(target)

    sink = _Counting([None])
    S._wait_and_claim_broker(sink, PHONE)
    assert sink.claims == 1
    assert _Counting.reads == 1


def test_wait_holds_until_other_owner_frees(monkeypatch):
    # Other host holds it for two polls, then it frees.
    sink = _ScriptedOwnerSink([
        {"owner": "h2:1", "deadline": 2_000_000.0},
        {"owner": "h2:1", "deadline": 2_000_000.0},
        None,
    ])
    S._wait_and_claim_broker(sink, PHONE)
    assert sink.claims == 1  # only claimed once, after it freed


def test_wait_gives_up_on_stalled_owner(monkeypatch):
    # Owner never frees and its deadline never advances (stalled). With a zero
    # give-up timeout we proceed without claiming rather than wait forever.
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "0")
    sink = _ScriptedOwnerSink([{"owner": "h2:1", "deadline": 2_000_000.0}])
    S._wait_and_claim_broker(sink, PHONE)
    assert sink.claims == 0  # gave up; the caller plays best-effort


def test_wait_is_noop_for_local_target():
    sink = _ScriptedOwnerSink([{"owner": "h2:1", "deadline": 2_000_000.0}])
    S._wait_and_claim_broker(sink, Target(name="local"))
    assert sink.claims == 0


# --------------------------------------------------------------------------
# yield-and-resume in the remote follow loop
# --------------------------------------------------------------------------

def _fake_render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


class _RecordingCoord:
    def __init__(self):
        self.before = self.after = 0

    def cancel_pre_pause(self): pass

    def pre_pause_remote(self):
        pass

    def before_speech(self, title="", priority="", defer_music=False,
                          text="", **_):
        self.before += 1

    def speaking_line(self, text=""): pass
    def after_speech(self):
        self.after += 1

    def release_music_duck(self):
        pass

    def reapply_music_duck(self):
        pass


class _ResumeSink:
    """Plays two clips, is interrupted after reaching clip 1, then finishes.

    Snapshot sequence: pos 0, pos 1, (yield happens here), idle. Records the
    broker + playlist operations so the test can assert the reload/jump/claim.
    """

    def __init__(self):
        self.snaps = [
            {"playlist-pos": 0, "idle-active": False, "pause": False,
             "mute": False, "time-pos": 0.1},
            {"playlist-pos": 1, "idle-active": False, "pause": False,
             "mute": False, "time-pos": 0.2},
            {"idle-active": True},
        ]
        self.playlists = 0
        self.jumps = []
        self.stops = 0
        self.claims = 0
        self.releases = 0
        self.refreshes = 0

    # playback
    def prefetch(self, paths, target=None):
        pass

    def play_playlist(self, uris, target=None, gapless=True):
        self.playlists += 1

    def snapshot(self, target=None):
        return self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]

    def set_playlist_pos(self, pos, target=None):
        self.jumps.append(pos)

    def stop(self, target=None):
        self.stops += 1

    def muted(self, target):
        return False

    # broker token
    def active_other_owner(self, target):
        return None

    def claim_broker(self, target):
        self.claims += 1
        return True

    def refresh_broker(self, target):
        self.refreshes += 1

    def release_broker(self, target):
        self.releases += 1


def test_higher_priority_yield_resumes_the_reply(monkeypatch):
    monkeypatch.setattr(S, "render_text", _fake_render)
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")

    # should_yield fires exactly once, on the third check (after i has reached 1)
    # so the reply resumes from sentence 1 (a real playlist-pos jump).
    calls = {"n": 0, "yielded": 0}

    def fake_should_yield(self):
        calls["n"] += 1
        return calls["n"] == 3

    def fake_yield(self):
        calls["yielded"] += 1

    monkeypatch.setattr(S._SpeechPlaybackLock, "should_yield", fake_should_yield)
    monkeypatch.setattr(S._SpeechPlaybackLock, "yield_to_higher", fake_yield)

    state = StateStore()
    coord, sink = _RecordingCoord(), _ResumeSink()
    ev = Event(text="First sentence here. Second sentence here.",
               source=Source.CLI, target=PHONE, metadata={"pane": "%7"})

    rid = S.submit_event(ev, state=state, sink=sink, coordinator=coord)

    assert rid is not None
    assert calls["yielded"] == 1                 # stepped aside once
    assert sink.playlists == 2                   # initial load + reload on resume
    assert sink.jumps == [1]                     # resumed at sentence index 1
    assert sink.stops >= 1                        # broker stopped to hand over
    # Broker handed over on yield and re-taken (initial claim + reclaim).
    assert sink.releases >= 1
    assert sink.claims >= 2
    assert (coord.before, coord.after) == (1, 1)
    assert state.get_now_playing("speech") is None


def test_the_time_away_is_not_left_on_the_replys_clock(monkeypatch):
    """Back from stepping aside, the phone starts the sentence it was on again
    from its beginning: the reply's clock must read that sentence's start
    then, not its start plus however long the other speaker had. Left on, the
    time away was a hole before the next sentence that the app read as lag,
    and the bold went back three sentences (David, 16 Oct 2026)."""
    monkeypatch.setattr(S, "render_text", _fake_render)
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")

    real_time = S.time.time
    away = {"s": 0.0}
    monkeypatch.setattr(S.time, "time", lambda: real_time() + away["s"])
    calls = {"n": 0}

    def fake_should_yield(self):
        calls["n"] += 1
        return calls["n"] == 3

    def fake_yield(self):
        away["s"] += 40.0            # the other speaker's turn

    monkeypatch.setattr(S._SpeechPlaybackLock, "should_yield", fake_should_yield)
    monkeypatch.setattr(S._SpeechPlaybackLock, "yield_to_higher", fake_yield)

    state = StateStore()
    rows = []
    real_set = state.set_now_playing

    def recording_set(sink_name, **kw):
        if sink_name == "speech":
            rows.append((S.time.time(), dict(kw.get("extras") or {})))
        return real_set(sink_name, **kw)

    monkeypatch.setattr(state, "set_now_playing", recording_set)
    ev = Event(text="First sentence here. Second sentence here.",
               source=Source.CLI, target=PHONE, metadata={"pane": "%7"})
    S.submit_event(ev, state=state, sink=_ResumeSink(), coordinator=_RecordingCoord())

    after = [(t, ex) for t, ex in rows
             if away["s"] and ex.get("current_sentence_idx") == 1
             and ex.get("play_started_at") and t >= rows[0][0] + 40.0]
    assert after, "no row was written after the resume"
    t, ex = after[0]
    starts = ex.get("clip_starts_s") or []
    assert len(starts) >= 2
    elapsed = S.elapsed_from_row(ex, 0.0) - (real_time() + away["s"] - t)
    assert elapsed == pytest.approx(starts[1], abs=1.0), (
        "the time away was counted as reply")
