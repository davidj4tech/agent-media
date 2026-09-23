"""opencode threads read from its database (transcript.OpencodeBuilder).

The rows are the shapes opencode 1.18.32 wrote on red5 (24 Sep 2026): one
`message` row per model step with its role in JSON, and `part` rows holding
text, reasoning, and tool calls that carry their own result.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_media_server import panes
from agent_media_server import transcript as T

OC = "ses_f2fa343dcffeKPfrR1z5h6u4NN"
T0 = 1790202133665

READ_OUT = ("<path>/p/a.txt</path>\n<type>file</type>\n<content>\n1: line one\n\n"
            "(End of file - total 1 lines)\n</content>")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = tmp_path / "data" / "opencode" / "opencode.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as c:
        c.executescript("""
            create table session (id text primary key, parent_id text, directory text,
                title text, time_created integer, time_updated integer, time_archived integer);
            create table message (id text primary key, session_id text, time_created integer,
                data text);
            create table part (id text primary key, message_id text, session_id text,
                time_created integer, time_updated integer, data text);
        """)
        c.execute("insert into session values (?,null,'/p','t',?,?,null)", (OC, T0, T0))
    return path


def put(path, n, role, parts, **extra):
    mid = f"msg_{n:03d}"
    with sqlite3.connect(path) as c:
        c.execute("insert into message values (?,?,?,?)",
                  (mid, OC, T0 + n * 1000, json.dumps({"role": role, **extra})))
        for k, p in enumerate(parts):
            c.execute("insert into part values (?,?,?,?,?,?)",
                      (f"prt_{n:03d}{k:03d}", mid, OC, T0 + n * 1000 + k, T0 + n * 1000 + k,
                       json.dumps(p)))
    return mid


def tool(name, status, inp, **state):
    return {"type": "tool", "tool": name, "callID": f"call_{name}",
            "state": {"status": status, "input": inp, **state}}


def test_a_turn_of_several_steps_is_one_message(db):
    u = put(db, 0, "user", [{"type": "text", "text": "Read a.txt and run echo hi"}])
    a = put(db, 1, "assistant", [
        {"type": "step-start"},
        {"type": "reasoning", "text": "Read it, then run it."},
        tool("read", "completed", {"filePath": "/p/a.txt"}, output=READ_OUT),
        tool("bash", "completed", {"command": "echo hi"}, output="hi\n"),
        {"type": "step-finish", "reason": "tool-calls"}], finish="tool-calls")
    put(db, 2, "assistant", [{"type": "text", "text": "Done."}], finish="stop")
    msgs, more = T.messages(OC)
    assert not more
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["id"] == u and msgs[1]["id"] == a
    assert msgs[0]["parts"] == [{"type": "text", "text": "Read a.txt and run echo hi"}]
    parts = msgs[1]["parts"]
    assert [p["type"] for p in parts] == ["reasoning", "tool", "tool", "text"]
    read, bash = parts[1], parts[2]
    assert (read["name"], read["input_summary"], read["result_summary"]) == \
        ("Read", "/p/a.txt", "1 lines")
    assert (bash["name"], bash["input_summary"], bash["status"]) == ("Bash", "echo hi", "done")
    assert msgs[1]["turn"]["running"] is False


def test_a_tool_still_running_keeps_the_turn_running(db):
    put(db, 0, "user", [{"type": "text", "text": "sleep"}])
    put(db, 1, "assistant", [tool("bash", "running", {"command": "sleep 10"})])
    msgs, _ = T.messages(OC)
    assert msgs[-1]["parts"][0]["status"] == "running"
    assert msgs[-1]["turn"]["running"] is True


def test_a_failed_tool_says_why(db):
    put(db, 0, "user", [{"type": "text", "text": "edit it"}])
    put(db, 1, "assistant", [tool("edit", "error", {"filePath": "/p/a.txt", "oldString": "a",
                                                    "newString": "b\nc"},
                                  error="oldString not found")], finish="stop")
    part = T.messages(OC)[0][-1]["parts"][0]
    assert part["name"] == "Edit" and part["status"] == "error"
    assert part["input_summary"] == "/p/a.txt (−1 +2 lines)"
    assert part["result_summary"] == "oldString not found"


def test_synthetic_text_is_not_what_was_typed(db):
    put(db, 0, "user", [{"type": "text", "text": "look at this"},
                        {"type": "text", "text": "<file body>", "synthetic": True}])
    assert T.messages(OC)[0][0]["parts"][0]["text"] == "look at this"


def test_paging_and_the_stream_state(db):
    for n in range(0, 6, 2):
        put(db, n, "user", [{"type": "text", "text": f"q{n}"}])
        put(db, n + 1, "assistant", [{"type": "text", "text": f"a{n}"}], finish="stop")
    msgs, more = T.messages(OC, limit=2)
    assert more and [m["parts"][0]["text"] for m in msgs] == ["q4", "a4"]
    older, more = T.messages(OC, before=msgs[0]["id"])
    assert [m["parts"][0]["text"] for m in older][-1] == "a2"
    state = T.file_state(OC)
    put(db, 6, "user", [{"type": "text", "text": "again"}])
    assert T.file_state(OC) != state


def test_not_opencode_is_still_none(db):
    assert T.messages("ses_" + "Q" * 26) is None
    assert T.file_state("20260921_102508_f74b02") is None


def test_its_screen_says_working_or_waiting():
    assert panes.classify("   ⬝■■■■⬝  esc interrupt        ctrl+p commands", "opencode") == "working"
    assert panes.classify("   /p                    14.8K (7%)  ctrl+p commands", "opencode") == "input"
    assert panes.classify("", "opencode") is None
