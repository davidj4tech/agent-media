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
        return {"idle-active": True}

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
