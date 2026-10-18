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
    looks = iter(["turn", "turn", ""])
    monkeypatch.setattr(moves, "waiting_on", lambda s, p="": next(looks))
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

    def busy(s, p=""):
        if first[0]:                            # a new move lands mid-wait
            first[0] = False
            moves.PENDING.put(SID2, {"project": "p-agent-mail", "cwd": dest, "at": 1e12})
        return ""

    monkeypatch.setattr(moves, "waiting_on", busy)
    moves._wait_then_move(SID2)
    assert windows[-1] == ("open", SID2, dest, True, "p-agent-mail")


def test_a_turn_that_never_ends_drops_its_move(shelf, claude, dest, windows, monkeypatch):
    moves.PENDING.put(SID2, {"project": "p-agent-mail", "cwd": dest, "at": 0.0})
    monkeypatch.setattr(moves, "waiting_on", lambda s, p="": "turn")
    moves._wait_then_move(SID2)
    assert windows == [] and moves.pending(SID2) == {}
    assert "still busy" in moves.failed(SID2)["error"]      # and says why


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


# --- busy is more than a turn (19 Oct 2026) -------------------------------------

def _headless_view(**kw):
    return {"state": "waiting", "live": True, "pane": None, "raw": "waiting",
            "approval": None, "tasks": 0, "queued": False, **kw}


@pytest.fixture()
def live_headless(monkeypatch, headless):
    """SID is a live headless session; `view` is what sessiond says of it."""
    from agent_media_server import driver

    view = {"now": _headless_view()}
    monkeypatch.setattr(driver, "headless_state", lambda s: view["now"] if s == SID else None)
    # sessiond's events are not waited on here: `_pause` is replaced.
    monkeypatch.setattr(moves, "_events_seq", lambda s: None)
    return view, headless[0]


def test_what_a_move_waits_for(live_headless, monkeypatch):
    view, _log = live_headless
    assert moves.waiting_on(SID) == ""
    view["now"] = _headless_view(state="working", raw="starting")
    assert moves.waiting_on(SID) == "turn"
    view["now"] = _headless_view(queued=True)
    assert moves.waiting_on(SID) == "turn"
    view["now"] = _headless_view(state="approval", raw="approval", approval={"id": "r1"})
    assert moves.waiting_on(SID) == "question"
    view["now"] = _headless_view(tasks=2)
    assert moves.waiting_on(SID) == "agents"
    view["now"] = _headless_view()
    monkeypatch.setattr(moves, "_agents_running", lambda s: True)   # a subagent's file
    assert moves.waiting_on(SID) == "agents"
    view["now"] = _headless_view(live=False, tasks=3)               # parked: nothing runs
    assert moves.waiting_on(SID) == ""


def test_idle_with_background_agents_waits_and_is_not_parked(
        server, shelf, signed_in, claude, dest, windows, waiters, live_headless):
    view, relocated = live_headless
    view["now"] = _headless_view(tasks=1)
    res, obj = call(server, "POST", "/session/move",
                    {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert res.status == 200 and obj["ok"] is True and obj["pending"] is True
    assert obj["why_waiting"] == "agents"
    assert obj["move_pending"] == {"project": "p-agent-mail", "cwd": dest,
                                   "since": moves.pending(SID)["at"], "why_waiting": "agents"}
    assert relocated == []                      # sessiond was never told: not parked
    assert moves.moved(SID) == {}
    assert waiters == [SID]


def test_a_pane_session_with_a_subagent_running_waits(
        server, shelf, signed_in, claude, dest, windows, waiters, idle, monkeypatch):
    monkeypatch.setattr(moves, "_agents_running", lambda s: s == SID2)
    _res, obj = call(server, "POST", "/session/move",
                     {"session": SID2, "project": "p-agent-mail"}, AUTH)
    assert obj["pending"] is True and obj["why_waiting"] == "agents"
    assert windows == []                        # the pane was not closed


def test_a_pending_question_waits_too(
        server, shelf, signed_in, claude, dest, windows, waiters, live_headless):
    view, relocated = live_headless
    view["now"] = _headless_view(state="approval", raw="approval", approval={"id": "r1"})
    _res, obj = call(server, "POST", "/session/move",
                     {"session": SID, "project": "p-agent-mail"}, AUTH)
    assert obj["pending"] is True and obj["why_waiting"] == "question"
    assert relocated == []


def test_the_pending_move_runs_the_moment_the_work_is_done(
        shelf, claude, dest, windows, live_headless, monkeypatch):
    view, relocated = live_headless
    moves.PENDING.put(SID, {"project": "p-agent-mail", "cwd": dest, "at": 1e12,
                            "why_waiting": "turn"})
    monkeypatch.setattr(moves.time, "time", lambda: 1e12)
    # A turn, then a message sent meanwhile (another turn), then its agents,
    # then free: each look waits once on the session's events.
    script = iter([_headless_view(state="working"), _headless_view(state="working"),
                   _headless_view(tasks=1), _headless_view()])
    whys: list = []

    def pause(session, seq):
        whys.append(moves.pending(session).get("why_waiting"))
        view["now"] = next(script)

    view["now"] = next(script)
    monkeypatch.setattr(moves, "_pause", pause)
    moves._wait_then_move(SID)
    assert whys == ["turn", "turn", "agents"]   # it said why at every step
    assert relocated == [(SID, dest)]
    assert moves.moved(SID)["project"] == "p-agent-mail"
    assert moves.pending(SID) == {} and moves.failed(SID) == {}
    assert (claude / moves.encoded_dir(dest) / f"{SID}.jsonl").exists()


def test_a_session_busy_again_at_the_last_moment_keeps_waiting(
        shelf, claude, dest, windows, live_headless, monkeypatch):
    """sessiond refused to park it (a turn started between the look and the
    move): the move goes back to waiting, it does not fail."""
    view, relocated = live_headless
    moves.PENDING.put(SID, {"project": "p-agent-mail", "cwd": dest, "at": 1e12})
    monkeypatch.setattr(moves.time, "time", lambda: 1e12)
    from agent_media_server import driver

    tries = []

    class Racy:
        def relocate(self, session, cwd):
            tries.append(cwd)
            if len(tries) == 1:
                view["now"] = _headless_view(state="working")
                return "session is working"
            return ""

    monkeypatch.setattr(driver, "headless_driver", lambda: Racy())
    monkeypatch.setattr(moves, "_pause", lambda s, q: view.__setitem__("now", _headless_view()))
    moves._wait_then_move(SID)
    assert tries == [dest, dest]
    assert moves.moved(SID)["project"] == "p-agent-mail" and moves.failed(SID) == {}


def test_a_waiting_move_is_on_disk_and_a_restart_picks_it_up(
        server, shelf, signed_in, claude, dest, windows, waiters, live_headless):
    view, _ = live_headless
    view["now"] = _headless_view(tasks=1)
    call(server, "POST", "/session/move", {"session": SID, "project": "p-agent-mail"}, AUTH)
    on_disk = json.loads(moves.PENDING.path().read_text())
    assert on_disk[SID]["cwd"] == dest and on_disk[SID]["why_waiting"] == "agents"
    # A fresh process: a new map over the same file, and `start` watches it.
    from agent_media_server._jsonmap import JsonMap

    assert JsonMap("moves_pending.json").get(SID)["project"] == "p-agent-mail"
    waiters.clear()
    moves.start()
    assert waiters == [SID]


def test_a_second_move_replaces_the_waiting_one(
        server, shelf, signed_in, claude, dest, windows, waiters, live_headless, tmp_path):
    view, _ = live_headless
    view["now"] = _headless_view(state="working")
    other = tmp_path / "elsewhere"
    other.mkdir()
    call(server, "POST", "/session/move", {"session": SID, "project": "p-agent-mail"}, AUTH)
    _res, obj = call(server, "POST", "/session/move", {"session": SID, "cwd": str(other)}, AUTH)
    assert obj["pending"] is True and moves.pending(SID)["cwd"] == str(other)
    assert len(moves.PENDING.all()) == 1


def test_a_waiting_move_can_be_cancelled(
        server, shelf, signed_in, claude, dest, windows, waiters, live_headless):
    view, relocated = live_headless
    view["now"] = _headless_view(state="working")
    call(server, "POST", "/session/move", {"session": SID, "project": "p-agent-mail"}, AUTH)
    res, obj = call(server, "POST", "/session/move/cancel", {"session": SID}, AUTH)
    assert res.status == 200 and obj == {"ok": True, "session": SID, "cancelled": True}
    assert moves.pending(SID) == {}
    # The waiter, waking, finds nothing to do.
    view["now"] = _headless_view()
    moves._wait_then_move(SID)
    assert relocated == [] and moves.moved(SID) == {}
    _res, obj = call(server, "POST", "/session/move/cancel", {"session": SID}, AUTH)
    assert obj["cancelled"] is False


def test_cancel_needs_a_paired_device(server, shelf, claude):
    moves.PENDING.put(SID, {"project": "p-agent-mail", "cwd": "/x", "at": 1.0})
    res, _obj = call(server, "POST", "/session/move/cancel", {"session": SID}, AUTH)
    assert res.status == 401 and moves.pending(SID)


def test_a_vanished_directory_is_recorded_and_shown(
        shelf, claude, tmp_path, windows, live_headless, monkeypatch):
    view, relocated = live_headless
    gone = tmp_path / "gone"
    moves.PENDING.put(SID, {"project": "p-gone", "cwd": str(gone), "at": 1e12})
    monkeypatch.setattr(moves.time, "time", lambda: 1e12)
    moves._wait_then_move(SID)
    assert relocated == [] and moves.moved(SID) == {}
    assert moves.pending(SID) == {}
    assert "no longer a directory" in moves.failed(SID)["error"]
    rows = [{"session": SID}]
    sessions.add_projects(rows)
    assert rows[0]["move_failed"]["project"] == "p-gone"
    assert "no longer a directory" in rows[0]["move_failed"]["error"]
    assert "move_pending" not in rows[0]


def test_a_move_that_fails_when_it_runs_is_recorded(
        shelf, claude, dest, windows, live_headless, monkeypatch):
    view, _ = live_headless
    moves.PENDING.put(SID, {"project": "p-agent-mail", "cwd": dest, "at": 1e12})
    monkeypatch.setattr(moves.time, "time", lambda: 1e12)
    from agent_media_server import driver

    class Broken:
        def relocate(self, session, cwd):
            return "could not record its new directory: disk full"

    monkeypatch.setattr(driver, "headless_driver", lambda: Broken())
    moves._wait_then_move(SID)
    assert "disk full" in moves.failed(SID)["error"]
    assert moves.pending(SID) == {} and moves.moved(SID) == {}


def test_a_thread_row_shows_its_waiting_move(
        server, shelf, signed_in, claude, dest, windows, waiters, live_headless):
    view, _ = live_headless
    rows = [{"session": SID}]
    sessions.add_projects(rows)
    assert "move_pending" not in rows[0]            # additive: absent when none
    view["now"] = _headless_view(state="working")
    call(server, "POST", "/session/move", {"session": SID, "project": "p-agent-mail"}, AUTH)
    rows = [{"session": SID}]
    sessions.add_projects(rows)
    assert rows[0]["move_pending"]["project"] == "p-agent-mail"
    assert rows[0]["move_pending"]["why_waiting"] == "turn"
    from agent_media_server import thread_events

    ev = thread_events.move_status(SID)
    assert ev["move_pending"]["cwd"] == dest and ev["move_failed"] is None
