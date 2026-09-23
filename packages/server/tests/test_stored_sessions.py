"""Every harness's conversations in the one list (§6.16).

A thread the app knew about used to be one of three things: a pane with an
agent in it, a session sessiond drives, or a conversation that spoke and
reached the library. A Codex thread from this morning that said nothing was
none of them, and so was invisible. `sessions_index` now reads each
harness's own store as well, tags every row with the agent that holds it,
and keeps the list short with a window and a per-harness cap.
"""

import json
import os
import time

import pytest

from agent_media_server import sessions

CL = "3d1b4a2c-1111-4222-8333-444455556666"
CX = "01a0a6f1-ed4b-7f91-8d63-a61653a846f9"
LIVE = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"


@pytest.fixture()
def stores(monkeypatch, tmp_path):
    """A Claude conversation and a Codex one, neither running nor shelved."""
    claude = tmp_path / "claude" / "projects" / "-home-x-scratch"
    claude.mkdir(parents=True)
    (claude / f"{CL}.jsonl").write_text(json.dumps(
        {"type": "user", "cwd": "/home/x/scratch",
         "message": {"role": "user", "content": "how long is a piece of string"}}) + "\n")
    day = tmp_path / "codex" / "sessions" / "2026" / "09" / "16"
    day.mkdir(parents=True)
    (day / f"rollout-2026-09-16T07-21-07-{CX}.jsonl").write_text("\n".join(map(json.dumps, [
        {"type": "session_meta", "payload": {"id": CX, "cwd": "/home/x/scratch"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text",
                                                           "text": "what broke the build"}]}},
    ])) + "\n")
    shelf = tmp_path / "book-tracks"
    shelf.mkdir()
    monkeypatch.setattr(sessions, "_manifest_dir", lambda: shelf)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    monkeypatch.setattr(sessions, "_pane_titles", lambda: {})
    monkeypatch.setattr(sessions, "session_cwd", lambda s: "/home/x/scratch")
    return tmp_path


def _rows(**kw):
    return {r["session"]: r for r in sessions.sessions_index(**kw)}


def test_a_conversation_that_never_spoke_is_still_a_thread(stores):
    rows = _rows()
    assert set(rows) == {CL, CX}
    assert rows[CX]["harness"] == "codex" and rows[CL]["harness"] == "claude"
    assert rows[CX]["title"] == "what broke the build"
    assert rows[CL]["title"] == "how long is a piece of string"
    # Shaped like any other closed row, and marked as coming from the store.
    assert rows[CX]["live"] is False and rows[CX]["pane"] is None
    assert rows[CX]["source"] == "store"
    assert rows[CX]["archived"] is False and rows[CX]["pinned"] is False
    assert rows[CX]["at"] > 0 and rows[CX]["recap"] is None


def test_the_window_and_the_cap_keep_the_list_short(stores, monkeypatch):
    old = time.time() - 40 * 86400
    os.utime(stores / "claude" / "projects" / "-home-x-scratch" / f"{CL}.jsonl", (old, old))
    assert set(_rows()) == {CX}
    # The app's "Everything" asks for the lot.
    assert set(_rows(days=-1)) == {CL, CX}
    monkeypatch.setattr(sessions, "STORE_ROWS", 0)
    assert _rows() == {}


def test_a_live_session_is_not_listed_twice(stores, monkeypatch):
    """The store knows every conversation, including the ones that are
    running — those are already rows, with a pane and a state."""
    monkeypatch.setattr(sessions, "live_sessions", lambda: {CX: "%42"})
    monkeypatch.setattr(sessions, "_pane_titles", lambda: {"%42": "codex here"})
    rows = _rows()
    assert rows[CX]["live"] is True and rows[CX]["pane"] == "%42"
    assert rows[CX]["harness"] == "codex" and "source" not in rows[CX]
    assert len([r for r in sessions.sessions_index() if r["session"] == CX]) == 1


def test_a_shelved_conversation_keeps_the_shelfs_name_and_gains_its_agent(stores):
    (stores / "book-tracks" / f"{CX}.json").write_text(json.dumps(
        {"session": CX, "folder": "/lib/Conversations/p-x/What broke the build"}))
    rows = _rows()
    assert rows[CX]["title"] == "What broke the build"
    assert rows[CX]["harness"] == "codex" and "source" not in rows[CX]


def test_a_directory_can_be_left_out(stores, monkeypatch):
    """The same exclusion the reaper and the live sweep keep: a gateway's
    scratch folder is thousands of sessions nobody had."""
    monkeypatch.setenv("MEDIA_SESSIONS_EXCLUDE_CWD", "/home/x/scratch")
    assert set(_rows()) == {CX}      # codex's paths say nothing about the cwd


def test_a_directory_can_be_left_out_of_the_list_alone(stores, monkeypatch):
    """A folder whose sessions are written by a schedule: hundreds of them,
    each named after the report it printed. Only the *list* drops them — a
    session opened there by hand is live, and the reaper still has it."""
    monkeypatch.setenv("MEDIA_SESSIONS_STORE_EXCLUDE_CWD", "~/scratch")
    monkeypatch.setenv("HOME", "/home/x")
    assert set(_rows()) == {CX}
    # Not the live sweep's list, which is what the reaper reads.
    assert "/home/x/scratch" not in sessions._excluded_dirs()
    assert "/home/x/scratch" in sessions._store_excluded_dirs()
