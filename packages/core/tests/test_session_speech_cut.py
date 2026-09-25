"""The per-session speech cut behind `/session/stop` (server-contract.md §12).

Two modes of one marker. `after`: a stop while the turn is working lets
through what was said before the press and drops what the interrupted turn
says after it, until the listener's next turn. `all`: a stop of this thread's
speech drops every reply of its not yet heard — queued, rendering, waiting —
and the rest of the one playing. Another session's speech is never touched,
and a dropped reply still writes its history row, marked flushed.

No audio and no sockets: a recording sink, a render that writes a byte, and a
throwaway state dir.
"""

import threading
import time
from pathlib import Path

import pytest

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source

A, B = "sess-a", "sess-b"


@pytest.fixture(autouse=True)
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.delenv("MEDIA_SPEECH_DEFAULT_TARGET", raising=False)
    monkeypatch.delenv("MEDIA_SPEECH_CUT_TTL_S", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "10")
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


class _Sink:
    """Records each clip played; a clip is 'playing' for one idle poll, then
    done. `on_play(n)` runs after the n-th play."""

    def __init__(self, on_play=None):
        self.played: list[str] = []
        self._busy = False
        self._lock = threading.Lock()
        self.on_play = on_play

    def play(self, uri, target, **_):
        with self._lock:
            self.played.append(uri)
            self._busy = True
        if self.on_play:
            self.on_play(len(self.played))

    def idle(self, target):
        with self._lock:
            busy, self._busy = self._busy, False
        return not busy

    def paused(self, target):
        return False

    def stop(self, target=None):
        pass


def _say(text, session, state, sink):
    return S.submit_event(Event(text=text, source=Source.CLAUDE_CODE,
                                metadata={"session": session}),
                          state=state, sink=sink, coordinator=_Coord())


def _row(state, rid):
    return next(r for r in state.recent_history(sink="speech", limit=50) if r["id"] == rid)


# --- after: the cutoff --------------------------------------------------------

def test_after_drops_what_is_said_after_the_press_and_nothing_before():
    state, sink = StateStore(), _Sink()
    S.request_session_speech_cut(A, "after", at=time.time() + 60)
    # Submitted before the stop time: still true, still heard.
    before = _say("Said before the stop.", A, state, sink)
    assert len(sink.played) == 1 and not _row(state, before)["extras"].get("flushed")

    # A second press in the same exchange: the first cutoff stands.
    first = S.session_speech_cut(A)["after"]
    S.request_session_speech_cut(A, "after", at=first + 30)
    assert S.session_speech_cut(A)["after"] == first
    S.end_session_speech_cut(A)
    S.request_session_speech_cut(A, "after")
    later = _say("Said by the interrupted turn.", A, state, sink)
    assert len(sink.played) == 1, "a reply after the cutoff was played"
    row = _row(state, later)
    assert row["extras"]["flushed"] is True
    assert row["text"] == "Said by the interrupted turn."

    # Another session is never touched.
    other = _say("Another thread entirely.", B, state, sink)
    assert len(sink.played) == 2 and not _row(state, other)["extras"].get("flushed")


def test_the_cutoff_ends_with_the_next_listener_turn():
    state, sink = StateStore(), _Sink()
    S.request_session_speech_cut(A, "after")
    _say("Dropped.", A, state, sink)
    assert sink.played == []
    S.end_session_speech_cut(A)
    assert S.session_speech_cut(A) == {}
    rid = _say("The next exchange speaks.", A, state, sink)
    assert len(sink.played) == 1 and not _row(state, rid)["extras"].get("flushed")


def test_the_prompt_hook_is_a_listener_turn(monkeypatch):
    from agent_media_core.intake import hook_claude_code as H

    class _BT:
        @staticmethod
        def record_listener_turn(session, text, extras=None):
            return True

    import sys

    import agent_media_core
    monkeypatch.setattr(agent_media_core, "book_tracks", _BT, raising=False)
    monkeypatch.setitem(sys.modules, "agent_media_core.book_tracks", _BT)
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    S.request_session_speech_cut(A, "after")
    S.request_session_speech_cut(B, "after")
    H._handle_user_prompt({"prompt": "carry on", "session_id": A})
    assert "after" not in S.session_speech_cut(A)
    assert "after" in S.session_speech_cut(B)      # its own session only


def test_a_cutoff_nobody_ends_lapses(monkeypatch):
    S.request_session_speech_cut(A, "after", at=time.time() - 10)
    assert S._speech_cut(A, time.time())
    monkeypatch.setenv("MEDIA_SPEECH_CUT_TTL_S", "0")
    assert not S._speech_cut(A, time.time())
    assert S.session_speech_cut(A) == {}


# --- all: this thread's queue -------------------------------------------------

def test_all_drops_a_reply_still_rendering_and_spares_the_next(monkeypatch):
    def slow(text, outfile, **_):
        if "slow" in text:
            time.sleep(0.6)
        return _render(text, outfile)

    monkeypatch.setattr(S, "render_text", slow)
    state, sink = StateStore(), _Sink()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault(
        "rid", _say("A slow reply, rendering when stop lands.", A, state, sink)))
    t.start()
    time.sleep(0.2)
    S.request_session_speech_cut(A, "all")
    t.join(timeout=20)
    assert sink.played == []
    assert _row(state, out["rid"])["extras"]["flushed"] is True
    # One-shot: what the session says after the stop is heard.
    rid = _say("Said after the stop.", A, state, sink)
    assert len(sink.played) == 1 and not _row(state, rid)["extras"].get("flushed")


def test_all_drops_a_queued_reply_while_another_thread_speaks():
    """B holds the voice; A's reply waits on the lock; the stop drops A's
    without cutting B's."""
    state = StateStore()
    release_b = threading.Event()
    order: list[str] = []

    class _Held(_Sink):
        def play(self, uri, target, **_):
            super().play(uri, target)
            order.append(uri)

        def idle(self, target):
            # B's one clip keeps playing until released.
            if not release_b.is_set():
                return False
            return super().idle(target)

    sink = _Held()
    rb = {}
    tb = threading.Thread(target=lambda: rb.setdefault(
        "rid", _say("Thread B is speaking.", B, state, sink)))
    tb.start()
    deadline = time.time() + 5
    while not sink.played and time.time() < deadline:
        time.sleep(0.05)
    ra = {}
    ta = threading.Thread(target=lambda: ra.setdefault(
        "rid", _say("Thread A waits its turn.", A, state, sink)))
    ta.start()
    deadline = time.time() + 5
    while not [q for q in S.speech_queue() if q["session"] == A] and time.time() < deadline:
        time.sleep(0.05)
    assert [q for q in S.speech_queue() if q["session"] == A], "A never queued"
    S.request_session_speech_cut(A, "all")
    release_b.set()
    tb.join(timeout=20)
    ta.join(timeout=20)
    assert len(order) == 1                       # B's clip only
    assert not _row(state, rb["rid"])["extras"].get("flushed")
    assert _row(state, ra["rid"])["extras"]["flushed"] is True


def test_all_ends_the_reply_playing_at_its_clip():
    state = StateStore()
    sink = _Sink(on_play=lambda n: n == 1 and S.request_session_speech_cut(A, "all"))
    rid = _say("First sentence here. Second sentence here. Third one too.", A, state, sink)
    assert len(sink.played) == 1, "the rest of the reply played after the stop"
    row = _row(state, rid)
    assert row["text"].startswith("First sentence")   # archived whole
    assert not row["extras"].get("flushed")           # it was heard, in part


def test_all_never_cuts_another_threads_reply_mid_play():
    state = StateStore()
    sink = _Sink(on_play=lambda n: n == 1 and S.request_session_speech_cut(A, "all"))
    _say("First sentence here. Second sentence here.", B, state, sink)
    assert len(sink.played) == 2


def test_a_cut_needs_a_session_and_a_known_mode():
    assert S.request_session_speech_cut("", "all") is None
    with pytest.raises(ValueError):
        S.request_session_speech_cut(A, "some")
    assert not S._speech_cut("", time.time())


# --- ask: the question was answered -------------------------------------------

def _ask(text, session, state, sink):
    return S.submit_event(Event(text=text, source=Source.CLAUDE_CODE,
                                metadata={"session": session, "kind": "notif",
                                          "ask": True}),
                          state=state, sink=sink, coordinator=_Coord())


def test_an_answer_ends_the_question_read_out_at_its_clip():
    state = StateStore()
    sink = _Sink(on_play=lambda n: n == 1 and S.request_session_speech_cut(A, "ask"))
    _ask("Which one? Option one. Option two. Option three.", A, state, sink)
    assert len(sink.played) == 1, "the options kept reading after the answer"


def test_an_answer_leaves_the_lead_in_and_the_reply_alone():
    state, sink = StateStore(), _Sink()
    S.request_session_speech_cut(A, "ask", at=time.time() + 60)
    _say("The lead-in. It sets up the question.", A, state, sink)
    assert len(sink.played) == 2
    rid = _ask("Which one? Option one.", A, state, sink)
    assert len(sink.played) == 2 and _row(state, rid)["extras"]["flushed"] is True
    _ask("Another thread asks. Option one.", B, state, sink)
    assert len(sink.played) > 2


def test_a_later_question_is_read_out():
    state, sink = StateStore(), _Sink()
    S.request_session_speech_cut(A, "ask")
    rid = _ask("The next question. Option one.", A, state, sink)
    assert sink.played and not _row(state, rid)["extras"].get("flushed")


# --- read: the listener replied -----------------------------------------------

def test_a_reply_ends_the_reply_playing_at_its_sentence():
    state = StateStore()
    sink = _Sink(on_play=lambda n: n == 1 and S.session_reply_read(A))
    rid = _say("First sentence here. Second sentence here. Third one too.", A, state, sink)
    assert len(sink.played) == 1, "the reply kept reading after it was answered"
    assert not _row(state, rid)["extras"].get("flushed")   # heard, in part


def test_a_reply_drops_what_was_queued_and_spares_what_it_starts():
    state, sink = StateStore(), _Sink()
    S.session_reply_read(A, at=time.time() + 60)
    old = _say("Queued before the reply.", A, state, sink)
    assert sink.played == [] and _row(state, old)["extras"]["flushed"] is True
    _say("Another thread entirely.", B, state, sink)
    assert len(sink.played) == 1


def test_the_reply_a_reply_starts_is_heard():
    state, sink = StateStore(), _Sink()
    S.session_reply_read(A)
    new = _say("The answer to the reply.", A, state, sink)
    assert len(sink.played) == 1 and not _row(state, new)["extras"].get("flushed")


def test_keep_reading_leaves_it_and_holds_off_the_hook_once():
    assert S.session_reply_read(A, keep=True) is None
    assert S.session_speech_cut(A) == {}
    # The prompt hook the same send sets off: left alone, and the keep is spent.
    assert S.session_reply_read(A) is None
    assert S.session_speech_cut(A) == {}
    assert S.session_reply_read(A) is not None
    assert "read" in S.session_speech_cut(A)


def test_a_keep_from_long_ago_is_not_this_send(monkeypatch):
    S.session_reply_read(A, keep=True)
    monkeypatch.setattr(S.time, "time", lambda: 10 ** 10)
    assert S.session_reply_read(A) is not None


def test_auto_speak_is_never_cut_by_a_reply():
    from agent_media_core import speak_priority

    speak_priority.set_level(A, "auto")
    assert S.session_reply_read(A) is None
    assert S.session_speech_cut(A) == {}


def test_the_read_check_can_be_left_to_the_caller():
    S.session_reply_read(A, at=time.time() + 60)
    now = time.time()
    assert S._speech_cut(A, now, playing=True)
    assert not S._speech_cut(A, now, playing=True, read=False)
    assert S._speech_read(A, now) and not S._speech_read(B, now)


def _hook_env(monkeypatch):
    seen = []

    class _BT:
        @staticmethod
        def record_listener_turn(session, text, extras=None):
            seen.append(text)
            return True

    import sys

    import agent_media_core
    monkeypatch.setattr(agent_media_core, "book_tracks", _BT, raising=False)
    monkeypatch.setitem(sys.modules, "agent_media_core.book_tracks", _BT)
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    return seen


def test_a_typed_turn_marks_the_reply_read(monkeypatch):
    from agent_media_core.intake import hook_claude_code as H

    _hook_env(monkeypatch)
    H._handle_user_prompt({"prompt": "got it, next", "session_id": A})
    assert "read" in S.session_speech_cut(A)
    assert S.session_speech_cut(B) == {}


def test_a_notice_or_a_settings_command_reads_nothing(monkeypatch):
    from agent_media_core.intake import hook_claude_code as H

    _hook_env(monkeypatch)
    H._handle_user_prompt({"prompt": "<command-name>/model</command-name>",
                           "session_id": A})
    H._handle_user_prompt({"prompt": "<task-notification>done</task-notification>",
                           "session_id": A})
    assert S.session_speech_cut(A) == {}


def test_claude_codes_hook_cuts_and_records_nothing(monkeypatch):
    import io
    import json

    from agent_media_core.intake import hook_claude_code as H

    seen = _hook_env(monkeypatch)
    monkeypatch.setattr(H, "load_env_file", lambda name: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": "hi", "session_id": A})))
    assert H.main() == 0
    assert seen == [] and "read" in S.session_speech_cut(A)
