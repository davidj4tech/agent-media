"""Streaming: a reply starts on the sentences that have rendered.

Phase 1 used to resolve every render future before any audio played, so
time-to-first-audio was the reply's SLOWEST sentence. Measured on red5 with 32
sentences through edge, that was ~5s against 0.7s for the first one
(docs/speech-latency-notes.md).

With MEDIA_STREAM_CLIPS the reply starts on a lead of contiguous audio and the
rest are appended to the playlist as they land. The flag exists because the
offsets behind `play_started_at + clip_starts_s` stop being known up front, so
there has to be a way back to a whole-reply plan.
"""

import threading

from pathlib import Path

import pytest

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source, Target


PHONE = Target(name="phone")
# Four sentences that the splitter keeps apart — it coalesces short leading
# ones, and this test counts clips.
FOUR = ("The first sentence of this reply is long enough to stand on its own. "
        "The second sentence of this reply is also long enough to stand alone. "
        "The third sentence of this reply is likewise long enough to stand alone. "
        "The fourth sentence of this reply is long enough to stand by itself too.")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")
    monkeypatch.setattr(S.time, "sleep", lambda *_a, **_k: None)
    # Every clip is two seconds long, so a lead is a count the test can pick.
    monkeypatch.setattr(S, "_clip_duration", lambda _p: 2.0)


class _Coord:
    def __init__(self):
        self.before = self.after = 0

    def pre_pause_remote(self):
        pass

    def before_speech(self, title="", priority="", defer_music=False, text=""):
        self.before += 1

    def speaking_line(self, text=""):
        pass

    def after_speech(self):
        self.after += 1

    def release_music_duck(self):
        pass

    def reapply_music_duck(self):
        pass


class _Sink:
    """Plays through whatever it has been given, then reports idle."""

    def __init__(self, gate=None, idle_first=0):
        self.loaded = []        # uris passed to play_playlist
        self.appended = []      # uris passed to append_clips
        self.prefetched = []
        self.snapshots = 0
        self._gate = gate
        self._idle_first = idle_first

    def prefetch(self, paths, target=None):
        self.prefetched.extend(str(p) for p in paths)

    def play_playlist(self, uris, target=None, gapless=True):
        self.loaded = [str(u) for u in uris]

    def append_clips(self, uris, target=None):
        self.appended.extend(str(u) for u in uris)
        return True

    def snapshot(self, target=None):
        self.snapshots += 1
        # Let the held-back renders go once the follow loop has proved it
        # keeps polling an idle player rather than calling the reply over.
        if self._gate is not None and self.snapshots >= self._idle_first:
            self._gate.set()
        # A real snapshot always carries `pause` — the follow loop now counts
        # one that doesn't as unreadable rather than as "not paused".
        return {"idle-active": True, "pause": False}

    def set_playlist_pos(self, pos, target=None):
        pass

    def stop(self, target=None):
        pass

    def muted(self, target):
        return False

    def active_other_owner(self, target):
        return None

    def claim_broker(self, target):
        return True

    def refresh_broker(self, target):
        pass

    def release_broker(self, target):
        pass


def _render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


def _row(state):
    rows = state.recent_history(sink="speech", limit=1)
    return rows[0] if rows else None


def test_the_reply_starts_on_its_lead_and_the_rest_are_appended(monkeypatch):
    monkeypatch.setattr(S, "render_text", _render)
    monkeypatch.setenv("MEDIA_STREAM_CLIPS", "1")
    monkeypatch.setenv("MEDIA_STREAM_LEAD_S", "4")   # two clips of 2s

    state, sink, coord = StateStore(), _Sink(), _Coord()
    rid = S.submit_event(Event(text=FOUR, source=Source.CLI, target=PHONE,
                               metadata={"pane": "%7"}),
                         state=state, sink=sink, coordinator=coord)

    assert rid is not None
    assert len(sink.loaded) == 2, (
        f"playback should start on the lead, not the whole reply: {sink.loaded}")
    assert len(sink.appended) == 2, "the rest belong to the playlist too"
    assert sink.loaded + sink.appended == sorted(sink.loaded + sink.appended), (
        "clips must reach the player in the order they are spoken")
    # Appended clips are pushed to the player's own dir first, like the lead.
    for uri in sink.appended:
        assert uri in sink.prefetched


def test_the_archive_holds_every_sentence(monkeypatch):
    monkeypatch.setattr(S, "render_text", _render)
    monkeypatch.setenv("MEDIA_STREAM_CLIPS", "1")
    monkeypatch.setenv("MEDIA_STREAM_LEAD_S", "2")   # one clip

    state = StateStore()
    S.submit_event(Event(text=FOUR, source=Source.CLI, target=PHONE,
                         metadata={"pane": "%7"}),
                   state=state, sink=_Sink(), coordinator=_Coord())

    ex = (_row(state) or {}).get("extras") or {}
    assert len(ex.get("clip_uris") or []) == 4, (
        "a reply is archived whole even though it was played in pieces")
    assert len(ex.get("clip_sentences") or []) == 4
    assert ex.get("clip_durations_s") == [2.0, 2.0, 2.0, 2.0]


def test_running_out_of_clips_is_not_the_end_of_the_reply(monkeypatch):
    """The player can drink the lead faster than the tail renders.

    Idle then means "waiting for the next clip". Ending the reply there was
    the whole risk of streaming, so the follow loop has to keep polling.
    """
    gate = threading.Event()

    def _slow_tail(text, outfile, **_):
        # Sentences past the lead are held until the follow loop has polled an
        # idle player a few times without giving up on the reply.
        if not text.startswith("The first"):
            gate.wait(timeout=5)
        return _render(text, outfile)

    monkeypatch.setattr(S, "render_text", _slow_tail)
    monkeypatch.setenv("MEDIA_STREAM_CLIPS", "1")
    monkeypatch.setenv("MEDIA_STREAM_LEAD_S", "2")   # one clip

    state, sink = StateStore(), _Sink(gate=gate, idle_first=3)
    S.submit_event(Event(text=FOUR, source=Source.CLI, target=PHONE,
                         metadata={"pane": "%7"}),
                   state=state, sink=sink, coordinator=_Coord())

    assert sink.snapshots >= 3, (
        "an idle player ended the reply while sentences were still rendering")
    assert len(sink.appended) == 3


def test_without_the_flag_the_whole_reply_is_loaded_up_front(monkeypatch):
    monkeypatch.setattr(S, "render_text", _render)
    monkeypatch.delenv("MEDIA_STREAM_CLIPS", raising=False)

    state, sink = StateStore(), _Sink()
    S.submit_event(Event(text=FOUR, source=Source.CLI, target=PHONE,
                         metadata={"pane": "%7"}),
                   state=state, sink=sink, coordinator=_Coord())

    assert len(sink.loaded) == 4
    assert sink.appended == []
    ex = (_row(state) or {}).get("extras") or {}
    assert len(ex.get("clip_uris") or []) == 4


def test_a_local_target_never_streams(monkeypatch):
    """The per-sentence path would have to wait mid-loop for an unrendered
    clip; the playlist path only has to append."""
    monkeypatch.setenv("MEDIA_STREAM_CLIPS", "1")
    assert S._stream_clips(Target(name="local")) is False


def test_the_render_pool_is_bounded(monkeypatch):
    monkeypatch.delenv("MEDIA_RENDER_WORKERS", raising=False)
    assert S._render_workers(32) == 4      # not one per sentence
    assert S._render_workers(2) == 2       # never more workers than sentences
    monkeypatch.setenv("MEDIA_RENDER_WORKERS", "0")
    assert S._render_workers(32) == 32     # the old behaviour, on request


class _Player(_Sink):
    """Behaves like the phone's players: each clip plays for one snapshot and
    the player advances only if it already holds the next one. Out of clips,
    it goes idle with its list intact — and an append does not restart it.
    A stop clears the list, as both mpv and Sasonica do."""

    def __init__(self, gate, open_at=3, stop_after=None, keeps_pos=False):
        super().__init__()
        self.list = []
        self.pos = None
        # Sasonica stays on the last clip it played when it runs dry; mpv
        # reports -1.
        self._keeps_pos = keeps_pos
        self._last = -1
        self.jumps = []
        self.played = []
        self._gate = gate
        self._open_at = open_at
        self._stop_after = stop_after

    def play_playlist(self, uris, target=None, gapless=True):
        self.loaded = [str(u) for u in uris]
        self.list = list(self.loaded)
        self.pos = 0

    def append_clips(self, uris, target=None):
        self.appended.extend(str(u) for u in uris)
        self.list.extend(str(u) for u in uris)
        return True

    def set_playlist_pos(self, pos, target=None):
        self.jumps.append(pos)
        self.pos = pos

    def stop(self, target=None):
        self.list = []
        self.pos = None

    def snapshot(self, target=None):
        self.snapshots += 1
        if self.snapshots >= self._open_at:
            self._gate.set()
        if self.pos is None:
            return {"idle-active": True, "pause": False,
                    "playlist-count": len(self.list),
                    "playlist-pos": self._last if self._keeps_pos else -1}
        cur = self.pos
        self._last = cur
        self.played.append(cur)
        if self._stop_after is not None and cur == self._stop_after:
            self.stop()          # somebody pressed stop at the phone
        else:
            self.pos = cur + 1 if cur + 1 < len(self.list) else None
        return {"idle-active": False, "playlist-pos": cur, "pause": False,
                "time-pos": float(self.snapshots), "mute": False,
                "playlist-count": len(self.list)}


def _gated(gate):
    def render(text, outfile, **_):
        if not text.startswith("The first"):
            gate.wait(timeout=5)
        return _render(text, outfile)
    return render


@pytest.mark.parametrize("keeps_pos", [False, True], ids=["mpv", "sasonica"])
def test_a_player_that_ran_dry_is_put_back_on_the_next_clip(monkeypatch,
                                                            keeps_pos):
    """The underrun, as the phone really behaves: the lead ends, the player
    idles, the next clip is appended — and nothing plays unless we jump."""
    gate = threading.Event()
    monkeypatch.setattr(S, "render_text", _gated(gate))
    monkeypatch.setenv("MEDIA_STREAM_CLIPS", "1")
    monkeypatch.setenv("MEDIA_STREAM_LEAD_S", "2")   # one clip

    player = _Player(gate, keeps_pos=keeps_pos)
    S.submit_event(Event(text=FOUR, source=Source.CLI, target=PHONE,
                         metadata={"pane": "%7"}),
                   state=StateStore(), sink=player, coordinator=_Coord())

    assert 1 in player.jumps, "the idle player was never restarted"
    assert player.played == [0, 1, 2, 3], (
        f"every sentence should be heard, in order: {player.played}")


def test_a_reply_stopped_at_the_phone_stays_stopped(monkeypatch):
    """A stop also leaves an idle player with sentences unplayed. It clears
    the list, which an underrun never does — so it is not restarted."""
    gate = threading.Event()
    monkeypatch.setattr(S, "render_text", _gated(gate))
    monkeypatch.setenv("MEDIA_STREAM_CLIPS", "1")
    monkeypatch.setenv("MEDIA_STREAM_LEAD_S", "2")   # one clip

    # Stopped on the first clip, with the tail still rendering: its appends
    # then refill the list the stop cleared, which must not read as ours.
    player = _Player(gate, open_at=1, stop_after=0)
    state = StateStore()
    S.submit_event(Event(text=FOUR, source=Source.CLI, target=PHONE,
                         metadata={"pane": "%7"}),
                   state=state, sink=player, coordinator=_Coord())

    assert player.jumps == [], f"a stopped reply was restarted: {player.jumps}"
    assert player.played == [0]
    ex = (_row(state) or {}).get("extras") or {}
    assert len(ex.get("clip_uris") or []) == 4, "still archived whole"


def test_the_first_sentence_starts_when_the_player_does(monkeypatch):
    """The first start is stamped when the reply is handed over, before the
    phone has fetched the clip; the player's own position moves it to when
    the voice began (p8a, 26 Sep 2026: 1.1-1.8 s, and the bold ran ahead)."""
    monkeypatch.setattr(S, "render_text", _render)
    clock = [1000.0]
    monkeypatch.setattr(S.time, "time", lambda: clock[0])

    class _Fetching(_Sink):
        # 2 s fetching (position 0), then clip 0 plays, then clip 1, then idle.
        def snapshot(self, target=None):
            self.snapshots += 1
            clock[0] += 0.5
            n = self.snapshots
            if n <= 4:
                return {"idle-active": False, "pause": False, "playlist-pos": 0, "time-pos": 0.0}
            if n <= 8:
                return {"idle-active": False, "pause": False, "playlist-pos": 0, "time-pos": (n - 4) * 0.5}
            if n <= 10:
                return {"idle-active": False, "pause": False, "playlist-pos": 1, "time-pos": (n - 8) * 0.5}
            return {"idle-active": True, "pause": False}

    state = StateStore()
    S.submit_event(Event(text=FOUR, source=Source.CLI, target=PHONE,
                         metadata={"pane": "%7"}),
                   state=state, sink=_Fetching(), coordinator=_Coord())
    starts = ((_row(state) or {}).get("extras") or {}).get("clip_starts_s") or []
    assert starts, "the reply measured no starts"
    assert 1.5 <= starts[0] <= 2.5, f"sentence one began when the player did, not at 0: {starts}"
    assert len(starts) < 2 or 1.5 <= starts[1] - starts[0] <= 2.5, f"and sentence two a clip later: {starts}"
