"""Codex's and pi's conversations: where they are, what they are called."""

import json
import os

import pytest

from agent_media_core import activity, harnesses

CX = "01a0a6f1-ed4b-7f91-8d63-a61653a846f9"
PI = "5cee9d00-6a28-4d77-9942-bb3ec54d096c"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    day = tmp_path / "codex" / "sessions" / "2026" / "09" / "16"
    day.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": CX, "cwd": "/home/x/scratch"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "What are the\ntwo startup issues?"}]}},
    ]
    (day / f"rollout-2026-09-16T07-21-07-{CX}.jsonl").write_text("\n".join(map(json.dumps, rows)))
    (tmp_path / "codex" / "session_index.jsonl").write_text(
        json.dumps({"id": CX, "thread_name": "Old name"}) + "\n"
        + json.dumps({"id": CX, "thread_name": "Identify two startup issues"}) + "\n")
    sess = tmp_path / "pi" / "sessions" / "--home-x-scratch--"
    sess.mkdir(parents=True)
    rows = [
        {"type": "session", "id": PI, "cwd": "/home/x/scratch"},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "play my book"}]}},
        {"type": "session_info", "name": "Audiobookshelf via search"},
    ]
    (sess / f"2026-09-19T21-29-33-432Z_{PI}.jsonl").write_text("\n".join(map(json.dumps, rows)))
    return tmp_path


def test_each_agent_is_found_by_its_own_file(homes):
    assert harnesses.harness_of(CX) == "codex"
    assert harnesses.harness_of(PI) == "pi"
    assert harnesses.harness_of("11111111-2222-3333-4444-555555555555") == ""
    assert harnesses.harness_of("../../etc") == ""


def test_cwd_title_and_question(homes):
    assert harnesses.cwd_of(CX) == harnesses.cwd_of(PI) == "/home/x/scratch"
    assert harnesses.title_of(CX) == "Identify two startup issues"     # the last name wins
    assert harnesses.title_of(PI) == "Audiobookshelf via search"
    assert harnesses.first_prompt(CX) == "What are the two startup issues?"   # not the preamble
    assert harnesses.first_prompt(PI) == "play my book"


def test_the_library_names_a_codex_thread_by_its_title(homes):
    from agent_media_core import session_feed
    assert session_feed.asked_for(CX, []) == "Identify two startup issues"


def test_resume_and_fresh_arguments():
    assert harnesses.resume_argv("codex", CX) == ["resume", CX]
    assert harnesses.resume_argv("pi", PI) == ["--session", PI]
    assert harnesses.resume_argv("claude", PI) == ["--resume", PI]
    assert harnesses.fresh_argv("pi", PI) == ["--session-id", PI]
    assert harnesses.fresh_argv("codex", CX) == []


def test_a_pi_registration_holds_only_while_that_pi_is_in_that_pane(homes, monkeypatch):
    assert harnesses.register_pane("pi", PI, 4242, "%7")
    assert not harnesses.register_pane("pi", "not-a-uuid", 4242, "%7")
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["pi"])
    monkeypatch.setattr(harnesses, "_pane_of", lambda pid: "%7")
    assert [(r.session, r.pane) for r in harnesses._registered()] == [(PI, "%7")]
    monkeypatch.setattr(harnesses, "_pane_of", lambda pid: "%8")      # pid reused elsewhere
    assert harnesses._registered() == []
    monkeypatch.setattr(harnesses, "_pane_of", lambda pid: "%7")
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["zsh"])      # no longer a pi
    assert harnesses._registered() == []


@pytest.mark.parametrize("tool,args,want", [
    ("bash", {"command": "ls -la"}, "List files"),
    ("read", {"path": "/a/b/canvas.py"}, "Read canvas.py"),
    ("edit", {"path": "/a/x.ts"}, "Edit x.ts"),
    ("ls", {"path": "/home/x/projects"}, "List projects"),
    ("exec_command", {"command": ["bash", "-lc", "pytest -q"]}, "Run the tests"),
    ("apply_patch", {"input": "*** Begin Patch\n*** Update File: src/a.py\n@@"}, "Edit a.py"),
    ("update_plan", {}, ""),
])
def test_codex_and_pi_tools_are_named(tool, args, want):
    assert activity.describe(tool, args) == want


def test_pi_events_reach_the_step_list_and_the_registry(homes, monkeypatch):
    from agent_media_core.intake import agent_events
    monkeypatch.setenv("TMUX_PANE", "%5")
    agent_events.handle({"hook_event_name": "SessionStart", "session_id": PI, "pid": 99}, "pi")
    assert json.loads((harnesses.registry_dir() / "5").read_text())["session"] == PI
    agent_events.handle({"hook_event_name": "PreToolUse", "session_id": PI,
                         "tool_name": "read", "tool_input": {"path": "/x/a.py"}}, "pi")
    rows = (activity.activity_dir() / f"{PI}.jsonl").read_text().splitlines()
    assert json.loads(rows[-1])["text"] == "Read a.py"


# --- hermes: a store, not a file per conversation ------------------------------

HM = "20260921_102508_f74b02"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A Hermes installation with two profiles, one session in the second."""
    import sqlite3

    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "profiles" / "meridian").mkdir(parents=True)
    (home / "active_profile").write_text("meridian\n")
    for db in (home / "state.db", home / "profiles" / "meridian" / "state.db"):
        c = sqlite3.connect(db)
        c.execute("create table sessions (id text primary key, cwd text, title text,"
                  " started_at real, ended_at real)")
        c.execute("create table messages (id integer primary key, session_id text,"
                  " role text, content text, timestamp real)")
        c.commit()
        c.close()
    c = sqlite3.connect(home / "profiles" / "meridian" / "state.db")
    c.execute("insert into sessions values (?, ?, ?, ?, ?)",
              (HM, None, None, 1789950355.0, None))
    c.executemany("insert into messages (session_id, role, content, timestamp) values (?,?,?,?)",
                  [(HM, "user", "reply with exactly: hermes reached", 1789950356.0),
                   (HM, "assistant", "hermes reached", 1789950359.0)])
    c.commit()
    c.close()
    return home


def test_a_hermes_session_is_found_in_whichever_profile_holds_it(hermes_home):
    assert harnesses.harness_of(HM) == harnesses.HERMES
    assert harnesses.harness_of("20260921_000000_ffffff") == ""


def test_hermes_ids_are_session_ids_though_they_are_not_uuids():
    assert harnesses._safe(HM)
    assert harnesses.is_hermes(HM) and not harnesses.is_hermes(CX)
    assert not harnesses._safe("../etc/passwd")


def test_a_hermes_conversation_is_named_by_what_was_asked(hermes_home):
    # It leaves `title` null unless renamed, so the first question stands in.
    assert harnesses.title_of(HM) == ""
    assert harnesses.first_prompt(HM) == "reply with exactly: hermes reached"


def test_a_tui_session_records_no_directory(hermes_home):
    # Not a bug to work around here: the caller falls back to the pane's own.
    assert harnesses.cwd_of(HM) == ""


def test_hermes_is_started_and_resumed_in_its_tui(hermes_home):
    assert harnesses.fresh_argv(harnesses.HERMES) == ["--tui"]
    assert harnesses.resume_argv(harnesses.HERMES, HM) == ["--tui", "--resume", HM]


def test_a_hermes_is_recognised_by_its_argv_not_its_name():
    # It runs as the venv's python with the script as an argument.
    assert harnesses._hermes_pid(["/h/.hermes/hermes-agent/venv/bin/python3",
                                  "/h/.hermes/hermes-agent/venv/bin/hermes", "--tui"])
    assert harnesses._hermes_pid(["/home/ryer/.local/bin/hermes"])
    assert not harnesses._hermes_pid(["/usr/bin/python3", "-m", "http.server"])
    assert not harnesses._hermes_pid([])


def test_the_profile_decides_which_store_a_live_hermes_writes_to(hermes_home, monkeypatch):
    # Its SQLite connection is per-transaction, so the profile is read from the
    # process, or from the installation's active one.
    monkeypatch.setattr(harnesses, "_env_of", lambda pid, key: "")
    assert harnesses._hermes_store_of("1") == hermes_home / "profiles" / "meridian" / "state.db"
    monkeypatch.setattr(harnesses, "_env_of",
                        lambda pid, key: str(hermes_home) if key == "HERMES_HOME" else "")
    assert harnesses._hermes_store_of("1") == hermes_home / "state.db"


def test_a_live_hermes_is_on_the_newest_session_it_could_have_started(hermes_home, monkeypatch):
    monkeypatch.setattr(harnesses, "_env_of", lambda pid, key: "")
    monkeypatch.setattr(harnesses, "_started_at", lambda pid: 1789950000.0)
    assert harnesses._hermes_session("1") == HM
    # A process that started after every row on file has not said anything yet.
    monkeypatch.setattr(harnesses, "_started_at", lambda pid: 1789960000.0)
    assert harnesses._hermes_session("1") == ""
