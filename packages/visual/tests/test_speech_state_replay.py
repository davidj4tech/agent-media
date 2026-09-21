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


class _Done:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_a_failed_replay_says_why(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["timeout"] = argv, kw.get("timeout")
        return _Done(1, err="media replay: that reply's audio is no longer on "
                            "this host (cache cleared)\n")

    monkeypatch.setattr(canvas.subprocess, "run", run)
    out = canvas._speech_ctl("replay-id", 9222)
    assert out == "error: that reply's audio is no longer on this host (cache cleared)"
    assert seen["argv"][1:] == ["replay", "--id", "9222"]
    # Room for the replay to wait for a speaking reply to step aside.
    assert seen["timeout"] >= 20


def test_a_replay_that_worked_reads_as_before(monkeypatch):
    monkeypatch.setattr(canvas.subprocess, "run",
                        lambda argv, **kw: _Done(0, out="3\n"))
    assert canvas._speech_ctl("prev", 2) == "3"


def test_other_verbs_keep_the_short_path(monkeypatch):
    seen = []
    monkeypatch.setattr(canvas, "_media",
                        lambda args, timeout=10: seen.append(args) or "ok")
    assert canvas._speech_ctl("toggle", 1) == "ok"
    assert seen == [["toggle"]]


def test_a_replay_that_hangs_is_not_an_error(monkeypatch):
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    monkeypatch.setattr(canvas.subprocess, "run", run)
    assert canvas._speech_ctl("replay", 1) == ""
