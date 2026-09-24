"""The speech snapshot says what is HEARD: a replay as a replay, under its own
session, and a reply that arrived meanwhile as waiting (2026-09-22)."""

import subprocess

from agent_media_visual import canvas

OLD = "6c73498c-02c1-4846-8350-a82006973571"
NEW = "5f8ca313-c85f-469e-afc7-f3068bc2bfda"


def _speaking(monkeypatch, extras, queued=()):
    monkeypatch.setattr(canvas, "_media", lambda args, timeout=10: "▶ 00:02 / 00:30")
    monkeypatch.setattr(canvas, "_speech_extras", lambda: extras)
    monkeypatch.setattr(canvas, "_speech_queue", lambda: list(queued))


def test_a_replay_is_named_as_one_under_its_own_session(monkeypatch):
    _speaking(monkeypatch, {"replay": True, "source_session": OLD,
                            "current_sentence": "An older sentence."},
              queued=[{"session": NEW, "urgent": False, "at": 1790031449.7}])
    st = canvas.speech_state()
    assert st["session"] == OLD and st["replay"] is True
    assert st["sentence"] == "An older sentence."
    assert st["queued"] == [{"session": NEW, "urgent": False, "at": 1790031449.7}]


def test_a_live_reply_is_not_a_replay_and_nothing_waits(monkeypatch):
    _speaking(monkeypatch, {"source_session": NEW, "current_sentence": "New."})
    st = canvas.speech_state()
    assert st["session"] == NEW
    assert "replay" not in st and "queued" not in st


def test_the_queue_is_read_from_the_waiter_registry(monkeypatch):
    """Wired to core's speech_queue, and quiet when the registry is empty."""
    from agent_media_core.intake import submit
    monkeypatch.setattr(submit, "speech_queue", lambda: [{"session": NEW}])
    assert canvas._speech_queue() == [{"session": NEW}]

    def boom():
        raise OSError("gone")

    monkeypatch.setattr(submit, "speech_queue", boom)
    assert canvas._speech_queue() == []


class _Proc:
    """A `media` child: exits `rc` having written `out`/`err`, or hangs."""
    killed = False

    def __init__(self, rc, out="", err="", hang=False):
        self.rc, self.out, self.err, self.hang = rc, out, err, hang

    def __call__(self, argv, **kw):
        self.argv = argv
        kw["stdout"].write(self.out)
        kw["stderr"].write(self.err)
        return self

    def wait(self, timeout=None):
        self.timeout = timeout
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.rc

    def kill(self):
        self.killed = True


def test_a_failed_replay_says_why(monkeypatch):
    proc = _Proc(1, err="media replay: that reply's audio is no longer on "
                        "this host (cache cleared)\n")
    monkeypatch.setattr(canvas.subprocess, "Popen", proc)
    out = canvas._speech_ctl("replay-id", 9222)
    assert out == "error: that reply's audio is no longer on this host (cache cleared)"
    assert proc.argv[1:] == ["replay", "--id", "9222"]
    # Room for the replay to wait for a speaking reply to step aside.
    assert proc.timeout >= 20


def test_a_replay_that_worked_reads_as_before(monkeypatch):
    monkeypatch.setattr(canvas.subprocess, "Popen", _Proc(0, out="3\n"))
    assert canvas._speech_ctl("prev", 2) == "3"


def test_other_verbs_keep_the_short_path(monkeypatch):
    seen = []
    monkeypatch.setattr(canvas, "_media",
                        lambda args, timeout=10: seen.append(args) or "ok")
    assert canvas._speech_ctl("toggle", 1) == "ok"
    assert seen == [["toggle"]]


def test_a_slow_replay_is_left_to_finish(monkeypatch):
    """A long reply's push outlasts the wait: killing it cut the reply off
    and its follower (the bar, the follow-along) never started."""
    proc = _Proc(0, hang=True)
    monkeypatch.setattr(canvas.subprocess, "Popen", proc)
    assert canvas._speech_ctl("replay", 1) == ""
    assert not proc.killed


def test_the_snapshot_names_the_turn_being_spoken(monkeypatch):
    """`turn` keys the player to a transcript line (`at`, and `id` on a
    replay). A reader holding a line's sentences can then tell whether they
    are the ones being spoken — which is what lets the bold survive losing
    the live row."""
    _speaking(monkeypatch, {"source_session": NEW, "current_sentence": "New."})
    monkeypatch.setattr(canvas, "_speech_turn",
                        lambda: {"at": 1790031449.7, "id": 91})
    assert canvas.speech_state()["turn"] == {"at": 1790031449.7, "id": 91}


def test_nothing_playing_names_no_turn(monkeypatch):
    monkeypatch.setattr(canvas, "_media", lambda args, timeout=10: "")
    monkeypatch.setattr(canvas, "_speech_extras", lambda: {})
    monkeypatch.setattr(canvas, "_speech_queue", lambda: [])
    monkeypatch.setattr(canvas, "_speech_turn", lambda: {"at": 1.0})
    assert "turn" not in canvas.speech_state()


def test_the_turn_is_read_from_the_now_playing_row(monkeypatch, tmp_path):
    """Read from the row itself, not the extras — `started_at` is the key the
    log's lines carry, and a row with no history id (a fresh reply, not a
    replay) names only that."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from agent_media_core.state.store import StateStore

    st = StateStore()
    st.set_now_playing("speech", uri="/tmp/a.mp3", started_at=1790031449.7,
                       extras={"source_session": NEW})
    assert canvas._speech_turn() == {"at": 1790031449.7}

    st.set_now_playing("speech", uri="/tmp/a.mp3", started_at=1790031500.0,
                       extras={"source_session": NEW, "history_id": 91})
    assert canvas._speech_turn() == {"at": 1790031500.0, "id": 91}
