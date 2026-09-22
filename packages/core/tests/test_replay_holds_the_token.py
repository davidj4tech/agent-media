"""A replay holds the speech token, and what is reported is what is heard.

2026-09-22, 08:57, speech on the app: a replay of an older reply (▶ on a bubble)
and a new reply from another session reached the phone within two seconds of
each other. The replay held nothing — it pushed its playlist straight at the
player — so the new reply found the playback token free, took it, logged
`start`, and its follow loop wrote its own sentences into the now-playing row
every tick. The listener heard the replay; the speech bar and the follow-along
named the new reply.

Now a replay takes the token for as long as it plays (its follower holds it):
a new ordinary reply waits, writes nothing, and is listed as waiting; a speaking
reply steps aside for a replay and resumes after it (still paused, if it was);
a question or an urgent say still barges in. Fakes only: no audio, no sockets,
no phone, and the state dir is the test's own.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_media_core import cli
from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Priority, Source, Target

OLD = "6c73498c-02c1-4846-8350-a82006973571"     # the replayed reply's session
NEW = "5f8ca313-c85f-469e-afc7-f3068bc2bfda"     # the reply that arrives
APP = Target(name="app")


@pytest.fixture(autouse=True)
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_APP", "tcp://127.0.0.1:1")
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_APP", "")
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")
    monkeypatch.delenv("MEDIA_STREAM_CLIPS", raising=False)


@pytest.fixture
def sleeper():
    """A live process that is not this one: the stand-in for a replay's
    follower (its pid goes in writer_pid)."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield p
    p.kill()
    p.wait()


def _replay_token() -> S._SpeechPlaybackLock:
    lock = S._SpeechPlaybackLock(kind="replay")
    assert lock.take_within(1.0, rank=S._REPLAY_RANK, session="replay:test")
    return lock


def _replay_row(state: StateStore, writer_pid: int) -> None:
    state.set_now_playing("speech", uri="/clips/old-000.mp3", started_at=1.0,
                          target="app", extras={
                              "replay": True, "history_id": 9199,
                              "source_session": OLD, "writer_pid": writer_pid,
                              "current_sentence": "An older sentence.",
                              "clip_sentences": ["An older sentence."]})


def _events() -> list:
    p = S._speech_events_path()
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# --- the incident: replay audible, a new reply arrives ---------------------------

def _fake_render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


class _Coord:
    def pre_pause_remote(self): pass
    def before_speech(self, **_): pass
    def speaking_line(self, text=""): pass
    def after_speech(self): pass
    def release_music_duck(self): pass
    def reapply_music_duck(self): pass


class _PlaylistSink:
    """The app lane: plays its playlist through once, then idles."""

    def __init__(self, paused_at=None):
        self.ops: list = []
        snap = {"idle-active": False, "pause": False, "mute": False,
                "time-pos": 0.1, "playlist-count": 2}
        self.snaps = [dict(snap, **{"playlist-pos": 0}),
                      dict(snap, **{"playlist-pos": 1, "time-pos": 0.2}),
                      {"idle-active": True, "playlist-count": 2}]

    def prefetch(self, paths, target=None): return True
    def load_playlist(self, uris, target=None, gapless=True):
        self.ops.append("load")
        return True
    def start_playlist(self, target=None):
        self.ops.append("start")
        return True
    def play_playlist(self, uris, target=None, gapless=True):
        self.ops.append("play_playlist")
    def snapshot(self, target=None):
        return self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]
    def set_playlist_pos(self, pos, target=None): self.ops.append(("pos", pos))
    def pause(self, target=None): self.ops.append("pause")
    def stop(self, target=None): self.ops.append("stop")
    def muted(self, target=None): return False
    def active_other_owner(self, target=None): return None
    def claim_broker(self, target=None): return True
    def refresh_broker(self, target=None): pass
    def release_broker(self, target=None): pass


def test_a_reply_waits_for_a_replay_and_says_nothing_until_it_plays(monkeypatch, sleeper):
    monkeypatch.setattr(S, "render_text", _fake_render)
    state = StateStore()
    token = _replay_token()
    _replay_row(state, sleeper.pid)

    written: list = []
    real_set = StateStore.set_now_playing

    def spy(self, sink, **kw):
        written.append((kw.get("extras") or {}).get("source_session"))
        return real_set(self, sink, **kw)

    monkeypatch.setattr(StateStore, "set_now_playing", spy)
    sink = _PlaylistSink()
    ev = Event(text="The queue is updated. Where things stand.",
               source=Source.CLAUDE_CODE, target=APP,
               metadata={"pane": "%604", "session": NEW})
    done: dict = {}
    t = threading.Thread(target=lambda: done.setdefault(
        "rid", S.submit_event(ev, state=state, sink=sink, coordinator=_Coord())))
    t.start()
    try:
        # It asks for the token, and is listed as waiting for it.
        assert _wait_for(lambda: S.speech_queue() != [])
        assert S.speech_queue() == [{"session": NEW, "urgent": False,
                                     "at": pytest.approx(time.time(), abs=10)}]
        time.sleep(0.5)       # several of its polls
        # ...and while it waits, the replay is what the row says is heard.
        np = state.get_now_playing("speech")
        assert np["extras"]["replay"] is True
        assert np["extras"]["source_session"] == OLD
        assert NEW not in written, "a waiting reply wrote the now-playing row"
        assert not any(e["event"] == "start" for e in _events())
        assert sink.ops == [], "a waiting reply touched the player"
    finally:
        # The replay ends: its follower clears its row and exits.
        state.clear_now_playing("speech")
        token.release()
    t.join(timeout=10)
    assert not t.is_alive()
    # Then the reply plays, and is reported while it does.
    assert done["rid"] is not None
    assert NEW in written
    assert [e["event"] for e in _events()] == ["start", "end"]
    assert S.speech_queue() == []


def test_a_reply_does_not_write_over_an_audible_replay(monkeypatch, sleeper):
    """Belt and braces for a replay that could not get the token in time and
    pushed over a speaking reply: the reply's follow loop leaves the replay's
    row alone rather than naming itself over it."""
    monkeypatch.setattr(S, "render_text", _fake_render)
    state = StateStore()
    written: list = []
    real_set = StateStore.set_now_playing

    def spy(self, sink, **kw):
        written.append((kw.get("extras") or {}).get("source_session"))
        return real_set(self, sink, **kw)

    monkeypatch.setattr(StateStore, "set_now_playing", spy)
    sink = _PlaylistSink()
    real_play = sink.play_playlist

    def play_then_replay(uris, target=None, gapless=True):
        real_play(uris, target, gapless)
        _replay_row(state, sleeper.pid)      # the replay lands just after
        return True

    sink.play_playlist = play_then_replay
    sink.load_playlist = lambda *a, **k: False     # no early load: play_playlist
    ev = Event(text="First one here. Second one here.",
               source=Source.CLAUDE_CODE, target=APP,
               metadata={"pane": "%604", "session": NEW})
    S.submit_event(ev, state=state, sink=sink, coordinator=_Coord())
    assert OLD in written
    assert NEW not in written[written.index(OLD):], \
        "the reply wrote its sentences over the replay the listener was hearing"


def test_replay_audible_is_about_another_live_follower():
    assert not S._replay_is_audible({})
    assert not S._replay_is_audible({"writer_pid": os.getpid()})
    assert not S._replay_is_audible({"replay": True, "writer_pid": os.getpid()})
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert not S._replay_is_audible({"replay": True, "writer_pid": dead.pid})


# --- who gets the voice --------------------------------------------------------

def _waiter(rank: int, session: str, speaker: str = "") -> S._SpeechPlaybackLock:
    w = S._SpeechPlaybackLock(speaker=speaker)
    w._rank, w._session, w._seq = rank, session, time.time()
    w._register()
    return w


def test_ordinary_replies_wait_for_a_replay_and_questions_do_not():
    token = _replay_token()
    try:
        normal = _waiter(S._PRIO_RANK[Priority.NORMAL], "%1", NEW)
        # A reply that stepped aside once re-queues a notch up — still polite.
        resumed = _waiter(S._PRIO_RANK[Priority.NORMAL] + 9, "%2", NEW)
        assert not token.should_yield()
        high = _waiter(S._PRIO_RANK[Priority.HIGH], "%3", NEW)
        assert token.should_yield()
        high._unregister()
        urgent = _waiter(S._PRIO_RANK[Priority.URGENT], "%1", NEW)
        assert token.should_yield()
        for w in (normal, resumed, urgent):
            w._unregister()
    finally:
        token.release()


def test_a_speaking_reply_steps_aside_for_a_replay():
    """The follow loop's check (`should_yield`) sees a replay waiting."""
    reply = S._SpeechPlaybackLock(speaker=NEW)
    reply.acquire(Priority.NORMAL, session="%604", seq=time.time())
    try:
        assert not reply.should_yield()
        replay = S._SpeechPlaybackLock(kind="replay")
        got: dict = {}
        t = threading.Thread(target=lambda: got.setdefault(
            "held", replay.take_within(5.0, rank=S._REPLAY_RANK,
                                       session="replay:t")))
        t.start()
        assert _wait_for(reply.should_yield)
        reply.release()                  # what yield_to_higher does first
        t.join(timeout=5)
        assert got["held"] is True
        # A replay waiting for the token is not a "reply waiting".
        assert S.speech_queue() == []
        replay.release()
    finally:
        reply.release()


def test_a_replay_gives_up_waiting_and_plays_anyway():
    """A holder that cannot step aside (before its first clip, or blind) does
    not make a keypress hang: the replay stops waiting, as before."""
    holder = S._SpeechPlaybackLock()
    holder.acquire(Priority.NORMAL, session="%1", seq=time.time())
    try:
        started = time.monotonic()
        replay = S._SpeechPlaybackLock(kind="replay")
        assert replay.take_within(0.3, rank=S._REPLAY_RANK,
                                  session="replay:t") is False
        assert time.monotonic() - started < 2.0
        assert replay.fileno is None
    finally:
        holder.release()


def test_a_question_arriving_with_a_replay_goes_first():
    high = _waiter(S._PRIO_RANK[Priority.HIGH], "%3", NEW)
    try:
        replay = S._SpeechPlaybackLock(kind="replay")
        assert replay.take_within(0.2, rank=S._REPLAY_RANK,
                                  session="replay:t") is False
    finally:
        high._unregister()


# --- the queue the app shows ----------------------------------------------------

def test_the_queue_lists_waiting_replies_urgent_first(tmp_path):
    a = _waiter(S._PRIO_RANK[Priority.NORMAL], "%1", NEW)
    time.sleep(0.01)
    b = _waiter(S._PRIO_RANK[Priority.HIGH], "%2", OLD)
    pending = S._SpeechPlaybackLock(speaker="rendering")
    pending.announce(Priority.NORMAL, session="%9", seq=time.time())
    replay = _waiter(S._REPLAY_RANK, "replay:x")
    replay._kind = "replay"
    replay._register()
    anon = _waiter(S._PRIO_RANK[Priority.NORMAL], "")
    try:
        q = S.speech_queue()
        assert [(r["session"], r["urgent"]) for r in q] == \
            [(OLD, True), (NEW, False), (None, False)]
        assert all(set(r) == {"session", "urgent", "at"} for r in q)
    finally:
        for w in (a, b, pending, replay, anon):
            w._unregister()
    assert S.speech_queue() == []


def test_an_older_waiter_file_still_reads():
    d = S._speech_wait_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{os.getpid()}.old").write_text("10\n123.5\n%1\n0")
    try:
        assert S.speech_queue() == [{"session": None, "urgent": False, "at": 123.5}]
    finally:
        (d / f"{os.getpid()}.old").unlink()


def test_a_dead_waiter_is_not_waiting():
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    d = S._speech_wait_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{dead.pid}.x").write_text(f"10\n1.0\n%1\n0\n{NEW}\nreply")
    assert S.speech_queue() == []


# --- the replay command hands the token to its follower -------------------------

class _ReplaySink:
    def __init__(self):
        self.pushed: list = []

    def prefetch(self, paths, target=None): return True
    def play_playlist(self, uris, target=None, **kw): self.pushed.append(list(uris))
    def play(self, uri, target=None, **kw): self.pushed.append([uri])


@pytest.fixture
def replay_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "app")
    sink = _ReplaySink()
    monkeypatch.setattr(cli, "SinkSpeech", lambda: sink)
    monkeypatch.setattr(cli, "_replay_visual", lambda ex: None)
    monkeypatch.setattr(cli._speech_sink, "set_media_title", lambda *a, **k: None)
    monkeypatch.setattr(cli._speech_sink, "set_reply_text", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_caller_pane", lambda: "")
    followers: list = []
    real_popen = subprocess.Popen

    def fake_popen(argv, **kw):
        # The follower, minus the following: a process that just keeps the
        # descriptor it was handed, as `replay-track` does while the replay plays.
        p = real_popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             pass_fds=kw.get("pass_fds") or (),
                             start_new_session=True)
        followers.append((argv, kw, p))
        return p

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    clips = []
    for i in range(2):
        c = tmp_path / f"old-{i:03d}.mp3"
        c.write_bytes(b"x")
        clips.append(str(c))
    row = {"id": 9199, "uri": clips[0], "target": "app",
           "text": "An older sentence. And another.",
           "extras": {"clip_uris": clips, "clip_durations_s": [1.0, 1.0],
                      "clip_sentences": ["An older sentence.", "And another."],
                      "source_session": OLD, "source_pane": "%12"}}
    yield sink, followers, row
    for _, _, p in followers:
        p.kill()
        p.wait()


def test_the_replay_row_names_the_replay(replay_env):
    sink, followers, row = replay_env
    assert cli._replay_row(row) == 0
    assert sink.pushed == [row["extras"]["clip_uris"]]
    np = StateStore().get_now_playing("speech")
    ex = np["extras"]
    assert ex["replay"] is True and ex["history_id"] == 9199
    assert ex["source_session"] == OLD
    assert ex["current_sentence"] == "An older sentence."
    assert ex["writer_pid"] == followers[0][2].pid


def test_the_token_outlives_the_replay_command(replay_env):
    _, followers, row = replay_env
    assert cli._replay_row(row) == 0
    argv, kw, follower = followers[0]
    assert "--lock-fd" in argv and kw["pass_fds"]
    # The command has returned; its follower still holds the token.
    probe = S._SpeechPlaybackLock()
    assert probe.take_within(0.3, rank=S._PRIO_RANK[Priority.NORMAL],
                             session="%1") is False
    follower.kill()
    follower.wait()
    assert probe.take_within(1.0, rank=S._PRIO_RANK[Priority.NORMAL],
                             session="%1") is True
    probe.release()


def test_a_second_replay_supersedes_the_first_follower(replay_env):
    _, followers, row = replay_env
    assert cli._replay_row(row) == 0
    assert cli._replay_row(dict(row, id=9200)) == 0
    first, second = followers[0][2], followers[1][2]
    assert first.wait(timeout=5) is not None, "the first follower was left running"
    assert second.poll() is None
    assert StateStore().get_now_playing("speech")["extras"]["history_id"] == 9200


def test_a_restart_of_the_live_turn_takes_no_token(replay_env):
    """`<` mid-reply replays the turn that is speaking (no id yet). Its own
    follow loop holds the token; asking for it would speak the turn twice."""
    _, followers, row = replay_env
    live = dict(row, id=None, started_at=123.0)
    assert cli._replay_row(live) == 0
    argv, kw, _ = followers[0]
    assert "--lock-fd" not in argv and not kw.get("pass_fds")
    ex = StateStore().get_now_playing("speech")["extras"]
    assert "replay" not in ex and ex["replay_of_started_at"] == 123.0


def test_a_replay_whose_audio_is_gone_says_so(replay_env, capsys):
    sink, followers, row = replay_env
    Path(row["extras"]["clip_uris"][1]).unlink()
    assert cli._replay_row(row) == 1
    assert "no longer on this host" in capsys.readouterr().err
    assert sink.pushed == [] and followers == []


def test_a_far_side_render_for_another_player_says_so(replay_env, capsys):
    sink, followers, row = replay_env
    remote = dict(row, target="phone",
                  extras=dict(row["extras"], clips_remote=True,
                              clip_uris=["remote-1.mp3"],
                              clip_durations_s=[2.0]))
    assert cli._replay_row(remote) == 1
    assert "lives on phone" in capsys.readouterr().err
    assert sink.pushed == []


def test_the_follower_takes_the_descriptor_on_its_command_line():
    a = cli._build_parser().parse_args(
        ["replay-track", "--durations", "[1.0]", "--lock-fd", "7"])
    assert a.lock_fd == 7
    a = cli._build_parser().parse_args(["replay-track", "--durations", "[1.0]"])
    assert a.lock_fd == -1


# --- the follower gives the voice to a question ---------------------------------

def test_the_follower_stops_the_replay_for_a_question(monkeypatch):
    token = _replay_token()
    fd = token.fileno
    state = StateStore()
    state.set_now_playing("speech", uri="/c.mp3", started_at=time.time(),
                          target="app", extras={"replay": True,
                                                "writer_pid": os.getpid()})
    stopped: list = []
    monkeypatch.setattr(cli.ipc, "command",
                        lambda sock, *args, **k: stopped.append(args))
    high = _waiter(S._PRIO_RANK[Priority.HIGH], "%3", NEW)
    try:
        started = time.monotonic()
        rc = cli.cmd_replay_track(argparse.Namespace(
            sentences=json.dumps(["One.", "Two."]), offsets=json.dumps([0.0, 30.0]),
            pane="", durations=json.dumps([60.0]), lock_fd=fd))
        assert rc == 0
        assert time.monotonic() - started < 5, "the replay did not give way"
        assert ("stop",) in stopped
        assert state.get_now_playing("speech") is None
    finally:
        high._unregister()
        token.release()


def test_the_follower_keeps_the_voice_from_an_ordinary_reply(monkeypatch):
    """An ordinary reply waiting does not end the replay: it runs to the end
    of its timeline."""
    token = _replay_token()
    state = StateStore()
    state.set_now_playing("speech", uri="/c.mp3", started_at=time.time(),
                          target="app", extras={"replay": True,
                                                "writer_pid": os.getpid()})
    stopped: list = []
    monkeypatch.setattr(cli.ipc, "command",
                        lambda sock, *args, **k: stopped.append(args))
    normal = _waiter(S._PRIO_RANK[Priority.NORMAL], "%1", NEW)
    try:
        started = time.monotonic()
        cli.cmd_replay_track(argparse.Namespace(
            sentences=json.dumps(["One.", "Two."]), offsets=json.dumps([0.0, 0.2]),
            pane="", durations=json.dumps([0.6]), lock_fd=token.fileno))
        assert time.monotonic() - started >= 0.5
        assert stopped == []
    finally:
        normal._unregister()
        token.release()


def test_the_follower_stops_the_replay_for_end_of_reply(monkeypatch):
    """End of reply on the phone lane hands the follower a past-the-end jump;
    it stops the replay and ends, as for a question."""
    state = StateStore()
    state.set_now_playing("speech", uri="/c.mp3", started_at=time.time(),
                          target="app", extras={"replay": True,
                                                "writer_pid": os.getpid()})
    stopped: list = []
    monkeypatch.setattr(cli.ipc, "command",
                        lambda sock, *args, **k: stopped.append(args))
    S._nav_flag_path(APP).write_text(str(sys.maxsize))
    started = time.monotonic()
    rc = cli.cmd_replay_track(argparse.Namespace(
        sentences=json.dumps(["One.", "Two."]), offsets=json.dumps([0.0, 30.0]),
        pane="", durations=json.dumps([60.0]), lock_fd=-1))
    assert rc == 0
    assert time.monotonic() - started < 5, "End did not end the replay"
    assert ("stop",) in stopped
    assert state.get_now_playing("speech") is None
    assert not S._nav_flag_path(APP).exists()


def test_the_follower_leaves_a_sentence_step_alone(monkeypatch):
    """A jump to a sentence is not End: the follower plays on and leaves it."""
    state = StateStore()
    state.set_now_playing("speech", uri="/c.mp3", started_at=time.time(),
                          target="app", extras={"replay": True,
                                                "writer_pid": os.getpid()})
    stopped: list = []
    monkeypatch.setattr(cli.ipc, "command",
                        lambda sock, *args, **k: stopped.append(args))
    S._nav_flag_path(APP).write_text("1")
    cli.cmd_replay_track(argparse.Namespace(
        sentences=json.dumps(["One.", "Two."]), offsets=json.dumps([0.0, 0.2]),
        pane="", durations=json.dumps([0.6]), lock_fd=-1))
    assert stopped == []
    assert S._nav_flag_path(APP).read_text() == "1"


# --- a reply that was paused comes back paused ----------------------------------

class _PausedThenYieldSink(_PlaylistSink):
    def __init__(self):
        super().__init__()
        snap = {"idle-active": False, "mute": False, "time-pos": 0.1,
                "playlist-count": 2, "playlist-pos": 0}
        self.snaps = [dict(snap, pause=False), dict(snap, pause=True),
                      {"idle-active": True, "playlist-count": 2}]


def test_a_paused_reply_resumes_paused_after_a_replay(monkeypatch):
    monkeypatch.setattr(S, "render_text", _fake_render)
    calls = {"n": 0}

    def should_yield(self):
        calls["n"] += 1
        return calls["n"] == 3          # after the tick that read "paused"

    monkeypatch.setattr(S._SpeechPlaybackLock, "should_yield", should_yield)
    monkeypatch.setattr(S._SpeechPlaybackLock, "yield_to_higher", lambda self: None)
    sink = _PausedThenYieldSink()
    ev = Event(text="First one here. Second one here.",
               source=Source.CLAUDE_CODE, target=APP,
               metadata={"pane": "%604", "session": NEW})
    S.submit_event(ev, state=StateStore(), sink=sink, coordinator=_Coord())
    assert "stop" in sink.ops
    after = sink.ops[sink.ops.index("stop"):]
    assert "play_playlist" in after and "pause" in after
    assert after.index("pause") > after.index("play_playlist")
