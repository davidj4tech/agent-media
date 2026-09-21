"""Archived threads (`archive.py`) and per-session memory (`procmem.py`).

Both surface on the thread list (server-contract.md §6.1, §6.4): `archived`
on every `/targets` row, `POST /session/archive` to set it, and `mem_mb` per
`/sessions/state` row with a `host` block. Over real HTTP with the contract
rig; the state dir and the /proc root are throwaway (conftest), and anything
that would type goes to the `typed` recorder.
"""

from __future__ import annotations

import json

import pytest

from agent_media_server import app, archive, procmem, send, sessions

from test_contract import (AUTH, SID, SID2, call, server, shelf,  # noqa: F401
                           signed_in, typed)


def _row(obj: dict, sid: str) -> dict:
    return {r["session"]: r for r in obj["sessions"]}[sid]


# --- archive ------------------------------------------------------------------------

def test_archive_toggles_and_the_row_stays_listed(server, shelf, signed_in, typed):
    res, obj = call(server, "POST", "/session/archive", {"session": SID, "archived": True}, AUTH)
    assert res.status == 200, obj
    assert obj == {"ok": True, "session": SID, "archived": True}
    _, targets = call(server, "GET", "/targets", headers=AUTH)
    assert _row(targets, SID)["archived"] is True       # still listed, flagged
    assert _row(targets, SID2)["archived"] is False
    _, conv = call(server, "GET", "/conversations", headers=AUTH)
    assert _row(conv, SID)["archived"] is True
    res, obj = call(server, "POST", "/session/archive", {"session": SID, "archived": False}, AUTH)
    assert obj == {"ok": True, "session": SID, "archived": False}
    _, targets = call(server, "GET", "/targets", headers=AUTH)
    assert _row(targets, SID)["archived"] is False


def test_archiving_ends_nothing(server, shelf, signed_in, typed):
    res, obj = call(server, "POST", "/session/archive", {"session": SID2}, AUTH)
    assert res.status == 200 and obj["archived"] is True    # absent means archive
    assert typed == []                                     # no kill-pane, no keys
    _, targets = call(server, "GET", "/targets", headers=AUTH)
    assert _row(targets, SID2)["live"] is True


def test_archive_persists_on_disk(server, shelf, signed_in, monkeypatch):
    call(server, "POST", "/session/archive", {"session": SID, "archived": True}, AUTH)
    data = json.loads(archive._path().read_text())
    assert set(data) == {SID} and isinstance(data[SID], float)
    # A fresh process (no parsed copy in memory) reads the same answer.
    monkeypatch.setattr(archive, "_CACHE", (None, {}))
    assert archive.is_archived(SID)
    # And a change made by another process is seen on the next read.
    archive._path().write_text(json.dumps({SID2: 1.0}))
    assert archive.archived() == {SID2: 1.0}


def test_archive_refusals(server, shelf, signed_in, monkeypatch):
    res, obj = call(server, "POST", "/session/archive", {"session": "nope"}, AUTH)
    assert res.status == 400 and obj == {"ok": False, "error": "not a session id"}
    res, obj = call(server, "POST", "/session/archive", {"session": SID, "archived": "yes"}, AUTH)
    assert res.status == 400 and obj["error"] == "archived must be true or false"
    other = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(sessions, "session_exists", lambda s: False)
    res, obj = call(server, "POST", "/session/archive", {"session": other}, AUTH)
    assert res.status == 404 and obj == {"ok": False, "error": "no such session 11111111"}
    assert not archive._path().exists()


def test_archive_is_gated(server, shelf, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, obj = call(server, "POST", "/session/archive", {"session": SID})
    assert res.status == 401 and obj["ok"] is False
    assert not archive._path().exists()


def test_archive_is_open_to_the_web_client():
    assert "/session/archive" in app.CORS_PATHS


def test_a_reply_unarchives_the_thread(server, shelf, signed_in, typed, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    archive.set_archived(SID, True)
    res, obj = call(server, "POST", "/reply", {"session": SID, "text": "back to this"}, AUTH)
    assert res.status == 200, obj
    assert ("_send_to_pane", ("%42", "back to this")) in typed
    assert not archive.is_archived(SID)


def test_a_routed_ask_unarchives_the_thread(server, shelf, signed_in, typed):
    archive.set_archived(SID2, True)
    res, obj = call(server, "POST", "/ask", {"text": "hello", "target": SID2}, AUTH)
    assert res.status == 200, obj
    assert ("_send_to_pane", ("%42", "hello")) in typed
    assert not archive.is_archived(SID2)


def test_a_send_that_failed_leaves_it_archived(server, shelf, signed_in, typed, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    monkeypatch.setattr(send, "_ensure_submitted", lambda *a, **k: False)
    archive.set_archived(SID, True)
    res, _obj = call(server, "POST", "/reply", {"session": SID, "text": "lost"}, AUTH)
    assert res.status == 502
    assert archive.is_archived(SID)


def test_set_archived_only_writes_on_a_change():
    assert archive.set_archived(SID, False) is False        # nothing to clear
    assert not archive._path().exists()
    assert archive.set_archived(SID, True) is True
    assert archive.set_archived(SID, True) is False


# --- memory -------------------------------------------------------------------------

PAGE = 4096


def _proc(root, pid: int, ppid: int, pages: int, comm: str = "node") -> None:
    d = root / str(pid)
    d.mkdir()
    # The command name may hold spaces and ")" — the parser counts from the last ")".
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560 0 0\n")
    (d / "statm").write_text(f"{pages * 3} {pages} 100 10 0 50 0\n")


@pytest.fixture()
def fake_proc(monkeypatch):
    """Two sessions' trees and a bystander, in the conftest's fake /proc.

    100 claude (100 MB) → 101 an MCP server (50 MB) → 102 its child (25 MB)
    200 codex (10 MB), no children
    300 an unrelated process (500 MB), child of init — never counted
    """
    root = procmem.PROC
    mb = 1024 * 1024 // PAGE
    _proc(root, 1, 0, 1, "init")
    _proc(root, 100, 1, 100 * mb, "claude")
    _proc(root, 101, 100, 50 * mb, "node) (mcp server")
    _proc(root, 102, 101, 25 * mb, "sh")
    _proc(root, 200, 1, 10 * mb, "codex")
    _proc(root, 300, 1, 500 * mb, "firefox")
    (root / "meminfo").write_text("MemTotal:        8054400 kB\nMemFree:  100 kB\n"
                                  "MemAvailable:    1468000 kB\n")
    monkeypatch.setattr(procmem, "_page_size", lambda: PAGE)
    return root


def test_a_session_counts_its_whole_process_tree(fake_proc):
    got = procmem.tree_mem_mb({"a": 100, "b": 200, "gone": 999, "unknown": None})
    assert got == {"a": 175, "b": 10, "gone": None, "unknown": None}


def test_an_unreadable_root_is_unknown_and_a_vanished_child_is_skipped(fake_proc):
    (fake_proc / "200" / "statm").unlink()
    (fake_proc / "102" / "statm").unlink()
    assert procmem.tree_mem_mb({"a": 100, "b": 200}) == {"a": 150, "b": None}


def test_host_memory_from_meminfo(fake_proc):
    assert procmem.host_mem() == {"mem_total_mb": 7866, "mem_available_mb": 1434}
    (fake_proc / "meminfo").write_text("MemTotal: 8054400 kB\n")
    assert procmem.host_mem() == {"mem_total_mb": 7866, "mem_available_mb": None}


def test_sessions_state_carries_memory_and_the_host(server, shelf, signed_in, fake_proc,
                                                    monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID2: "%42", SID: "%43"})
    # What the real sweep would have recorded; the fake live_sessions cannot.
    monkeypatch.setattr(sessions, "_PIDS", {SID2: 100, SID: 200})
    res, obj = call(server, "GET", "/sessions/state", headers=AUTH)
    assert res.status == 200, obj
    assert {r["session"]: r["mem_mb"] for r in obj["sessions"]} == {SID2: 175, SID: 10}
    assert obj["host"] == {"mem_total_mb": 7866, "mem_available_mb": 1434,
                           "sessions_mem_mb": 185}


def test_live_sessions_records_the_pids_it_found(monkeypatch):
    """The pid comes from the same sweep that finds the session — no second walk."""
    from types import SimpleNamespace

    from agent_media_core import harnesses

    monkeypatch.setattr(sessions.glob, "glob", lambda pattern: [])
    monkeypatch.setattr(harnesses, "running", lambda: [
        SimpleNamespace(pid=4242, session=SID, pane="%9", harness="codex")])
    assert sessions.live_sessions() == {SID: "%9"}
    assert sessions._PIDS == {SID: 4242}
