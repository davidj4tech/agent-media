"""Regression: the detached Stop-playback child must not inherit the parent
hook's open SQLite WAL connection.

When it did, the inherited WAL handle corrupted the child's wal-index lock
arbitration: the parent's exit (and unrelated short-lived reader processes)
checkpointed and unlinked the ``-wal``/``-shm`` out from under the still-running
child. The child's ``now_playing`` writes then landed in an orphaned, already
deleted WAL — invisible to every fresh reader — which surfaced as the grey
per-sentence status bar, the wrong popup subject pane, and ``goto`` claiming a
live pane was "already closed". The fix is to release the parent's connection
before forking; see ``StateStore.close`` and ``_play_detached``.
"""

import os
import subprocess

from agent_media_core.intake import hook_claude_code as H
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source


def _dead_pid() -> int:
    """A pid that is guaranteed not to be a live process (reaped child)."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_close_releases_then_reopens(tmp_path):
    db = tmp_path / "state.db"
    store = StateStore(db)
    store.set_now_playing("speech", uri="clip-0", started_at=1.0)
    # An op opened a thread-local connection.
    assert getattr(store._local, "conn", None) is not None
    store.close()
    assert getattr(store._local, "conn", None) is None
    # Reopens lazily and still sees the committed row.
    assert store.get_now_playing("speech")["uri"] == "clip-0"


def test_play_detached_closes_caller_state_before_fork(monkeypatch):
    """The caller's StateStore is closed before any fork/inline submit."""
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")  # inline: observable, no fork
    monkeypatch.setattr(H, "submit_event", lambda *a, **k: "rid")

    closed = {"n": 0}

    class SpyState:
        def close(self):
            closed["n"] += 1

    H._play_detached(Event(text="hi", source=Source.CLAUDE_CODE),
                     state=SpyState())
    assert closed["n"] == 1


# --- orphan guard: a row whose writer process is gone must not be shown ------

def test_orphaned_speech_row_is_hidden_and_cleared(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.set_now_playing("speech", uri="clip-0", started_at=1.0,
                          extras={"writer_pid": _dead_pid(),
                                  "total_duration_s": 9.0})
    # Writer is gone → the row is treated as absent...
    assert store.get_now_playing("speech") is None
    # ...and self-healed out of the table.
    with store._cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM now_playing WHERE sink='speech'")
        assert cur.fetchone()[0] == 0


def test_live_writer_row_is_returned(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.set_now_playing("speech", uri="clip-0", started_at=1.0,
                          extras={"writer_pid": os.getpid(),
                                  "total_duration_s": 9.0})
    np = store.get_now_playing("speech")
    assert np is not None and np["uri"] == "clip-0"


def test_row_without_writer_pid_is_untouched(tmp_path):
    # Music/book/legacy rows carry no writer_pid and must never be guarded.
    store = StateStore(tmp_path / "state.db")
    store.set_now_playing("music", uri="track-1", started_at=1.0,
                          content_type="music")
    np = store.get_now_playing("music")
    assert np is not None and np["uri"] == "track-1"
