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


def test_play_detached_owns_its_state(monkeypatch):
    """The play path creates its OWN StateStore (in the detached child), so the
    parent never holds a WAL connection to inherit across the fork — the whole
    point of this regression file. dedup + stamp + submit all run on that store.
    """
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")  # inline: observable, no fork
    sentinel = object()
    monkeypatch.setattr(H, "StateStore", lambda *a, **k: sentinel)
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_write_stamp", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(H, "submit_event",
                        lambda event, state=None: seen.setdefault("state", state))

    H._play_detached(Event(text="hi", source=Source.CLAUDE_CODE))
    assert seen["state"] is sentinel  # submit ran on the store the child opened


def test_play_detached_dedup_skips_submit(monkeypatch):
    """A duplicate reply is dropped on the play path (dedup lives in the child
    now, not the parent hook), so submit_event is never reached."""
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    monkeypatch.setattr(H, "StateStore", lambda *a, **k: object())
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: True)  # seen recently
    monkeypatch.setattr(H, "_write_stamp", lambda *a, **k: None)
    called = {"n": 0}
    monkeypatch.setattr(H, "submit_event",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    H._play_detached(Event(text="dup", source=Source.CLAUDE_CODE))
    assert called["n"] == 0


def test_handle_stop_prefers_last_assistant_message(monkeypatch, tmp_path):
    """Stop speaks the payload's last_assistant_message directly (markdown
    stripped) without touching the transcript."""
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_write_stamp", lambda *a, **k: None)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    used = {"transcript": False}
    monkeypatch.setattr(H, "_latest_assistant_text",
                        lambda tp: used.__setitem__("transcript", True) or "")
    seen = {}
    monkeypatch.setattr(H, "submit_event",
                        lambda event, **k: seen.setdefault("event", event))

    rc = H._handle_stop({"last_assistant_message": "**Done** refactoring.",
                         "session_id": "s"})
    assert rc == 0
    assert used["transcript"] is False           # never read the JSONL
    assert "Done refactoring" in seen["event"].text and "**" not in seen["event"].text


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
