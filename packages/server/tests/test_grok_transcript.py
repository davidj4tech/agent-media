"""Grok threads read from its session log (transcript.GrokBuilder).

The fixture (fixtures/grok/) is in the shapes Grok 1.0.62's docs describe —
ACP `session/update` events, words in chunks, tool calls updated further
down — not read off a real session (none on disk yet). It is copied under
the throwaway GROK_HOME the conftest sets, never ~/.grok.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.parse import quote

import pytest

from agent_media_server import transcript as T

SID = "0199f0a1-2b3c-7d4e-8f50-6172839405a6"
FIXTURE = Path(__file__).parent / "fixtures" / "grok"


@pytest.fixture
def chat(tmp_path, monkeypatch):
    home = tmp_path / "grok-home"
    monkeypatch.setenv("GROK_HOME", str(home))
    folder = home / "sessions" / quote("/home/x/proj", safe="") / SID
    folder.mkdir(parents=True)
    for f in ("summary.json", "updates.jsonl"):
        shutil.copy(FIXTURE / f, folder / f)
    return folder / "updates.jsonl"


def test_a_conversation_reads_like_any_other(chat):
    msgs, more = T.messages(SID)
    assert not more
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    # The reminder in the second prompt is not what was asked.
    assert msgs[0]["parts"][0]["text"] == "Run the fast tests"
    assert msgs[2]["parts"][0]["text"] == "Now read the README"
    first = msgs[1]
    assert [p["type"] for p in first["parts"]] == ["reasoning", "text", "tool", "text"]
    assert first["parts"][0]["text"] == "Looking for the test command"
    run = first["parts"][2]
    assert (run["name"], run["title"], run["status"], run["input_summary"], run["result_summary"]) == \
        ("Bash", "Run the tests", "done", "pytest -q -m fast", "12 passed in 0.4s")
    assert first["parts"][3]["text"] == "Done: **12 passed**."
    last = msgs[3]
    read = last["parts"][0]
    assert (read["name"], read["title"], read["status"]) == ("Read", "Read README.md", "done")
    assert last["parts"][-1]["text"] == "It says hi." and not last["turn"]["running"]
    assert msgs[0]["at"] == T.epoch("2026-10-17T00:00:01.000Z")
    assert last["at"] == T.epoch("2026-10-17T00:01:01.000Z")


def test_paging_and_a_jump(chat):
    msgs, more = T.messages(SID, limit=2)
    assert more and [m["id"] for m in msgs] == ["u2", msgs[1]["id"]]
    msgs, more = T.messages(SID, before="u2")
    assert [m["role"] for m in msgs] == ["user", "assistant"] and more
    win = T.messages_around(SID, "u1", limit=10, most=50)
    assert win and len(win[0]) == 4


def test_a_running_tool_and_a_change_read_again(chat):
    before = T.file_state(SID)
    with open(chat, "a") as fh:
        for u in ({"sessionUpdate": "user_message_chunk", "messageId": "u3",
                   "content": {"type": "text", "text": "Edit app.py"}},
                  {"sessionUpdate": "tool_call", "toolCallId": "call_3", "title": "Edit app.py",
                   "kind": "edit", "status": "in_progress", "toolName": "search_replace",
                   "rawInput": {"file_path": "/p/app.py", "old_string": "x", "new_string": "y"}}):
            fh.write(json.dumps({"sessionId": SID, "update": u}) + "\n")
    assert T.file_state(SID) != before
    msgs = T.messages(SID)[0]
    edit = msgs[-1]["parts"][0]
    assert (edit["name"], edit["status"]) == ("Edit", "running") and msgs[-1]["turn"]["running"]
    with open(chat, "a") as fh:
        fh.write(json.dumps({"sessionId": SID, "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "call_3", "status": "failed",
            "rawOutput": "old_string not found"}}) + "\n")
    edit = T.messages(SID)[0][-1]["parts"][0]
    assert (edit["status"], edit["result_summary"]) == ("error", "old_string not found")


def test_the_last_reply_and_home(chat):
    harness, path = T.transcript_of(SID)
    assert harness == "grok" and path == str(chat)
    got = T.last_reply(path, harness)
    assert got and got["text"] == "It says hi."
