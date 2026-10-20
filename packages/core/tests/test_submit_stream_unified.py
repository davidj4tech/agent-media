"""A streamed reply runs the same clip loop as a finished one.

`submit_stream` used to be its own copy of the clip loop, and drifted: no
speech hold, no flush, no paragraph map, a different highlight rule, and for
two months a NameError before its first word (36dc6fc). It is now a front end
on `_submit_event` with a sentence source that is still growing. These tests
hold it to the loop's features, and to the one thing streaming is for: the
first sentence is heard before the model has written the second.
"""

import threading
from pathlib import Path

import pytest

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source, Target


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")
    monkeypatch.delenv("MEDIA_SPEECH_DEFAULT_TARGET", raising=False)
    monkeypatch.delenv("MEDIA_STREAM_CLIPS", raising=False)
    monkeypatch.setattr(S, "render_text", _render)
    monkeypatch.setattr(S, "_clip_duration", lambda _p: 2.0)


def _render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


class _Coord:
    def __init__(self):
        self.before = []        # (title, text) per before_speech
        self.lines = []         # speaking_line, in order
        self.after = 0

    def cancel_pre_pause(self): pass
    def pre_pause_remote(self): pass
    def release_music_duck(self): pass
    def reapply_music_duck(self): pass

    def before_speech(self, title="", priority="", defer_music=False, text="", **_):
        self.before.append((title, text))

    def speaking_line(self, text=""):
        self.lines.append(text)

    def after_speech(self):
        self.after += 1


class _LocalSink:
    """The per-sentence path: each clip starts, then is idle."""

    def __init__(self, on_play=None):
        self.plays = []
        self._on_play = on_play
        self._calls = 0

    def play(self, uri, target, **_):
        self.plays.append(str(uri))
        self._calls = 0
        if self._on_play is not None:
            self._on_play(len(self.plays))

    def idle(self, target):
        self._calls += 1
        return self._calls > 1

    def paused(self, target): return False
    def muted(self, target): return False
    def prefetch(self, paths, target=None): pass
    def stop(self, target=None): pass


def _event(**md):
    return Event(text="", source=Source.CLI,
                 metadata={"pane": "%7", "session": "s-stream", **md})


def _row(state):
    rows = state.recent_history(sink="speech", limit=1)
    return rows[0] if rows else None


SENTS = ["The first sentence arrives.", "The second one follows it.",
         "And a third closes the reply."]


def test_the_row_is_the_clip_loops_row():
    state, coord, sink = StateStore(), _Coord(), _LocalSink()
    rid = S.submit_stream(iter(SENTS), _event(title="Thread name"),
                          state=state, sink=sink, coordinator=coord)

    assert rid is not None
    assert len(sink.plays) == 3
    ex = _row(state)["extras"]
    assert ex["clip_sentences"] == SENTS
    assert len(ex["clip_uris"]) == 3
    assert ex["clip_durations_s"] == [2.0, 2.0, 2.0]
    # What only the finished reply's loop used to write.
    assert ex["clip_paragraph_idx"] == [0, 0, 0]
    assert len(ex.get("clip_starts_s") or []) == 3
    assert ex["streaming"] is True
    # A headless thread's name reaches the row and the card.
    assert ex["source_title"] == "Thread name"
    assert _row(state)["text"] == " ".join(SENTS)
    assert state.get_now_playing("speech") is None


def test_the_card_opens_on_the_first_sentence_and_follows_the_voice():
    coord = _Coord()
    S.submit_stream(iter(SENTS), _event(title="Thread name"),
                    state=StateStore(), sink=_LocalSink(), coordinator=coord)

    assert coord.before == [("Thread name", SENTS[0])]
    assert coord.lines == SENTS
    assert coord.after == 1


def test_the_first_sentence_plays_before_the_second_is_written():
    """Streaming's whole point: the voice does not wait for the model."""
    second_may_come = threading.Event()
    heard_before_second: list[int] = []

    def source():
        yield SENTS[0]
        assert second_may_come.wait(timeout=10), "never played the first"
        yield SENTS[1]

    def on_play(n):
        if n == 1:
            heard_before_second.append(n)
            second_may_come.set()

    state, sink = StateStore(), _LocalSink(on_play=on_play)
    S.submit_stream(source(), _event(), state=state, sink=sink,
                    coordinator=_Coord())

    assert heard_before_second == [1]
    assert len(sink.plays) == 2
    assert _row(state)["extras"]["clip_sentences"] == SENTS[:2]


def test_a_cut_stops_it_and_the_row_still_holds_every_sentence(monkeypatch):
    cut = {"on": False}

    def _cut(session, seq, **_):
        return cut["on"]

    monkeypatch.setattr(S, "_speech_cut", _cut)
    monkeypatch.setattr(S, "_note_read_stop", lambda *a, **k: None)
    played_cut_earcon = []
    from agent_media_core import earcons
    monkeypatch.setattr(earcons, "play",
                        lambda name, *a, **k: played_cut_earcon.append(name))

    def on_play(n):
        cut["on"] = True        # /session/stop while the first is speaking

    state, sink = StateStore(), _LocalSink(on_play=on_play)
    S.submit_stream(iter(SENTS), _event(), state=state, sink=sink,
                    coordinator=_Coord())

    assert len(sink.plays) == 1
    assert played_cut_earcon == ["cut"]
    # Archived whole: nobody heard the rest, but it is the reply.
    assert _row(state)["extras"]["clip_sentences"] == SENTS


def test_a_source_that_hangs_after_a_cut_is_archived_as_far_as_it_got(
        monkeypatch):
    """The history write waits for the model no longer than a finished reply
    waits for its renders; then it keeps what arrived, marked truncated."""
    monkeypatch.setattr(S, "_STREAM_ARCHIVE_WAIT_S", 0.3)
    cut = {"on": False}
    monkeypatch.setattr(S, "_speech_cut", lambda *a, **k: cut["on"])
    monkeypatch.setattr(S, "_note_read_stop", lambda *a, **k: None)
    from agent_media_core import earcons
    monkeypatch.setattr(earcons, "play", lambda *a, **k: None)
    release = threading.Event()

    def source():
        yield SENTS[0]
        release.wait(timeout=30)    # the model hangs after the listener cut
        yield SENTS[1]

    def on_play(n):
        cut["on"] = True

    state, sink = StateStore(), _LocalSink(on_play=on_play)
    done = threading.Event()
    rid = []

    def run():
        rid.append(S.submit_stream(source(), _event(), state=state, sink=sink,
                                   coordinator=_Coord()))
        done.set()

    threading.Thread(target=run, daemon=True).start()
    try:
        assert done.wait(timeout=10), "the history write waited on the model"
    finally:
        release.set()

    assert rid and rid[0] is not None
    assert len(sink.plays) == 1
    row = _row(state)
    assert row["extras"]["truncated"] is True
    assert row["extras"]["clip_sentences"] == [SENTS[0]]
    assert row["text"] == SENTS[0]


def test_a_supersede_drops_the_rest(monkeypatch):
    aborted = {"on": False}
    monkeypatch.setattr(S._SpeechPlaybackLock, "should_abort",
                        lambda self: aborted["on"])

    def on_play(n):
        aborted["on"] = True

    sink = _LocalSink(on_play=on_play)
    S.submit_stream(iter(SENTS), _event(), state=StateStore(), sink=sink,
                    coordinator=_Coord())
    assert len(sink.plays) == 1


def test_a_flush_before_it_starts_archives_it_unheard(monkeypatch):
    monkeypatch.setattr(S, "_speech_flushed", lambda seq: True)
    state, coord, sink = StateStore(), _Coord(), _LocalSink()
    rid = S.submit_stream(iter(SENTS), _event(), state=state, sink=sink,
                          coordinator=coord)

    assert rid is not None
    assert sink.plays == []
    assert coord.before == []
    ex = _row(state)["extras"]
    assert ex["flushed"] is True
    assert ex["clip_sentences"] == SENTS


def test_a_skip_past_the_end_while_the_model_writes_waits_for_more(monkeypatch):
    """There is no end to skip to yet: the loop goes on with what lands."""
    second_may_come = threading.Event()
    navs = iter([5])            # one jump, way past what has arrived

    def _wait(*_a, **_k):
        nav = next(navs, None)
        # The model writes its next sentence only after the jump was asked
        # for, so the jump lands while the reply is still arriving.
        second_may_come.set()
        return nav

    monkeypatch.setattr(S, "_wait_for_clip", _wait)

    def source():
        yield SENTS[0]
        second_may_come.wait(timeout=10)
        yield SENTS[1]

    sink = _LocalSink()
    state = StateStore()
    S.submit_stream(source(), _event(), state=state, sink=sink,
                    coordinator=_Coord())
    assert len(sink.plays) == 2


def test_nothing_speakable_writes_nothing():
    state, coord, sink = StateStore(), _Coord(), _LocalSink()
    rid = S.submit_stream(iter(["", "  "]), _event(), state=state, sink=sink,
                          coordinator=coord)
    assert rid is None
    assert sink.plays == [] and coord.before == []
    assert _row(state) is None


class _PhoneSink:
    """The playlist path, as test_stream_clips' player: one snapshot per clip,
    advancing only into clips it already holds; idle with its list intact."""

    def __init__(self):
        self.list, self.pos, self.loaded, self.appended = [], None, [], []
        self.played, self.jumps = [], []

    def prefetch(self, paths, target=None): pass
    def muted(self, target): return False
    def active_other_owner(self, target): return None
    def claim_broker(self, target): return True
    def refresh_broker(self, target): pass
    def release_broker(self, target): pass

    def play_playlist(self, uris, target=None, gapless=True):
        self.loaded = [str(u) for u in uris]
        self.list, self.pos = list(self.loaded), 0

    def append_clips(self, uris, target=None):
        self.appended.extend(str(u) for u in uris)
        self.list.extend(str(u) for u in uris)
        return True

    def set_playlist_pos(self, pos, target=None):
        self.jumps.append(pos)
        self.pos = pos

    def stop(self, target=None):
        self.list, self.pos = [], None

    def snapshot(self, target=None):
        if self.pos is None:
            return {"idle-active": True, "pause": False,
                    "playlist-count": len(self.list), "playlist-pos": -1}
        cur = self.pos
        self.played.append(cur)
        self.pos = cur + 1 if cur + 1 < len(self.list) else None
        return {"idle-active": False, "playlist-pos": cur, "pause": False,
                "time-pos": 0.5, "mute": False,
                "playlist-count": len(self.list)}


def test_on_the_phone_it_starts_on_one_clip_and_appends_the_rest(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")
    monkeypatch.setattr(S.time, "sleep", lambda *_a, **_k: None)
    gate = threading.Event()

    def source():
        yield SENTS[0]
        gate.wait(timeout=10)   # the model is slower than the first clip
        yield SENTS[1]
        yield SENTS[2]

    sink, state = _PhoneSink(), StateStore()

    def snapshot(target=None, _orig=sink.snapshot):
        gate.set()
        return _orig(target)

    sink.snapshot = snapshot
    S.submit_stream(source(), Event(text="", source=Source.CLI,
                                    target=Target(name="phone"),
                                    metadata={"pane": "%7"}),
                    state=state, sink=sink, coordinator=_Coord())

    assert len(sink.loaded) == 1, "started on its first clip, not a lead"
    assert len(sink.appended) == 2
    assert sink.played == [0, 1, 2]
    assert _row(state)["extras"]["clip_sentences"] == SENTS


def test_a_finished_reply_on_a_local_target_is_unchanged():
    """Control: submit_event still splits, plays every clip and archives."""
    state, coord, sink = StateStore(), _Coord(), _LocalSink()
    text = " ".join(SENTS)
    S.submit_event(Event(text=text, source=Source.CLI,
                         metadata={"pane": "%7"}),
                   state=state, sink=sink, coordinator=coord)
    ex = _row(state)["extras"]
    assert "streaming" not in ex
    assert len(sink.plays) == len(ex["clip_sentences"])
    assert _row(state)["text"] == text


def test_the_live_row_carries_the_replys_dedup_key():
    """The turn being spoken joins its thread message by key while it plays
    (book_tracks._live_turn → transcript.join_speech). Without the key on the
    live row, a reply whose code blocks were spoken as descriptions joined
    nothing and had no follow-along (session 2ccb8b31, 18 Oct 2026)."""
    state, coord = StateStore(), _Coord()
    seen = []
    sink = _LocalSink(on_play=lambda n: seen.append(
        ((state.get_now_playing("speech") or {}).get("extras") or {}).get("dedup_key")))
    S.submit_stream(iter(SENTS), _event(title="Thread name", dedup_key="c900c50f"),
                    state=state, sink=sink, coordinator=coord)
    assert seen and all(k == "c900c50f" for k in seen), seen
