"""Messages from a Claude Code transcript (transcript.py, server-contract.md §6.2.2).

Every transcript here is a small synthetic file written by the test, with the
record shapes Claude Code writes (compact JSON, one record per line, one
content block per assistant record). None of them is read from a real
transcript: the conftest points CLAUDE_CONFIG_DIR at a throwaway dir.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone

import pytest

from agent_media_server import transcript as T

SID = "6c73498c-02c1-4846-8350-a82006973571"


# --- building transcripts ---------------------------------------------------------

def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class Script:
    """A transcript written a record at a time, as Claude Code would."""

    def __init__(self, path):
        self.path = path
        self.n = 0
        self.t = 1_790_000_000.0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def _uuid(self) -> str:
        self.n += 1
        return f"00000000-0000-4000-8000-{self.n:012d}"

    def write(self, rec: dict) -> dict:
        with open(self.path, "ab") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")).encode() + b"\n")
        return rec

    def base(self, kind: str, **kw) -> dict:
        self.t += 1
        return {"parentUuid": None, "isSidechain": False, "type": kind, "uuid": self._uuid(),
                "timestamp": iso(self.t), "sessionId": SID, "cwd": "/w", **kw}

    def bookkeeping(self) -> None:
        self.write({"type": "mode", "mode": "normal", "sessionId": SID})
        self.write({"type": "permission-mode", "permissionMode": "default", "sessionId": SID})
        self.write({"type": "ai-title", "aiTitle": "A title", "sessionId": SID})
        self.write({"type": "file-history-snapshot", "messageId": "x", "snapshot": {}})
        self.write(self.base("attachment", attachment={"type": "environment", "snapshot": {}}))

    def prompt(self, text, **kw) -> dict:
        return self.write(self.base("user", message={"role": "user", "content": text}, **kw))

    def block(self, block: dict, msgid="msg_1", stop="tool_use", **kw) -> dict:
        return self.write(self.base("assistant", message={
            "id": msgid, "role": "assistant", "model": "claude-opus-5", "type": "message",
            "content": [block], "stop_reason": stop}, **kw))

    def text(self, text, msgid="msg_1", stop="end_turn", **kw) -> dict:
        return self.block({"type": "text", "text": text}, msgid, stop, **kw)

    def thinking(self, text="", msgid="msg_1") -> dict:
        return self.block({"type": "thinking", "thinking": text, "signature": "c2ln"}, msgid)

    def tool(self, name, inp, tid, msgid="msg_1") -> dict:
        return self.block({"type": "tool_use", "id": tid, "name": name, "input": inp}, msgid)

    def result(self, tid, content, is_error=False, tur=None) -> dict:
        block = {"type": "tool_result", "tool_use_id": tid, "content": content}
        if is_error:
            block["is_error"] = True
        return self.write(self.base("user", message={"role": "user", "content": [block]},
                                    toolUseResult=tur if tur is not None else {"stdout": ""}))

    def system(self, sub, **kw) -> dict:
        return self.write(self.base("system", subtype=sub, **kw))

    def end_turn(self) -> None:
        self.system("stop_hook_summary", hookCount=1)
        self.system("turn_duration", durationMs=1000)


@pytest.fixture()
def script(tmp_path):
    return Script(tmp_path / "claude" / "projects" / "-w" / f"{SID}.jsonl")


def msgs(**kw):
    got = T.messages(SID, **kw)
    assert got is not None
    return got[0]


def texts(m) -> list:
    return [p.get("text") for p in m["parts"] if p["type"] == "text"]


# --- parsing ----------------------------------------------------------------------

def test_a_turn_with_every_record_type(script):
    """The record types a real transcript has, and what each becomes."""
    s = script
    s.bookkeeping()
    first = s.prompt("Fix the log, please")
    s.write(s.base("user", isMeta=True, message={"role": "user", "content": [
        {"type": "text", "text": "Base directory for this skill: …"}]}))
    reply = s.thinking("")
    s.thinking("")                                      # a run of them: one part
    s.thinking("Found the route; reading it next.")
    s.tool("Bash", {"command": "ls -la /w", "description": "List the work dir"}, "t1")
    s.result("t1", "a\nb\n")
    s.write(s.base("assistant", isSidechain=True, message={
        "id": "side", "role": "assistant", "content": [{"type": "text", "text": "SIDECHAIN"}],
        "stop_reason": "end_turn"}))
    s.tool("Agent", {"description": "Map the clients", "prompt": "long…",
                     "subagent_type": "Explore"}, "t2")
    s.result("t2", [{"type": "text", "text": "The report."}])
    s.text("All fixed.", stop="end_turn")
    s.end_turn()
    s.system("away_summary", content="Where things stand. (disable recaps in /config)")
    s.write(s.base("attachment", attachment={"type": "total_tokens_reminder", "text": "x"}))
    s.write({"type": "last-prompt", "lastPrompt": "Fix", "sessionId": SID})

    out = msgs()
    assert [m["role"] for m in out] == ["user", "assistant"]
    you, agent = out
    assert you["id"] == first["uuid"] and texts(you) == ["Fix the log, please"]
    assert you["at"] == pytest.approx(T.epoch(first["timestamp"]))
    assert agent["id"] == reply["uuid"]
    assert [p["type"] for p in agent["parts"]] == ["reasoning", "reasoning", "tool", "tool",
                                                   "text"]
    assert agent["parts"][0] == {"type": "reasoning", "text": "", "redacted": True}
    assert agent["parts"][1] == {"type": "reasoning", "redacted": False,
                                 "text": "Found the route; reading it next."}
    bash, sub = agent["parts"][2], agent["parts"][3]
    assert bash == {"type": "tool", "name": "Bash", "title": "List the work dir",
                    "input_summary": "ls -la /w", "status": "done", "result_summary": "a\nb",
                    "tool_use_id": "t1"}
    # A subagent is one tool part; its own turns (the sidechain) are not messages.
    assert sub["name"] == "Agent" and sub["input_summary"] == "Map the clients (Explore)"
    assert sub["result_summary"] == "The report."
    assert "SIDECHAIN" not in json.dumps(out)
    assert agent["turn"] == {"running": False} and agent["spoken"] is None
    assert set(you) == set(agent) == {"id", "role", "at", "parts", "spoken", "turn"}


def test_harness_asides_are_not_messages_but_end_the_turn(script):
    s = script
    s.prompt("First")
    s.text("One.")
    s.end_turn()
    s.prompt("<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>\n"
             "</task-notification>")
    s.text("The task finished.", msgid="msg_2")
    s.end_turn()
    s.prompt("<local-command-caveat>Caveat</local-command-caveat>", isMeta=True)
    s.prompt("<local-command-stdout>output</local-command-stdout>")
    s.prompt("<system-reminder>be good</system-reminder>\nand the real question")
    out = msgs()
    assert [(m["role"], texts(m)) for m in out] == [
        ("user", ["First"]), ("assistant", ["One."]),
        ("assistant", ["The task finished."]),
        ("user", ["and the real question"])]


def test_slash_commands(script):
    s = script
    s.prompt("<command-name>/model</command-name>\n<command-message>model</command-message>\n"
             "<command-args></command-args>")
    s.prompt("<command-name>/review</command-name>\n<command-message>review</command-message>\n"
             "<command-args>123</command-args>")
    s.text("Reviewing.")
    s.system("local_command", content="<command-name>/remote-control</command-name>")
    (cmd, reply) = msgs()
    assert cmd["command"] == {"name": "review", "args": "123", "text": "/review 123"}
    assert texts(cmd) == ["/review 123"]
    assert texts(reply) == ["Reviewing."]


def test_a_prompt_typed_mid_turn_is_a_message(script):
    s = script
    s.prompt("Start")
    s.tool("Bash", {"command": "make"}, "t1")
    s.write(s.base("attachment", attachment={
        "type": "queued_command", "prompt": "also do the docs", "origin": {"kind": "human"}}))
    s.write(s.base("attachment", attachment={
        "type": "queued_command", "prompt": "<task-notification>x</task-notification>"}))
    s.result("t1", "ok")
    s.text("Done, and the docs.")
    out = msgs()
    assert [(m["role"], texts(m)) for m in out] == [
        ("user", ["Start"]), ("assistant", []), ("user", ["also do the docs"]),
        ("assistant", ["Done, and the docs."])]
    # The tool whose result came after the queued prompt still got it.
    assert out[1]["parts"][0]["status"] == "done"


def test_a_mid_turn_prompt_written_as_blocks_is_the_same_message(script):
    """Claude Code wrote the queued prompt as a string and now writes it as
    content blocks. Reading only the first shape dropped every message David
    sent while a turn was running: it reached the thread only as a spoken
    line, placed by when it was read aloud rather than when he sent it."""
    s = script
    s.prompt("Start")
    s.tool("Bash", {"command": "make"}, "t1")
    s.write(s.base("attachment", attachment={
        "type": "queued_command",
        "prompt": [{"type": "text", "text": "also do the docs"}]}))
    s.write(s.base("attachment", attachment={
        "type": "queued_command", "prompt": [{"type": "text", "text": "   "}]}))
    s.write(s.base("attachment", attachment={"type": "queued_command", "prompt": []}))
    s.result("t1", "ok")
    s.text("Done, and the docs.")
    assert [(m["role"], texts(m)) for m in msgs()] == [
        ("user", ["Start"]), ("assistant", []), ("user", ["also do the docs"]),
        ("assistant", ["Done, and the docs."])]


def test_compaction(script):
    s = script
    s.prompt("Before")
    s.text("Old reply.")
    s.system("compact_boundary", content="Conversation compacted")
    s.prompt("This session is being continued from a previous conversation…",
             isCompactSummary=True)
    s.prompt("After")
    s.text("New reply.", msgid="m2")
    assert [texts(m) for m in msgs()] == [["Before"], ["Old reply."], ["After"],
                                          ["New reply."]]


def test_redacted_thinking_block(script):
    script.prompt("Q")
    script.block({"type": "redacted_thinking", "data": "opaque"})
    script.text("A")
    (_, a) = msgs()
    assert a["parts"][0] == {"type": "reasoning", "text": "", "redacted": True}


# --- tools ------------------------------------------------------------------------

def test_tool_summaries_never_carry_file_contents(script):
    s = script
    body = "SECRET-LINE\n" * 50
    s.prompt("Edit things")
    s.tool("Read", {"file_path": "/w/a.py", "offset": 10, "limit": 20}, "r1")
    s.result("r1", "".join(f"{i}\tSECRET-LINE\n" for i in range(20)),
             tur={"type": "text", "file": {"filePath": "/w/a.py", "content": body}})
    s.tool("Write", {"file_path": "/w/b.py", "content": body}, "w1")
    s.result("w1", "File created successfully at: /w/b.py",
             tur={"type": "create", "content": body, "originalFile": body})
    s.tool("Edit", {"file_path": "/w/c.py", "old_string": "a\nb", "new_string": "SECRET-LINE\nb\nc",
                    "replace_all": False}, "e1")
    s.result("e1", "The file /w/c.py has been updated.", tur={"originalFile": body})
    s.tool("Grep", {"pattern": "def x", "path": "/w"}, "g1")
    s.result("g1", "missing", is_error=True)
    s.tool("mcp__media__music_play", {"query": "x" * 1000}, "m1")
    s.result("m1", "y" * 5000)
    (_, a) = msgs()
    read, write, edit, grep, mcp = a["parts"]
    assert read["input_summary"] == "/w/a.py (lines 10–29)"
    assert read["result_summary"] == "20 lines" and read["title"] == "Read a.py"
    assert write["input_summary"] == "/w/b.py (51 lines)"
    assert edit["input_summary"] == "/w/c.py (−2 +3 lines)"
    assert grep["status"] == "error" and grep["input_summary"] == "def x in /w"
    assert len(mcp["input_summary"]) <= T.SUMMARY_MAX
    assert len(mcp["result_summary"]) == T.SUMMARY_MAX
    assert "SECRET-LINE" not in json.dumps(a)


def test_ask_user_question_is_an_ask_part(script):
    s = script
    s.prompt("Which?")
    s.tool("AskUserQuestion", {"questions": [
        {"question": "Pick one", "header": "x", "multiSelect": False,
         "options": [{"label": "A", "description": "first"}, {"label": "B"}]}]}, "q1")
    s.result("q1", 'User has answered your questions: "Pick one"="A". You can now continue.')
    s.text("A it is.")
    (_, a) = msgs()
    ask = a["parts"][0]
    assert ask == {"type": "ask", "tool_use_id": "q1", "status": "done", "answer": "A",
                   "ask": [{"question": "Pick one", "multiSelect": False,
                            "options": [{"label": "A", "description": "first"},
                                        {"label": "B", "description": ""}]}]}


# --- turns ------------------------------------------------------------------------

def test_one_message_per_turn_and_running(script):
    s = script
    s.prompt("One")
    s.tool("Bash", {"command": "a"}, "t1")
    s.result("t1", "x")
    s.text("Reply one.", msgid="m1b")
    s.end_turn()
    s.prompt("Two")
    s.thinking("")
    s.tool("Bash", {"command": "b"}, "t2", msgid="m2")
    out = msgs()
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant"]
    assert out[1]["turn"]["running"] is False
    # The second turn is waiting on its tool: running.
    assert out[3]["turn"]["running"] is True and out[3]["parts"][-1]["status"] == "running"
    s.result("t2", "y")
    s.text("Reply two.", msgid="m2b", stop="end_turn")
    out = msgs()
    # Ended by its last block, before any turn-end record arrives.
    assert out[3]["turn"]["running"] is False
    assert texts(out[3]) == ["Reply two."]


def test_an_interrupted_turn(script):
    s = script
    s.prompt("Go")
    s.tool("Bash", {"command": "sleep 100"}, "t1")
    s.write(s.base("user", message={"role": "user", "content": [
        {"type": "text", "text": "[Request interrupted by user for tool use]"}]}))
    s.prompt("Stop that")
    out = msgs()
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    tool = out[1]["parts"][0]
    assert tool["status"] == "error" and tool["result_summary"] == "interrupted"
    assert out[1]["turn"]["running"] is False


# --- reading incrementally --------------------------------------------------------

def _conversation(s, turns=3):
    for i in range(turns):
        s.prompt(f"Question {i}")
        s.tool("Bash", {"command": f"cmd {i}"}, f"t{i}", msgid=f"m{i}")
        s.result(f"t{i}", f"out {i}")
        s.text(f"Answer {i}.", msgid=f"m{i}b")
        s.end_turn()


def test_growth_reads_only_the_new_bytes(script, monkeypatch):
    _conversation(script, 2)
    before = msgs()
    ranges = []
    real = T._feed_range
    monkeypatch.setattr(T, "_feed_range",
                        lambda b, fh, lo, hi, *a: (ranges.append((lo, hi)), real(b, fh, lo, hi, *a)))
    size = os.path.getsize(script.path)
    script.prompt("Question 2")
    script.text("Answer 2.", msgid="m9")
    after = msgs()
    assert ranges == [(size, os.path.getsize(script.path))]
    assert after[:len(before)] == before
    assert [texts(m) for m in after[len(before):]] == [["Question 2"], ["Answer 2."]]
    # Unchanged: nothing read at all.
    ranges.clear()
    msgs()
    assert ranges == []


def test_a_line_being_written_waits(script):
    script.prompt("Q")
    with open(script.path, "ab") as fh:
        fh.write(b'{"type":"assistant","uuid":"half')       # no newline yet
    assert [m["role"] for m in msgs()] == ["user"]
    script.path.write_bytes(script.path.read_bytes().rsplit(b"\n", 1)[0] + b"\n")
    script.text("A")
    assert [m["role"] for m in msgs()] == ["user", "assistant"]


def test_shrink_or_replace_reads_again(script, tmp_path):
    _conversation(script, 3)
    assert len(msgs()) == 6
    # Shrunk (truncated to its first turn): read again from scratch.
    data = script.path.read_bytes()
    lines = data.split(b"\n")
    script.path.write_bytes(b"\n".join(lines[:6]) + b"\n")
    assert [texts(m) for m in msgs()] == [["Question 0"], ["Answer 0."]]
    # Replaced by another file at the same path (a new inode).
    other = tmp_path / "other.jsonl"
    other.write_bytes(data)
    os.replace(other, script.path)
    assert len(msgs()) == 6


def test_rewritten_in_place_reads_again(script):
    _conversation(script, 2)
    msgs()
    data = script.path.read_bytes()
    # Same length, different bytes before the old offset: not an append.
    script.path.write_bytes(data.replace(b"Question 0", b"Querstion"))
    script.text("more")
    assert texts(msgs()[0]) == ["Querstion"]


def test_read_from_the_end_first(script, monkeypatch):
    monkeypatch.setattr(T, "TAIL_PROMPTS", 2)
    _conversation(script, 5)
    got, older = T.messages(SID)
    assert [texts(m) for m in got] == [["Question 3"], ["Answer 3."], ["Question 4"],
                                       ["Answer 4."]]
    assert older is True
    # Asking for more than the tail holds reads the rest, once.
    got, older = T.messages(SID, limit=100)
    assert len(got) == 10 and older is False
    assert texts(got[0]) == ["Question 0"]


def test_limit_and_before(script):
    _conversation(script, 4)
    got, older = T.messages(SID, limit=3)
    assert [texts(m) for m in got] == [["Answer 2."], ["Question 3"], ["Answer 3."]]
    assert older is True
    page, older = T.messages(SID, limit=3, before=got[0]["id"])
    assert [texts(m) for m in page] == [["Question 1"], ["Answer 1."], ["Question 2"]]
    assert older is True
    page, older = T.messages(SID, limit=3, before=page[0]["id"])
    assert [texts(m) for m in page] == [["Question 0"], ["Answer 0."]]
    assert older is False
    assert T.messages(SID, before="nope")[0] == []


def test_copies_do_not_change_the_cache(script):
    _conversation(script, 1)
    got = msgs()
    got[1]["parts"][0]["status"] = "changed"
    got[1]["spoken"] = {"id": 1}
    again = msgs()
    assert again[1]["parts"][0]["status"] == "done" and again[1]["spoken"] is None


def test_no_transcript_is_none(script):
    assert T.messages("11111111-2222-4333-8444-555555555555") is None
    assert T.messages("20260922_101010_abcd") is None


# --- speech -----------------------------------------------------------------------

def _line(who, text, at, key="", rid=0, **kw):
    line = {"who": who, "text": text, "at": at, "key": key, "start": None, "end": None}
    if rid:
        line["id"] = rid
    line.update(kw)
    return line


def test_spoken_join_by_key_even_when_summarised(script):
    s = script
    s.prompt("Explain")
    s.tool("Bash", {"command": "x"}, "t1")
    s.result("t1", "ok")
    s.text("Some **bold** words and `code`.\n\nA second paragraph. [[visual: a box]]")
    out = msgs()
    reply = out[1]
    (key,) = T.spoken_keys(texts(reply)[-1])
    # The spoken words were rewritten by a summary: only the key matches.
    lines = [_line("you", "Explain", reply["at"] - 1, rid=4),
             _line("agent", "A short summary nobody would match.", reply["at"] + 30,
                   key=key, rid=5, images=["/img/a.png"], figure=True)]
    T.join_speech(out, lines)
    assert reply["spoken"] == {"id": 5, "key": key, "at": reply["at"] + 30,
                               "images": ["/img/a.png"], "figure": True}
    assert out[0]["spoken"]["id"] == 4


def test_spoken_join_by_words_and_unspoken_stays_null(script):
    s = script
    s.prompt("One")
    s.text("The canvas now reads the transcript directly.", msgid="a")
    s.end_turn()
    s.prompt("Two")
    s.text("Something that was never said aloud.", msgid="b")
    out = msgs()
    first, second = out[1], out[3]
    lines = [
        # Said before the message existed: never joined to it.
        _line("agent", "Something that was never said aloud.", first["at"] - 60, rid=1),
        # No key (an old row), but the same words.
        _line("agent", "The canvas now reads the transcript directly", first["at"] + 400, rid=2),
    ]
    T.join_speech(out, lines)
    assert first["spoken"]["id"] == 2
    assert second["spoken"] is None


def test_a_figure_does_not_cost_the_reply_its_live_line(script):
    """A live line has no key, so it joins by words — and the words it is
    compared against still had the `[[visual:]]` marker in them, which the
    voice never said. A marker near the top of a reply is easily the whole
    400-character window, and the reply then joined nothing: no live line on
    the message, so no bold anywhere for as long as it spoke (David, 23 Sep
    2026). Measured on the reply that showed it: 0.45 against a 0.6 bar.
    """
    script.prompt("Explain it")
    script.text("Fixed, and this is the test of it.\n\n"
                "[[visual: two horizontal timelines stacked, the top one "
                "labelled elapsed with a shaded gap between the first clip "
                "and the second, the bottom one labelled pos with the clips "
                "butted together, and a dashed line down from the boundary "
                "showing that the two disagree by the width of the gap]]\n\n"
                "The bold was on a different clock from the voice, which is "
                "why it sat a whole sentence behind.")
    out = msgs()
    reply = out[1]
    spoken = ("Fixed, and this is the test of it. The bold was on a different "
              "clock from the voice, which is why it sat a whole sentence "
              "behind.")
    live = {"live": True, "sentences": ["Fixed, and this is the test of it."],
            "sentence": 0, "offsets": [0.0], "elapsed": 0.4,
            "server_time": 100.0, "delay": 0.0, "paused": False}
    # No key: a line that is still playing has never been written to history.
    T.join_speech(out, [_line("agent", spoken, reply["at"] + 900, **live)])
    assert reply["spoken"] is not None, "the reply lost its live line to its own figure"
    assert reply["spoken"]["live"] == {k: live[k] for k in T.LIVE_FIELDS}


def test_reveal_halves_join_by_their_own_key(script):
    script.prompt("Draw")
    script.text("Look here. [[reveal: a diagram]] As you can see, it works.")
    out = msgs()
    keys = T.spoken_keys(texts(out[1])[0])
    assert len(keys) == 3
    lines = [_line("agent", "Look here.", out[1]["at"] + 1, key=keys[1], rid=8),
             _line("agent", "As you can see, it works.", out[1]["at"] + 9, key=keys[2], rid=9)]
    T.join_speech(out, lines)
    assert out[1]["spoken"]["id"] == 8


def test_the_live_line_moves_onto_its_message(script):
    script.prompt("Q")
    script.text("Two. Three.")
    out = msgs()
    (key,) = T.spoken_keys("Two. Three.")
    live = {"live": True, "sentences": ["Two.", "Three."], "sentence": 1,
            "offsets": [0.0, 1.2], "elapsed": 1.5, "server_time": 100.0, "delay": 0.0,
            "paused": False}
    T.join_speech(out, [_line("agent", "Two. Three.", out[1]["at"] + 2, key=key, **live)])
    assert out[1]["spoken"]["live"] == {k: live[k] for k in T.LIVE_FIELDS}
    assert out[1]["spoken"]["id"] is None      # live and not yet in history
    got = T.live_of(out)
    assert got["id"] == out[1]["id"] and got["sentence"] == 1


def test_messages_from_lines_for_other_harnesses():
    lines = [_line("you", "Hi", 10.0, command={"name": "x", "args": "", "text": "/x"}),
             _line("agent", "Hello.", 20.0, key="k", rid=3),
             _line("agent", "Which?", 30.0, ask=[{"question": "Which?", "options": [],
                                                   "multiSelect": False}])]
    out = T.messages_from_lines(lines, working=True)
    assert [m["role"] for m in out] == ["user", "assistant", "assistant"]
    assert out[0]["command"]["text"] == "/x" and out[0]["id"] == "line:10.0"
    assert out[1]["spoken"] == {"id": 3, "key": "k", "at": 20.0}
    assert out[2]["parts"][0]["type"] == "ask"
    assert out[2]["turn"]["running"] is True


# --- over HTTP --------------------------------------------------------------------

@pytest.fixture()
def rigged(monkeypatch, script):
    from agent_media_core import activity, book_tracks

    _conversation(script, 30)
    monkeypatch.setattr(book_tracks, "conversation_log",
                        lambda s, f, target=None, positions=True: [])
    monkeypatch.setattr(activity, "attach", lambda s, lines: None)
    return script


from test_contract import AUTH, call, server, shelf, signed_in, typed  # noqa: E402,F401


def test_log_by_session_reads_the_transcript(server, shelf, signed_in, rigged):
    res, obj = call(server, "GET", f"/conversation/log?session={SID}&messages=1&limit=4", headers=AUTH)
    assert res.status == 200, obj
    assert [texts(m) for m in obj["messages"]] == [["Question 28"], ["Answer 28."],
                                                   ["Question 29"], ["Answer 29."]]
    assert obj["older"] is True and obj["lines"] == []
    first = obj["messages"][0]["id"]
    res, page = call(server, "GET", f"/conversation/log?session={SID}&messages=1&limit=2&before={first}",
                     headers=AUTH)
    assert [texts(m) for m in page["messages"]] == [["Question 27"], ["Answer 27."]]
    # SID is not live in the rig: nothing is running.
    assert not any(m["turn"]["running"] for m in obj["messages"])


def test_log_is_gzipped_when_asked(server, shelf, signed_in, rigged):
    import http.client

    conn = http.client.HTTPConnection(*server, timeout=10)
    conn.request("GET", f"/conversation/log?session={SID}&messages=1&limit=60",
                 headers={**AUTH, "Accept-Encoding": "gzip"})
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    assert res.status == 200 and res.getheader("Content-Encoding") == "gzip"
    assert len(json.loads(gzip.decompress(raw))["messages"]) == 60


def test_messages_are_opt_in_on_the_polled_log(server, monkeypatch):
    """A poller without `messages=1` gets the keys, empty — not 30–90 KB of
    tool summaries every few seconds."""
    res, obj = call(server, "GET", f"/conversation/log?session={SID}", headers=AUTH)
    if res.status == 200:
        assert obj["messages"] == [] and obj["older"] is False
