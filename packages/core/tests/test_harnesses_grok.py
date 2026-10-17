"""Grok's conversations: a folder per session, its log ACP session updates.

The fixture (fixtures/grok/) is in the shapes Grok 1.0.62's docs describe —
`summary.json`, and `updates.jsonl` as one `session/update` notification's
params a line — not read off a real session: none existed on disk yet.
Everything is under a throwaway GROK_HOME (conftest), never ~/.grok.
"""

import json
import shutil
from pathlib import Path
from urllib.parse import quote

import pytest

from agent_media_core import harnesses

SID = "0199f0a1-2b3c-7d4e-8f50-6172839405a6"
FIXTURE = Path(__file__).parent / "fixtures" / "grok"


def session(home: Path, cwd: str, sid: str = SID, group: str = "") -> Path:
    """`sessions/<cwd encoded>/<sid>/` holding the fixture."""
    d = home / "sessions" / (group or quote(cwd, safe="")) / sid
    d.mkdir(parents=True)
    for f in ("summary.json", "updates.jsonl"):
        shutil.copy(FIXTURE / f, d / f)
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "grok-home"
    monkeypatch.setenv("GROK_HOME", str(h))
    for var, sub in (("CODEX_HOME", "codex"), ("PI_CODING_AGENT_DIR", "pi"),
                     ("CLAUDE_CONFIG_DIR", "claude"), ("HERMES_HOME", "hermes")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    session(h, "/home/x/proj")
    return h


def test_the_fold_joins_chunks_into_messages():
    info, msgs = harnesses.grok_read(FIXTURE / "updates.jsonl")
    assert info["title"] == "Run the fast tests"
    assert [m["type"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["text"] == "Run the fast tests" and msgs[0]["id"] == "u1"
    parts = msgs[1]["parts"]
    assert [p["type"] for p in parts] == ["thought", "text", "tool", "text"]
    assert parts[0]["text"] == "Looking for the test command" and parts[1]["text"] == "I'll run them."
    tool = parts[2]
    assert tool["name"] == "run_terminal_command" and tool["status"] == "completed"
    assert tool["input"] == {"command": "pytest -q -m fast"}
    assert tool["output"] == {"exit_code": 0, "output": "12 passed in 0.4s"}
    # The tool's own name from `_meta`, its answer from its content blocks.
    read = msgs[3]["parts"][0]
    assert read["name"] == "read_file" and read["output"] == "# Proj\nhi\n"


def test_any_wrapping_of_an_update_is_read():
    """Which of these Grok writes is not confirmed; all three are taken."""
    u = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    for line in ({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": SID, "update": u}},
                 {"sessionId": SID, "update": u}, u):
        _info, msgs = harnesses.grok_fold([json.dumps(line)])
        assert msgs[0]["parts"] == [{"type": "text", "text": "hi"}]


def test_a_new_message_id_is_a_new_message():
    lines = [json.dumps({"sessionUpdate": "user_message_chunk", "messageId": m,
                         "content": {"type": "text", "text": t}})
             for m, t in (("a", "one "), ("a", "prompt"), ("b", "another"))]
    _info, msgs = harnesses.grok_fold(lines)
    assert [m["text"] for m in msgs] == ["one prompt", "another"]


def test_the_prompt_leaves_out_what_grok_added():
    assert harnesses.grok_prompt("<user_query>fix it</user_query>") == "fix it"
    assert harnesses.grok_prompt("go on<system-reminder>50% full</system-reminder>") == "go on"


def test_it_is_found_and_described(home):
    assert harnesses.harness_of(SID) == harnesses.GROK
    found = harnesses.transcript(SID)
    assert found and found[0] == harnesses.GROK and found[1].name == "updates.jsonl"
    assert harnesses.cwd_of(SID) == "/home/x/proj"
    assert harnesses.title_of(SID) == "Fast tests and the README"
    assert harnesses.first_prompt(SID) == "Run the fast tests"


def test_its_folder_without_a_summary_is_the_groups(home):
    """A long path's group is a slug with a `.cwd` file; a summary without
    `info.cwd` falls back to it."""
    other = "0199f0a1-0000-7000-8000-000000000002"
    d = session(home, "", other, group="long-slug-1a2b")
    (d.parent / ".cwd").write_text("/home/x/" + "deep/" * 60)
    (d / "summary.json").write_text("{}")
    assert harnesses.cwd_of(other) == "/home/x/" + "deep/" * 60
    assert harnesses.grok_group("/home/x/proj") == home / "sessions" / quote("/home/x/proj", safe="")


def test_it_is_listed_but_not_its_subagents(home):
    child = "0199f0a1-0000-7000-8000-00000000000c"
    session(home, "/home/x/proj", child)
    sub = home / "sessions" / quote("/home/x/proj", safe="") / SID / "subagents" / "explore-1"
    sub.mkdir(parents=True)
    (sub / "meta.json").write_text(json.dumps({"child_session_id": child, "type": "explore"}))
    # A folder that is not a session's is not one.
    (home / "sessions" / quote("/home/x/proj", safe="") / "not-a-session").mkdir()
    rows = [r for r in harnesses.stored() if r.harness == harnesses.GROK]
    assert [r.session for r in rows] == [SID]
    assert rows[0].folder == quote("/home/x/proj", safe="") and rows[0].path.endswith("updates.jsonl")


def test_grok_is_its_binary_but_not_its_tools():
    assert harnesses._grok_pid(["grok"])
    assert harnesses._grok_pid(["/home/x/.grok/bin/grok", "--resume", SID])
    assert harnesses._grok_pid(["/home/x/.grok/downloads/grok-linux-x86_64", "fix the bug"])
    assert not harnesses._grok_pid(["grok", "agent", "stdio"])
    assert not harnesses._grok_pid(["grok", "update", "--check", "--json"])
    assert not harnesses._grok_pid(["groklet"])


def test_a_registered_grok_is_running(home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert harnesses.register_pane(harnesses.GROK, SID, 4242, "%7")
    argv = {"4242": ["/home/x/.grok/bin/grok"]}
    monkeypatch.setattr(harnesses, "_argv", lambda pid: argv.get(pid, []))
    monkeypatch.setattr(harnesses, "_pane_of", lambda pid: "%7")
    monkeypatch.setattr(harnesses.glob, "glob", lambda pat, _real=harnesses.glob.glob:
                        [] if pat.startswith("/proc/") else _real(pat))
    got = harnesses.running()
    assert [(r.pid, r.session, r.pane, r.harness) for r in got] == [(4242, SID, "%7", "grok")]
    argv["4242"] = ["grok", "update"]
    assert harnesses.running() == []


def test_an_unregistered_grok_is_worked_out(home, monkeypatch):
    assert harnesses._grok_live_session("1", ["grok", "-r", SID]) == SID
    assert harnesses._grok_live_session("1", ["grok", f"--session-id={SID}"]) == SID
    # Else the newest conversation in its folder since it started.
    monkeypatch.setattr(harnesses, "_started_at", lambda pid: 0.0)
    monkeypatch.setattr(harnesses, "_env_of", lambda pid, key: "")
    assert harnesses._grok_live_session("1", ["grok", "--cwd", "/home/x/proj"]) == SID
    assert harnesses._grok_live_session("1", ["grok", "--cwd", "/elsewhere"]) == ""


def test_the_recipe(monkeypatch):
    monkeypatch.setattr(harnesses, "program", lambda name: f"/bin/{name}")
    r = harnesses.RECIPES["grok"]
    assert r.check == ("update", "--check", "--json") and not r.package
    assert harnesses.login_argv("grok") == ["/bin/grok", "login", "--device-auth"]
    assert harnesses.logout_argv("grok") == ["/bin/grok", "logout"]
    assert harnesses.install_argv("grok") == ["/bin/grok", "update"]
    monkeypatch.setattr(harnesses, "installed", lambda name: False)
    assert harnesses.install_argv("grok") == [
        "/bin/sh", "-c", "curl -fsSL https://x.ai/cli/install.sh | bash"]


def test_its_update_check_is_json():
    said = ('{"currentVersion":"1.0.62","latestVersion":"1.0.62","updateAvailable":false,'
            '"installer":"internal","channel":"stable","autoUpdate":null,"error":null}\n')
    assert harnesses._grok_update_answer(said) == (False, "Up to date (1.0.62)")
    said = said.replace('"latestVersion":"1.0.62","updateAvailable":false',
                        '"latestVersion":"1.0.70","updateAvailable":true')
    assert harnesses._grok_update_answer(said) == (True, "Update available: 1.0.70 (this is 1.0.62)")
    assert harnesses._grok_update_answer('{"updateAvailable":null,"error":"offline"}') == (None, "")
    assert harnesses._grok_update_answer("error: no network") == (None, "")


def test_sign_in_is_read_from_auth_json(home, monkeypatch):
    assert harnesses.auth_state("grok") == ("out", "not signed in")
    (home / "auth.json").write_text(json.dumps({"https://auth.x.ai::abc": {
        "key": "k", "refresh_token": "r", "expires_at": "2026-10-17T07:42:58Z",
        "auth_mode": "oidc", "email": "d@example.com"}}))
    assert harnesses.auth_state("grok") == ("in", "d@example.com")
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("XAI_API_KEY", "x")
    assert harnesses.auth_state("grok") == ("in", "API key (environment)")
    monkeypatch.delenv("XAI_API_KEY")
    # A profile's own home is read, not the default's.
    other = home.parent / "work"
    other.mkdir()
    (other / "auth.json").write_text(json.dumps({"key": "k", "email": "w@example.com"}))
    assert harnesses.auth_state("grok", env={"GROK_HOME": str(other)}) == ("in", "w@example.com")
