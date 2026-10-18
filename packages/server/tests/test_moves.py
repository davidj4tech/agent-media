"""Moving a conversation to another project (`moves.py`, §6.15).

`POST /session/move` over real HTTP with the contract rig. The state dir and
the Claude transcript root are throwaway (conftest), and anything that would
open or close a tmux window is recorded rather than done.
"""

from __future__ import annotations

import json
from pathlib import Path

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
def library(monkeypatch, tmp_path):
    """The Conversations root, with SID's own folder under `p-agent-media`."""
    root = tmp_path / "conversations"
    folder = root / "p-agent-media" / "Sasonica music"
    folder.mkdir(parents=True)
    (folder / "001 - a turn.mp3").write_bytes(b"audio")
    monkeypatch.setenv("MEDIA_BOOK_TRACKS_ROOT", str(root))
    sessions._manifest_dir().mkdir(parents=True, exist_ok=True)
    (sessions._manifest_dir() / f"{SID}.json").write_text(
        json.dumps({"session": SID, "folder": str(folder), "turns": []}))
    return root


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


@pytest.fixture()
def waiters(monkeypatch):
    """Record the sessions a waiter was started for, and start none: a real
    one would outlive the test's stubs."""
    log: list = []
    monkeypatch.setattr(moves, "_watch", log.append)
    return log


def test_a_working_session_waits_for_its_turn(
        server, shelf, signed_in, claude, dest, windows, waiters):
    """Mid-turn, the move is kept rather than refused (15 Oct 2026).

    The rig's SID2 is mid-turn (`shelf` classifies every pane "working").
    """
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID2, "project": "p-agent-mail"}, AUTH)
    assert res.status == 200 and obj["ok"] is True and obj["pending"] is True
    assert obj["project"] == "p-agent-mail" and obj["restarted"] is False
    assert windows == []                       # its turn was left alone
    assert moves.moved(SID2) == {}             # not filed yet
    assert moves.pending(SID2)["cwd"] == dest
    assert waiters == [SID2]


def test_a_working_session_with_no_pane_waits_too(
        server, shelf, signed_in, claude, dest, windows, waiters, monkeypatch):
    """The gate is the turn, not the pane (23 Sep 2026).

    A headless session has no screen to read, so the check used to be
    skipped for it and its transcript moved mid-turn — the very thing the
    gate exists to stop.
    """
    from agent_media_server import driver

    monkeypatch.setattr(driver, "headless_state",
                        lambda sid: {"state": "working", "live": True, "pane": None}
                        if sid == SID else None)
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert res.status == 200 and obj["pending"] is True
    assert moves.moved(SID) == {}
    assert (claude / moves.encoded_dir("/home/ryer/projects/agent-media")
            / f"{SID}.jsonl").exists()


def test_the_waiting_move_happens_when_the_turn_ends(
        shelf, claude, dest, windows, monkeypatch):
    moves.PENDING.put(SID2, {"project": "p-agent-mail", "cwd": dest, "at": 1e12})
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID2: "%42"})
    looks = iter(["working", "working", "input"])
    monkeypatch.setattr(moves, "_busy", lambda s, p: next(looks) == "working")
    monkeypatch.setattr(moves, "POLL_S", 0)
    monkeypatch.setattr(moves.time, "time", lambda: 1e12)
    moves._wait_then_move(SID2)
    assert windows == [("close", SID2, "%42"),
                       ("open", SID2, dest, True, "p-agent-mail")]
    assert moves.moved(SID2)["project"] == "p-agent-mail"
    assert moves.pending(SID2) == {}


def test_a_later_move_replaces_the_waiting_one(
        server, shelf, signed_in, claude, dest, windows, waiters, idle):
    moves.PENDING.put(SID2, {"project": "p-elsewhere", "cwd": "/nowhere", "at": 1.0})
    _res, obj = call(server, "POST", "/session/move",
                     {"session": SID2, "project": "p-agent-mail"}, AUTH)
    assert obj["restarted"] is True             # idle now: moved at once
    assert moves.pending(SID2) == {}            # and the old wait is over


def test_a_move_asked_for_while_waiting_is_the_one_carried_out(
        shelf, claude, dest, windows, monkeypatch):
    """The waiter takes only the row it read; one written since is read again."""
    moves.PENDING.put(SID2, {"project": "p-old", "cwd": "/old", "at": 1e12})
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID2: "%42"})
    monkeypatch.setattr(moves.time, "time", lambda: 1e12)
    monkeypatch.setattr(moves, "POLL_S", 0)
    first = [True]

    def busy(s, p):
        if first[0]:                            # a new move lands mid-wait
            first[0] = False
            moves.PENDING.put(SID2, {"project": "p-agent-mail", "cwd": dest, "at": 1e12})
            return False
        return False

    monkeypatch.setattr(moves, "_busy", busy)
    moves._wait_then_move(SID2)
    assert windows[-1] == ("open", SID2, dest, True, "p-agent-mail")


def test_a_turn_that_never_ends_drops_its_move(shelf, claude, dest, windows, monkeypatch):
    moves.PENDING.put(SID2, {"project": "p-agent-mail", "cwd": dest, "at": 0.0})
    monkeypatch.setattr(moves, "_busy", lambda s, p: True)
    moves._wait_then_move(SID2)
    assert windows == [] and moves.pending(SID2) == {}


def test_a_shelved_session_with_no_pane_still_moves(
        server, shelf, signed_in, claude, dest, windows):
    """Nothing is running there: no pane and no headless state is not busy."""
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert res.status == 200 and obj["restarted"] is False


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


# --- the conversation's own files ---------------------------------------------

def test_the_folder_moves_under_the_new_project(claude, dest, library):
    folder, why = moves.move_folder(SID, "p-agent-mail")
    assert why == ""
    to = library / "p-agent-mail" / "Sasonica music"
    assert folder == str(to)
    assert (to / "001 - a turn.mp3").read_bytes() == b"audio"
    assert not (library / "p-agent-media" / "Sasonica music").exists()
    # The manifest points at it there, so the project derives without the override.
    kept = json.loads((sessions._manifest_dir() / f"{SID}.json").read_text())
    assert kept["folder"] == str(to)
    assert sessions.project_of("", kept["folder"]) == "p-agent-mail"


def test_the_title_part_of_the_folder_is_kept(claude, dest, library):
    """An item that renames itself is a new item: only the project changes."""
    folder, _why = moves.move_folder(SID, "p-agent-mail")
    assert Path(folder).name == "Sasonica music"


def test_a_folder_already_there_is_refused(claude, dest, library):
    (library / "p-agent-mail" / "Sasonica music").mkdir(parents=True)
    folder, why = moves.move_folder(SID, "p-agent-mail")
    assert folder == "" and "already there" in why
    assert (library / "p-agent-media" / "Sasonica music").is_dir()   # left alone


def test_a_thread_with_no_files_still_moves(claude, dest, library):
    assert moves.move_folder(SID2, "p-agent-mail") == ("", "")       # no manifest


def test_a_manifest_with_no_folder_yet_moves_nothing(claude, dest, library):
    """`"folder": ""` is not `.`: the server's own directory was once moved into itself."""
    path = sessions._manifest_dir() / f"{SID}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "folder": ""}))
    assert moves.move_folder(SID, "p-agent-mail") == ("", "")


def test_the_move_takes_the_folder_with_it(server, shelf, signed_in, claude, dest,
                                           library, windows):
    _res, obj = call(server, "POST", "/session/move",
                     {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert obj["ok"] is True
    assert obj["folder"] == str(library / "p-agent-mail" / "Sasonica music")
    assert "folder_error" not in obj


def test_a_folder_that_could_not_move_is_said_beside_the_move(
        server, shelf, signed_in, claude, dest, library, windows):
    (library / "p-agent-mail" / "Sasonica music").mkdir(parents=True)
    _res, obj = call(server, "POST", "/session/move",
                     {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert obj["ok"] is True                       # the thread moved
    assert "already there" in obj["folder_error"]  # its files did not
    assert moves.moved(SID)["project"] == "p-agent-mail"


# --- headless sessions ----------------------------------------------------------

def test_the_newest_copy_moves_and_the_stale_ones_are_set_aside(
        claude, monkeypatch, tmp_path):
    """A resume in the old directory after an earlier move left a second
    copy; the first by name was moved, not the one being written."""
    import os

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    old = claude / moves.encoded_dir("/home/ryer/projects/agent-media") / f"{SID}.jsonl"
    early = claude / moves.encoded_dir("/home/ryer/projects/a-early")
    early.mkdir()
    (early / f"{SID}.jsonl").write_text("stale\n")
    os.utime(early / f"{SID}.jsonl", (1, 1))
    old.write_text("live\n")
    assert moves.move_transcript(SID, OTHER) == ""
    assert (claude / moves.encoded_dir(OTHER) / f"{SID}.jsonl").read_text() == "live\n"
    assert not (early / f"{SID}.jsonl").exists()
    aside = list((tmp_path / "cache" / "claude-stale-transcripts").glob("*/*.jsonl"))
    assert [f.read_text() for f in aside] == ["stale\n"]


@pytest.fixture()
def headless(monkeypatch):
    """SID is a headless session; record what sessiond is told."""
    from agent_media_server import driver

    log: list = []
    answer = {"why": ""}

    class Fake:
        def relocate(self, session, cwd):
            log.append((session, cwd))
            return answer["why"]

    monkeypatch.setattr(driver, "headless_state",
                        lambda s: {"state": "waiting", "live": False, "pane": None}
                        if s == SID else None)
    monkeypatch.setattr(driver, "owned_headless", lambda s: s == SID)
    monkeypatch.setattr(driver, "headless_driver", lambda: Fake())
    return log, answer


def test_a_headless_session_is_told_its_new_directory(
        server, shelf, signed_in, claude, dest, windows, headless):
    log, _ = headless
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert res.status == 200, obj
    assert log == [(SID, dest)]
    assert (claude / moves.encoded_dir(dest) / f"{SID}.jsonl").exists()


def test_a_headless_session_sessiond_will_not_move_stays_put(
        server, shelf, signed_in, claude, dest, windows, headless):
    _, answer = headless
    answer["why"] = "session bb2649cd is working"
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert res.status == 503 and "working" in obj["error"]
    assert moves.moved(SID) == {}
    assert (claude / moves.encoded_dir("/home/ryer/projects/agent-media")
            / f"{SID}.jsonl").exists()
