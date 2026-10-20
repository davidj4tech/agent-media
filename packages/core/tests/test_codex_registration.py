"""Shared-daemon Codex discovery uses an exact first-prompt association."""
import sqlite3

from agent_media_core import harnesses, native_models

SID = "11111111-2222-3333-4444-555555555555"


def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr(native_models, "home", lambda *a: tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with sqlite3.connect(tmp_path / "state_5.sqlite") as conn:
        conn.execute("CREATE TABLE threads (id, cwd, first_user_message, created_at)")
        conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (SID, "/project", "hello", 100))
    real_glob = harnesses.glob.glob
    monkeypatch.setattr(harnesses.glob, "glob",
                        lambda pattern: ["/proc/123"] if pattern.startswith("/proc/") else real_glob(pattern))
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["/bin/codex"])
    monkeypatch.setattr(harnesses, "_pane_of", lambda pid: "%5")


def test_matches_and_registers_live_tui(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    assert harnesses.register_new_codex("%5", "hello", "/project", 100) == SID
    assert harnesses._registered()[0].session == SID
    assert harnesses.running()[0].pane == "%5"


def test_refuses_wrong_prompt_directory_age_or_pane(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    for args in [("%5", "other", "/project", 100),
                 ("%5", "hello", "/elsewhere", 100),
                 ("%5", "hello", "/project", 101),
                 ("%6", "hello", "/project", 100)]:
        assert harnesses.register_new_codex(*args) == ""


def test_refuses_ambiguous_match_and_daemon(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["/bin/codex", "app-server"])
    assert harnesses.register_new_codex("%5", "hello", "/project", 100) == ""
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["/bin/codex"])
    with sqlite3.connect(tmp_path / "state_5.sqlite") as conn:
        conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                     ("22222222-2222-3333-4444-555555555555", "/project", "hello", 100))
    assert harnesses.register_new_codex("%5", "hello", "/project", 100) == ""
