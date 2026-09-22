"""Moving a conversation to another project (`moves.py`, §6.15).

`POST /session/move` over real HTTP with the contract rig. The state dir and
the Claude transcript root are throwaway (conftest), and anything that would
open or close a tmux window is recorded rather than done.
"""

from __future__ import annotations

import json

import pytest

from agent_media_server import moves, panes, send, sessions

from test_contract import (AUTH, SID, SID2, call, server, shelf,  # noqa: F401
                           signed_in, typed)

OTHER = "/home/ryer/projects/agent-mail"


@pytest.fixture()
def claude(monkeypatch, tmp_path):
    """A transcript root with SID filed under `~/projects/agent-media`."""
    root = tmp_path / "claude-projects"
    src = root / moves.encoded_dir("/home/ryer/projects/agent-media")
    src.mkdir(parents=True)
    (src / f"{SID}.jsonl").write_text(json.dumps({"cwd": "/home/ryer/projects/agent-media"}) + "\n")
    (src / f"{SID2}.jsonl").write_text(json.dumps({"cwd": "/home/ryer/projects/agent-media"}) + "\n")
    (src / SID / "subagents").mkdir(parents=True)
    monkeypatch.setattr(moves, "claude_root", lambda: root)
    return root


@pytest.fixture()
def dest(monkeypatch, tmp_path):
    """`agent-mail` exists as a directory, and is where its project lives."""
    d = tmp_path / "agent-mail"
    d.mkdir()
    monkeypatch.setattr(sessions, "project_target",
                        lambda p: ("p-agent-mail", str(d)) if p == "p-agent-mail" else ("", ""))
    return str(d)


@pytest.fixture()
def idle(monkeypatch):
    """The rig's live session (SID2) is mid-turn; here it is at its prompt."""
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "input")


@pytest.fixture()
def windows(monkeypatch):
    """Record every window opened and pane closed, and open none."""
    log: list = []

    def open_window(session, cwd, *, resume, host="", flags=(), agent="claude"):
        log.append(("open", session, cwd, resume, host))
        return "%99", ""

    def close_pane(session, pane=""):
        log.append(("close", session, pane))
        return True, {"session": session, "pane": pane, "closed": True}

    monkeypatch.setattr(send, "open_window", open_window)
    monkeypatch.setattr(send, "close_pane", close_pane)
    monkeypatch.setattr(panes, "alive", lambda p: True)
    return log


# --- where a transcript lives -------------------------------------------------

def test_the_directory_name_is_every_other_character_dashed():
    # Read off this host's own ~/.claude/projects names.
    assert moves.encoded_dir("/home/ryer/projects/agent-media") == "-home-ryer-projects-agent-media"
    assert moves.encoded_dir("/home/ryer/.meridian") == "-home-ryer--meridian"
    assert moves.encoded_dir("/tmp/tmp.3Pj1yVnCmL") == "-tmp-tmp-3Pj1yVnCmL"


def test_an_existing_directory_beats_the_rule(claude, monkeypatch, tmp_path):
    """A Claude Code that encoded cwds differently would still be found."""
    odd = claude / "some-other-name"
    odd.mkdir()
    (odd / "aaaa1111-0000-4000-8000-000000000000.jsonl").write_text("{}\n")
    monkeypatch.setattr(sessions, "transcript_cwd",
                        lambda s: OTHER if s.startswith("aaaa") else "")
    assert moves.transcript_dir(OTHER) == odd


def test_the_transcript_and_its_subagents_travel(claude, tmp_path):
    assert moves.move_transcript(SID, OTHER) == ""
    to = claude / moves.encoded_dir(OTHER)
    assert (to / f"{SID}.jsonl").exists()
    assert (to / SID / "subagents").is_dir()
    # Only that one: the session beside it stayed where it was.
    assert (claude / moves.encoded_dir("/home/ryer/projects/agent-media")
            / f"{SID2}.jsonl").exists()


def test_a_session_with_no_claude_transcript_is_not_an_error(claude):
    assert moves.move_transcript("bbbb2222-0000-4000-8000-000000000000", OTHER) == ""


# --- the move -----------------------------------------------------------------

def test_moving_a_closed_thread_files_it_and_opens_nothing(
        server, shelf, signed_in, claude, dest, windows):
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert res.status == 200, obj
    assert obj["ok"] is True and obj["project"] == "p-agent-mail"
    assert obj["cwd"] == dest and obj["restarted"] is False
    assert windows == []                       # nothing closed, nothing opened
    assert moves.moved(SID)["project"] == "p-agent-mail"
    assert (claude / moves.encoded_dir(dest) / f"{SID}.jsonl").exists()


def test_a_live_thread_comes_back_in_the_new_directory(
        server, shelf, signed_in, claude, dest, windows, idle):
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID2, "project": "p-agent-mail"}, AUTH)
    assert res.status == 200, obj
    assert obj["restarted"] is True and obj["pane"] == "%99"
    assert windows == [("close", SID2, "%42"),
                       ("open", SID2, dest, True, "p-agent-mail")]


def test_a_working_session_is_refused(server, shelf, signed_in, claude, dest, windows):
    # The rig's SID2 is mid-turn (`shelf` classifies every pane "working").
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID2, "project": "p-agent-mail"}, AUTH)
    assert res.status == 409 and "working" in obj["error"]
    assert windows == []                       # its turn was left alone
    assert moves.moved(SID2) == {}


def test_a_directory_may_be_named_instead_of_a_project(
        server, shelf, signed_in, claude, dest, windows):
    _res, obj = call(server, "POST", "/session/move", {"session": SID, "cwd": dest}, AUTH)
    assert obj["ok"] is True and obj["cwd"] == dest


def test_an_unknown_project_is_a_400(server, shelf, signed_in, claude, dest):
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID, "project": "p-nowhere"}, AUTH)
    assert res.status == 400 and "p-nowhere" in obj["error"]
    assert moves.moved(SID) == {}


def test_a_move_needs_a_paired_device(server, shelf, claude, dest):
    res, _obj = call(server, "POST", "/session/move",
                     {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert res.status == 401
    assert moves.moved(SID) == {}


# --- what the thread list says afterwards -------------------------------------

def test_the_thread_lists_under_its_new_project(
        server, shelf, signed_in, claude, dest, windows, monkeypatch):
    rows = [{"session": SID}]
    sessions.add_projects(rows, folders={SID: "/lib/Conversations/p-agent-media/Sasonica music"})
    assert rows[0]["project"] == "p-agent-media"     # its shelf folder, before
    call(server, "POST", "/session/move", {"session": SID, "project": "p-agent-mail"}, AUTH)
    rows = [{"session": SID}]
    sessions.add_projects(rows, folders={SID: "/lib/Conversations/p-agent-media/Sasonica music"})
    assert rows[0]["project"] == "p-agent-mail"      # the move wins
    # And a resume opens where it was moved to, not where the transcript ran
    # (`transcript_cwd` itself is stubbed by the rig; `session_cwd` is not).
    assert sessions.session_cwd(SID) == dest
