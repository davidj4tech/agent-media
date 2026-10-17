"""Gemini CLI threads read from its conversation file (transcript.GeminiBuilder).

The fixture (fixtures/gemini/) is in the shapes gemini-cli 0.64 writes: a
metadata line, messages written again whole as they change, `$set`, and a
`$rewindTo` that took back a turn. It is copied under the throwaway
GEMINI_CLI_HOME the conftest sets, never ~/.gemini.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_media_server import transcript as T

SID = "1a2b3c4d-5e6f-4a1b-8c2d-0123456789ab"
FIXTURE = Path(__file__).parent / "fixtures" / "gemini" / "session-2026-10-17T00-00-1a2b3c4d.jsonl"


@pytest.fixture
def chat(tmp_path, monkeypatch):
    home = tmp_path / "gemini-home"
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    project = home / ".gemini" / "tmp" / "proj"
    (project / "chats").mkdir(parents=True)
    (project / ".project_root").write_text("/home/x/proj")
    path = project / "chats" / FIXTURE.name
    shutil.copy(FIXTURE, path)
    return path


def test_a_conversation_reads_like_any_other(chat):
    msgs, more = T.messages(SID)
    assert not more
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    # The context note our hook added is not what was asked, and the turn
    # that was rewound ("Now run all the tests") is gone.
    assert msgs[0]["parts"][0]["text"] == "Read the README and tell me what it says"
    assert msgs[2]["parts"][0]["text"] == "Run only the fast tests"
    first = msgs[1]
    assert [p["type"] for p in first["parts"]] == ["reasoning", "tool", "text"]
    assert first["parts"][0]["text"] == "Reading the README\nI'll open README.md first."
    read = first["parts"][1]
    assert (read["name"], read["title"], read["status"], read["result_summary"]) == \
        ("Read", "Read README.md", "done", "3 lines")
    assert first["parts"][2]["text"] == "The README is a title and two lines."
    last = msgs[3]
    run = last["parts"][0]
    assert (run["name"], run["title"], run["input_summary"], run["result_summary"]) == \
        ("Bash", "Run the fast tests", "pytest -q -m fast", "12 passed in 0.4s")
    assert last["parts"][-1]["text"] == "Done: **12 passed**." and not last["turn"]["running"]
    assert last["id"] == "g4" and last["at"] == T.epoch("2026-10-17T00:02:02.000Z")


def test_paging_and_a_jump(chat):
    msgs, more = T.messages(SID, limit=2)
    assert more and [m["id"] for m in msgs] == ["u4", "g4"]
    msgs, more = T.messages(SID, before="u4")
    assert [m["id"] for m in msgs] == ["u1", "g1"] and more
    win = T.messages_around(SID, "g1", limit=10, most=50)
    assert win and [m["id"] for m in win[0]] == ["u1", "g1", "u4", "g4"]


def test_a_change_to_the_file_is_read_again(chat):
    assert len(T.messages(SID)[0]) == 4
    before = T.file_state(SID)
    with open(chat, "a") as fh:
        fh.write(json.dumps({"$rewindTo": "u4"}) + "\n")
    assert T.file_state(SID) != before
    assert [m["id"] for m in T.messages(SID)[0]] == ["u1", "g1"]


def test_a_failed_step_and_a_tool_answer_that_is_no_prompt(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (
        {"sessionId": SID, "projectHash": "h"},
        {"id": "u", "type": "user", "content": [{"text": "edit it"}]},
        {"id": "g", "type": "gemini", "content": "", "toolCalls": [
            {"id": "t", "name": "replace", "status": "error",
             "args": {"file_path": "/p/a.py", "old_string": "x", "new_string": "y\nz"},
             "result": [{"functionResponse": {"response": {"error": "old_string not found"}}}]}]},
        {"id": "r", "type": "user", "content": [{"functionResponse": {"name": "replace"}}]},
        {"id": "i", "type": "info", "content": "Request cancelled."},
    )) + "\n")
    msgs = T._gemini_messages(str(path))
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    edit = msgs[1]["parts"][0]
    assert (edit["name"], edit["status"], edit["result_summary"], edit["input_summary"]) == \
        ("Edit", "error", "old_string not found", "/p/a.py (−1 +2 lines)")


def test_the_last_reply_and_home(chat):
    harness, path = T.transcript_of(SID)
    assert harness == "gemini" and path == str(chat)
    got = T.last_reply(path, harness)
    assert got and got["text"] == "Done: 12 passed."
