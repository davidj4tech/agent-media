"""Earcons: a tick for a reply cut short, two rising notes before a barge-in,
two falling ones for a reply held behind a Play (agent_media_core/earcons.py).

No audio and no sockets: the tones are written to a throwaway cache dir and
"played" into recording sinks that note every clip, cue and stop in order.
"""

import array
import threading
import time
import wave
from pathlib import Path

import pytest

from agent_media_core import earcons
from agent_media_core.intake import submit as S
from agent_media_core.intake import toast
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Priority, Source, Target

from test_remote_playlist_hold import _RecordingCoord  # noqa: F401

A, B = "sess-a", "sess-b"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("MEDIA_AUDIO_DIR", raising=False)
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "local")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "10")
    monkeypatch.setenv("MEDIA_EARCONS", "1")
    for k in ("CUT", "INTERRUPT", "HELD"):
        monkeypatch.delenv(f"MEDIA_EARCON_{k}", raising=False)
    monkeypatch.setattr(S, "render_text", _render)
    monkeypatch.setattr(S, "_clip_duration", lambda p: 0.0)
    return tmp_path


def _render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


class _Coord:
    def pre_pause_remote(self): pass
    def before_speech(self, title="", priority="", defer_music=False, text=""): pass
    def speaking_line(self, text=""): pass
    def after_speech(self): pass


def _cue_name(uri):
    return Path(uri).name.split("-")[1]


class _Sink:
    """The desk's per-clip lane. A clip is 'playing' for one idle poll; a cue
    plays and is done. `log` is everything, in order: ("clip", uri, target),
    ("cue", name, target), ("stop", target)."""

    def __init__(self, on_play=None, hold=None):
        self.log: list[tuple] = []
        self._busy = False
        self._lock = threading.Lock()
        self.on_play = on_play
        self.hold = hold          # an Event: the first clip plays until set

    def play(self, uri, target, **_):
        with self._lock:
            self.log.append(("clip", uri, target.name))
            self._busy = True
        if self.on_play:
            self.on_play(len(self.clips))

    def play_cue(self, uri, target):
        with self._lock:
            self.log.append(("cue", _cue_name(uri), target.name))
            self._busy = True
        return True

    def idle(self, target):
        if self.hold is not None and not self.hold.is_set() and self.clips:
            return False
        with self._lock:
            busy, self._busy = self._busy, False
        return not busy

    def paused(self, target):
        return False

    def stop(self, target=None):
        self.log.append(("stop", target.name if target else None))

    @property
    def clips(self):
        return [e for e in self.log if e[0] == "clip"]

    @property
    def cues(self):
        return [e[1] for e in self.log if e[0] == "cue"]


def _say(text, session, state, sink, **md):
    return S.submit_event(Event(text=text, source=Source.CLAUDE_CODE,
                                metadata={"session": session, **md}),
                          state=state, sink=sink, coordinator=_Coord())


# --- the tones ----------------------------------------------------------------

@pytest.mark.parametrize("name,longest", [("cut", 0.15), ("interrupt", 0.32),
                                          ("held", 0.35)])
def test_each_tone_is_a_short_quiet_clean_wav(name, longest):
    p = earcons.path(name)
    assert p.parent.name == "audio", "not beside the clips the phone can reach"
    with wave.open(str(p)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 48000)
        frames = w.readframes(w.getnframes())
        dur = w.getnframes() / w.getframerate()
    assert 0.08 <= dur <= longest
    pcm = array.array("h", frames)
    peak = max(abs(s) for s in pcm) / 32768
    import math
    assert -12.5 <= 20 * math.log10(peak) <= -11.5
    # Smooth in and out: no sample jump at the ends big enough to click.
    assert abs(pcm[0]) < 50 and abs(pcm[-1]) < 50
    assert max(abs(pcm[i + 1] - pcm[i]) for i in range(len(pcm) - 1)) < 0.25 * 32768


def test_written_once_and_reused():
    p = earcons.path("cut")
    before = p.stat().st_mtime_ns
    time.sleep(0.01)
    assert earcons.path("cut") == p and p.stat().st_mtime_ns == before


def test_play_never_raises():
    class _Broken:
        def play_cue(self, uri, target):
            raise RuntimeError("bridge down")

    assert earcons.play("cut", Target(name="local"), _Broken()) is False
    assert earcons.play("cut", Target(name="local"), object()) is False


def test_no_sink_no_tone():
    """No default player: a caller that did not hand one over plays nothing,
    rather than the configured real one."""
    assert earcons.play("cut", Target(name="local"), None) is False
    assert earcons.play("cut", None, _Sink()) is False


@pytest.mark.parametrize("state,sent", [
    ({"idle-active": True, "pause": False}, True),
    ({"idle-active": True, "pause": True}, True),      # idle; a stale pause
    ({"idle-active": False, "pause": True}, False),    # a reply paused on it
    ({"idle-active": False, "pause": False}, False),   # a reply playing
    ({}, False),                                       # no answer
])
def test_a_cue_never_lands_on_a_player_holding_a_reply(monkeypatch, state, sent):
    from agent_media_core.sinks import speech as SP

    batches = []
    monkeypatch.setattr(SP.ipc, "get_properties", lambda sock, props, **k: dict(state))
    monkeypatch.setattr(SP.ipc, "command_batch",
                        lambda sock, cmds, **k: batches.append(cmds))
    assert SP.SinkSpeech().play_cue("/x/earcon-cut-v1.wav", Target(name="local")) is sent
    assert bool(batches) is sent


def test_the_switches(monkeypatch):
    assert earcons.enabled("cut")
    monkeypatch.setenv("MEDIA_EARCON_CUT", "0")
    assert not earcons.enabled("cut") and earcons.enabled("held")
    monkeypatch.delenv("MEDIA_EARCON_CUT")
    monkeypatch.setenv("MEDIA_EARCONS", "0")
    assert not any(earcons.enabled(n) for n in earcons.NAMES)
    sink = _Sink()
    assert earcons.play("held", Target(name="local"), sink) is False
    assert sink.cues == []


# --- cut ----------------------------------------------------------------------

def test_a_cut_ticks_after_the_reply_on_its_target():
    state = StateStore()
    sink = _Sink(on_play=lambda n: n == 1 and S.request_session_speech_cut(A, "all"))
    _say("First sentence here. Second sentence here. Third one too.", A, state, sink)
    assert [e[0] for e in sink.log] == ["clip", "cue"]
    assert sink.log[1] == ("cue", "cut", sink.log[0][2])


def test_one_ending_ticks_once():
    """The app's Stop cuts the thread and stops the player: the server and
    the reply's own loop can both see it. One tick, not two."""
    sink = _Sink()
    assert earcons.play("cut", Target(name="local"), sink)
    assert not earcons.play("cut", Target(name="local"), _Sink())
    assert sink.cues == ["cut"]
    # Not the other tones: two questions in a row each get their warning.
    assert earcons.play("interrupt", Target(name="local"), sink)
    assert earcons.play("interrupt", Target(name="local"), sink)


def test_a_natural_end_does_not_tick():
    state, sink = StateStore(), _Sink()
    _say("First sentence here. Second sentence here.", A, state, sink)
    assert len(sink.clips) == 2 and sink.cues == []


def test_a_reply_dropped_before_it_started_does_not_tick():
    state, sink = StateStore(), _Sink()
    S.request_session_speech_cut(A, "after")
    _say("Never heard.", A, state, sink)
    assert sink.log == []


def test_the_cut_switch(monkeypatch):
    monkeypatch.setenv("MEDIA_EARCON_CUT", "0")
    state = StateStore()
    sink = _Sink(on_play=lambda n: n == 1 and S.request_session_speech_cut(A, "all"))
    _say("First sentence here. Second sentence here.", A, state, sink)
    assert len(sink.clips) == 1 and sink.cues == []


class _PhoneSink:
    """The phone's playlist lane (test_remote_reply_read): the reply is read
    on the third tick; the player moves to sentence 2 on tick 8."""

    def __init__(self, read_on=3):
        self.ticks = 0
        self.log: list[tuple] = []
        self.pos = 0
        self.read_on = read_on

    def prefetch(self, paths, target=None): pass
    def play_playlist(self, uris, target=None, gapless=True):
        self.log.append(("playlist", len(uris), target.name))
    def set_playlist_pos(self, pos, target=None): pass
    def muted(self, target): return False

    def play_cue(self, uri, target):
        self.log.append(("cue", _cue_name(uri), target.name))
        self.cue_playing = True
        return True

    def idle(self, target):
        if getattr(self, "cue_playing", False):
            self.cue_playing = False        # heard for one poll, then done
            return False
        return any(e[0] == "stop" for e in self.log)

    def stop(self, target=None):
        self.log.append(("stop", target.name))

    def snapshot(self, target=None):
        self.ticks += 1
        if any(e[0] == "stop" for e in self.log):
            return {"idle-active": True, "pause": False, "playlist-count": 0}
        if self.ticks == self.read_on:
            S.session_reply_read(A)
        self.pos = 0 if self.ticks < 8 else 1 if self.ticks < 12 else 2
        if self.ticks >= 16:
            return {"idle-active": True, "pause": False, "playlist-count": 3}
        return {"idle-active": False, "pause": False, "playlist-pos": self.pos,
                "time-pos": self.ticks, "playlist-count": 3}


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")
    monkeypatch.setattr(S.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(earcons.time, "sleep", lambda *_a, **_k: None)


def _phone_say(sink):
    S.submit_event(Event(text="First sentence here. Second sentence here. Third one too.",
                         source=Source.CLAUDE_CODE, target=Target(name="phone"),
                         metadata={"session": A}),
                   state=StateStore(), sink=sink, coordinator=_RecordingCoord())


def test_a_read_cut_ticks_once_after_the_phone_stops(phone):
    sink = _PhoneSink()
    _phone_say(sink)
    kinds = [e[0] for e in sink.log]
    assert kinds.count("cue") == 1, sink.log
    assert kinds.index("cue") == kinds.index("stop") + 1, "not right after the stop"
    assert sink.log[kinds.index("cue")] == ("cue", "cut", "phone")


def test_a_reply_the_phone_plays_out_does_not_tick(phone):
    sink = _PhoneSink(read_on=10 ** 6)
    _phone_say(sink)
    assert not [e for e in sink.log if e[0] in ("cue", "stop")], sink.log


def test_a_listeners_stop_ticks_on_the_player_it_stopped(monkeypatch):
    from agent_media_core.sinks import speech as SP

    sent = []
    monkeypatch.setattr(SP.ipc, "command", lambda sock, *a, **k: sent.append(a))
    cued = []
    monkeypatch.setattr(SP.SinkSpeech, "play_cue",
                        lambda self, uri, target: cued.append((_cue_name(uri), target.name)) or True)
    monkeypatch.setattr(SP.SinkSpeech, "prefetch", lambda self, paths, target=None: True)
    # Nothing speaking: a stop of nothing is not a cut.
    SP.mark_speech_stopped()
    SP.SinkSpeech().stop(Target(name="local"))
    time.sleep(0.2)
    assert cued == []
    StateStore().set_now_playing("speech", uri="/x.mp3", started_at=time.time(),
                                 target="local", extras={"source_session": A})
    SP.mark_speech_stopped()
    SP.SinkSpeech().stop(Target(name="local"))
    for t in [th for th in threading.enumerate() if th.name == "earcon-cut"]:
        t.join(timeout=5)
    assert cued == [("cut", "local")]
    # A stop nobody marked (a lane's own) does not tick.
    SP.SinkSpeech().stop(Target(name="local"))
    time.sleep(0.2)
    assert cued == [("cut", "local")]


# --- interrupt ----------------------------------------------------------------

def test_a_question_barging_over_a_live_reply_opens_with_the_interrupt():
    state = StateStore()
    release = threading.Event()
    sink = _Sink(hold=release)
    rb = {}
    tb = threading.Thread(target=lambda: rb.setdefault(
        "rid", _say("First sentence here. Second sentence here.", B, state, sink)))
    tb.start()
    deadline = time.time() + 5
    while not sink.clips and time.time() < deadline:
        time.sleep(0.02)
    ta = threading.Thread(target=lambda: S.submit_event(
        Event(text="Which one?", source=Source.CLAUDE_CODE, priority=Priority.HIGH,
              metadata={"session": A, "kind": "notif", "ask": True}),
        state=state, sink=sink, coordinator=_Coord()))
    ta.start()
    deadline = time.time() + 5
    while not [q for q in S.speech_queue() if q["session"] == A] and time.time() < deadline:
        time.sleep(0.02)
    release.set()
    ta.join(timeout=20)
    tb.join(timeout=20)
    kinds = [e[0] for e in sink.log]
    assert kinds == ["clip", "cue", "clip", "clip"], sink.log
    assert sink.cues == ["interrupt"]
    # The question came straight after the tone (its first clip carries the
    # reply's text sidecar); B resumed after it.
    assert Path(sink.log[2][1]).with_suffix(".txt").read_text() == "Which one?"


def test_a_question_into_silence_needs_no_warning():
    state, sink = StateStore(), _Sink()
    _say("Which one?", A, state, sink, kind="notif", ask=True)
    assert len(sink.clips) == 1 and sink.cues == []


def test_a_question_while_another_thread_speaks_is_announced():
    state, sink = StateStore(), _Sink()
    state.set_now_playing("speech", uri="/x.mp3", started_at=time.time(),
                          target="local", extras={"source_session": B})
    _say("Which one?", A, state, sink, kind="notif", ask=True)
    assert [e[0] for e in sink.log] == ["cue", "clip"] and sink.cues == ["interrupt"]


def test_its_own_lead_in_is_not_an_interruption():
    state, sink = StateStore(), _Sink()
    state.set_now_playing("speech", uri="/x.mp3", started_at=time.time(),
                          target="local", extras={"source_session": A})
    _say("Which one?", A, state, sink, kind="notif", ask=True)
    assert sink.cues == []


def test_the_interrupt_switch(monkeypatch):
    monkeypatch.setenv("MEDIA_EARCON_INTERRUPT", "0")
    state, sink = StateStore(), _Sink()
    state.set_now_playing("speech", uri="/x.mp3", started_at=time.time(),
                          target="local", extras={"source_session": B})
    _say("Which one?", A, state, sink, kind="notif", ask=True)
    assert sink.cues == [] and len(sink.clips) == 1


def test_a_displacement_is_taken_once_and_only_if_recent():
    t0 = time.time()
    S.note_displaced()
    assert S._take_displaced(t0)
    assert not S._take_displaced(t0)            # consumed
    S.note_displaced()
    assert not S._take_displaced(time.time() + 1)  # older than our wait


# --- held ---------------------------------------------------------------------

@pytest.fixture
def desk(monkeypatch):
    monkeypatch.setattr(toast, "_tmux", lambda args: "")
    from agent_media_core.sinks import speech as SP

    cued = []

    class _Rec(_Sink):
        def play_cue(self, uri, target):
            cued.append((_cue_name(uri), target.name))
            return super().play_cue(uri, target)

    monkeypatch.setattr(SP, "SinkSpeech", _Rec)
    return cued


def _held_event(session="s1"):
    return Event(text="a reply", source=Source.CLAUDE_CODE,
                 metadata={"kind": "stop", "session": session})


def test_a_held_reply_chimes(desk):
    toast.remember(_held_event())
    assert desk == [("held", "local")]


def test_a_quiet_conversation_stays_silent(desk):
    from agent_media_core import speak_priority
    speak_priority.set_level("s1", "quiet")
    toast.remember(_held_event("s1"), ask=True)   # its question is held too
    assert desk == []


def test_no_chime_over_live_speech(desk):
    StateStore().set_now_playing("speech", uri="/x.mp3", started_at=time.time(),
                                 target="local", extras={"source_session": B})
    toast.remember(_held_event())
    assert desk == []


def test_no_chime_while_another_holds_the_voice(desk):
    lock = S._SpeechPlaybackLock()
    lock.acquire(Priority.NORMAL, session="other")
    try:
        toast.remember(_held_event())
    finally:
        lock.release()
    assert desk == []


def test_the_held_switch(desk, monkeypatch):
    monkeypatch.setenv("MEDIA_EARCON_HELD", "0")
    toast.remember(_held_event())
    assert desk == []


def test_end_of_reply_on_the_phone_ticks(phone, monkeypatch):
    """End of reply is the listener cutting it short: it ticks like a Stop
    (David, 25 Sep 2026: "I'm not hearing the chime when I hit end reply")."""
    sink = _PhoneSink(read_on=10 ** 6)
    asks = iter([None] * 4 + [99])
    monkeypatch.setattr(S, "_read_nav_request", lambda target: next(asks, None))
    _phone_say(sink)
    kinds = [e[0] for e in sink.log]
    assert kinds.count("cue") == 1, sink.log
    assert kinds.index("cue") == kinds.index("stop") + 1
    assert sink.log[kinds.index("cue")] == ("cue", "cut", "phone")


def test_a_sentence_step_does_not_tick(phone, monkeypatch):
    sink = _PhoneSink(read_on=10 ** 6)
    asks = iter([None] * 4 + [1])
    monkeypatch.setattr(S, "_read_nav_request", lambda target: next(asks, None))
    _phone_say(sink)
    assert not [e for e in sink.log if e[0] == "cue"], sink.log
