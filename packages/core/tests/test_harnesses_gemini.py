"""Gemini CLI's conversations: a JSONL file that is a fold, not a log.

The fixture (fixtures/gemini/) is in the shapes gemini-cli 0.64's
ChatRecordingService writes: a metadata first line, messages rewritten
whole as they change, `$set` metadata, and a `$rewindTo` that drops a turn.
Everything is under a throwaway GEMINI_CLI_HOME (conftest), never ~/.gemini.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from agent_media_core import harnesses

SID = "1a2b3c4d-5e6f-4a1b-8c2d-0123456789ab"
FIXTURE = Path(__file__).parent / "fixtures" / "gemini" / "session-2026-10-17T00-00-1a2b3c4d.jsonl"


def project(home: Path, folder: str, slug: str) -> Path:
    """`tmp/<slug>` with its `.project_root`, and `projects.json` saying so."""
    d = home / ".gemini" / "tmp" / slug
    (d / "chats").mkdir(parents=True, exist_ok=True)
    (d / ".project_root").write_text(folder)
    reg = home / ".gemini" / "projects.json"
    data = json.loads(reg.read_text()) if reg.exists() else {"projects": {}}
    data["projects"][folder] = slug
    reg.write_text(json.dumps(data))
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "gemini-home"
    monkeypatch.setenv("GEMINI_CLI_HOME", str(h))
    for var, sub in (("CODEX_HOME", "codex"), ("PI_CODING_AGENT_DIR", "pi"),
                     ("CLAUDE_CONFIG_DIR", "claude"), ("HERMES_HOME", "hermes")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    d = project(h, "/home/x/proj", "proj")
    shutil.copy(FIXTURE, d / "chats" / FIXTURE.name)
    return h


def test_the_fold_keeps_the_newest_line_and_honours_a_rewind():
    meta, msgs = harnesses.gemini_read(FIXTURE)
    assert meta["sessionId"] == SID and meta["summary"] == "Summarise the project README"
    # u3 and its answer were rewound away; g1 is its last version, with
    # the tool call it gained.
    assert [m["id"] for m in msgs] == ["u1", "g1", "u2", "g2", "u4", "g4", "u5", "g5"]
    g1 = msgs[1]
    assert g1["toolCalls"][0]["name"] == "read_file" and g1["tokens"]["input"] == 12000


def test_patches_and_a_rewind_to_nothing():
    lines = [json.dumps(r) for r in (
        {"sessionId": SID, "projectHash": "h"},
        {"id": "a", "type": "user", "content": "one"},
        {"id": "b", "type": "gemini", "content": "", "toolCalls": [{"id": "t", "result": None}]},
        {"$patch": {"id": "b", "content": "two", "toolCalls": [{"id": "t", "result": ["ok"]}]}},
    )]
    _meta, msgs = harnesses.gemini_fold(lines)
    assert msgs[1]["content"] == "two" and msgs[1]["toolCalls"][0]["result"] == ["ok"]
    _meta, msgs = harnesses.gemini_fold(lines + [json.dumps({"$patch": {"removeIds": ["a"]}})])
    assert [m["id"] for m in msgs] == ["b"]
    # An id Gemini no longer has: it clears everything, and so does this.
    _meta, msgs = harnesses.gemini_fold(lines + [json.dumps({"$rewindTo": "zzz"})])
    assert msgs == []


def test_the_words_leave_out_hook_context_and_tool_answers():
    assert harnesses.gemini_text([{"text": "hi"}, {"text": "<hook_context>x</hook_context>"}]) == "hi"
    assert harnesses.gemini_text([{"functionResponse": {"name": "read_file"}}]) == ""
    assert harnesses.gemini_text([{"text": "thinking", "thought": True}, {"text": "said"}]) == "said"
    assert harnesses.gemini_prompt({"content": [{"text": "expanded @a.txt contents"}],
                                    "displayContent": [{"text": "look at @a.txt"}]}) == "look at @a.txt"


def test_it_is_found_and_described(home):
    assert harnesses.harness_of(SID) == harnesses.GEMINI
    found = harnesses.transcript(SID)
    assert found and found[0] == harnesses.GEMINI and found[1].name == FIXTURE.name
    assert harnesses.cwd_of(SID) == "/home/x/proj"
    assert harnesses.title_of(SID) == "Summarise the project README"
    # The hook's context note is not what was asked.
    assert harnesses.first_prompt(SID) == "Read the README and tell me what it says"


def test_a_file_whose_prefix_is_shared_is_not_taken_for_it(home):
    """Eight characters of the id name the file; the first line decides."""
    other = "1a2b3c4d-0000-4000-8000-000000000000"
    d = project(home, "/home/x/other", "other")
    (d / "chats" / "session-2026-10-17T01-00-1a2b3c4d.jsonl").write_text(
        json.dumps({"sessionId": other, "projectHash": "h"}) + "\n")
    assert harnesses.cwd_of(SID) == "/home/x/proj"
    assert harnesses.cwd_of(other) == "/home/x/other"


def test_it_is_listed_but_not_its_subagents(home):
    d = home / ".gemini" / "tmp" / "proj" / "chats"
    sub = d / SID
    sub.mkdir()
    (sub / "9f9f9f9f-0000-4000-8000-000000000000.jsonl").write_text(json.dumps(
        {"sessionId": "9f9f9f9f-0000-4000-8000-000000000000", "projectHash": "h",
         "kind": "subagent"}) + "\n")
    # An empty start-up file Gemini has not removed yet has a session too;
    # a stray file that is not a session is not.
    (d / "session-2026-10-17T02-00-deadbeef.jsonl").write_text("not json\n")
    rows = [r for r in harnesses.stored() if r.harness == harnesses.GEMINI]
    assert [r.session for r in rows] == [SID]
    assert rows[0].folder == "proj" and rows[0].path.endswith(FIXTURE.name)


def test_the_project_folder_is_found_by_the_registry_or_the_marker(home):
    assert harnesses.gemini_project_dir("/home/x/proj") == home / ".gemini" / "tmp" / "proj"
    (home / ".gemini" / "projects.json").unlink()
    assert harnesses.gemini_project_dir("/home/x/proj/") == home / ".gemini" / "tmp" / "proj"
    assert harnesses.gemini_project_dir("/nowhere") is None


def test_gemini_is_node_running_its_bundle():
    assert harnesses._gemini_pid(["node", "/u/lib/node_modules/@google/gemini-cli/bundle/gemini.js"])
    assert harnesses._gemini_pid(["/usr/bin/node", "--max-old-space-size=8192",
                                  "/home/x/.npm-global/bin/gemini", "--resume", SID])
    assert harnesses._gemini_pid(["gemini"])
    assert not harnesses._gemini_pid(["node", "/usr/lib/node_modules/opencode/bin/opencode"])
    assert not harnesses._gemini_pid(["python3", "gemini.py"])


def test_a_registered_gemini_is_running(home, monkeypatch, tmp_path):
    """Its SessionStart hook registered the pane; the row is believed while
    the pid is still a Gemini in that pane."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert harnesses.register_pane(harnesses.GEMINI, SID, 4242, "%7")
    argv = {"4242": ["node", "/x/bin/gemini"]}
    monkeypatch.setattr(harnesses, "_argv", lambda pid: argv.get(pid, []))
    monkeypatch.setattr(harnesses, "_pane_of", lambda pid: "%7")
    monkeypatch.setattr(harnesses.glob, "glob", lambda pat, _real=harnesses.glob.glob:
                        [] if pat.startswith("/proc/") else _real(pat))
    got = harnesses.running()
    assert [(r.pid, r.session, r.pane, r.harness) for r in got] == [(4242, SID, "%7", "gemini")]
    argv["4242"] = ["node", "/x/bin/opencode"]
    assert harnesses.running() == []


def test_an_unregistered_gemini_is_worked_out_from_its_arguments():
    assert harnesses._gemini_live_session("1", ["node", "/x/gemini", "--resume", SID]) == SID
    assert harnesses._gemini_live_session("1", ["node", "/x/gemini", f"--resume={SID}"]) == SID


def test_the_recipe_signs_in_without_a_browser(monkeypatch):
    monkeypatch.setattr(harnesses, "program", lambda name: f"/bin/{name}" if name == "gemini" else "")
    assert harnesses.login_argv("gemini") == ["env", "NO_BROWSER=true", "/bin/gemini", "--skip-trust"]
    r = harnesses.RECIPES["gemini"]
    assert r.login_keys == ("/auth",) and r.package == "@google/gemini-cli"
    assert harnesses.logout_argv("gemini") == []


def test_sign_in_is_read_from_its_files(home, monkeypatch):
    d = home / ".gemini"
    assert harnesses.auth_state("gemini") == ("out", "not signed in")
    (d / "oauth_creds.json").write_text(json.dumps({"access_token": "a", "expiry_date": 1}))
    assert harnesses.auth_state("gemini")[0] == "out"       # no refresh token: dead
    (d / "oauth_creds.json").write_text(json.dumps({"refresh_token": "r"}))
    assert harnesses.auth_state("gemini") == ("in", "signed in with Google")
    (d / "google_accounts.json").write_text(json.dumps({"active": "d@example.com", "old": []}))
    assert harnesses.auth_state("gemini") == ("in", "d@example.com")
    os.remove(d / "oauth_creds.json")
    (d / ".env").write_text("# keys\nexport GEMINI_API_KEY='abc123'\n")
    assert harnesses.auth_state("gemini") == ("in", "API key (~/.gemini/.env)")
    os.remove(d / ".env")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert harnesses.auth_state("gemini") == ("in", "API key (environment)")
    monkeypatch.delenv("GEMINI_API_KEY")
    (d / "settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": "vertex-ai"}}}))
    assert harnesses.auth_state("gemini")[0] == "unknown"
    # A profile's own home is read, not the default's.
    other = home.parent / "work"
    (other / ".gemini").mkdir(parents=True)
    (other / ".gemini" / "oauth_creds.json").write_text(json.dumps({"refresh_token": "r"}))
    assert harnesses.auth_state("gemini", env={"GEMINI_CLI_HOME": str(other)})[0] == "in"
