"""Codex and pi threads read from their own transcripts (transcript.py).

Until now every harness but Claude Code answered with one message per spoken
line — a Codex thread read as a summary of itself, with no steps, no
narration and nothing said that was not said aloud. Each has a reader here
now, and the messages it builds are the same shape Claude's are, so the app
needs to know nothing about which agent wrote a conversation.

The fixtures are small synthetic files in the record shapes the two harnesses
actually write (sampled from red5's own stores on 23 Sep 2026); the conftest
points every harness home at a throwaway dir.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent_media_server import transcript as T

CX = "01a0a6f1-ed4b-7f91-8d63-a61653a846f9"
PI = "5cee9d00-6a28-4d77-9942-bb3ec54d096c"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class Rollout:
    """A Codex rollout, a record at a time."""

    def __init__(self, path):
        self.path, self.n, self.t = path, 0, 1_790_000_000.0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def _id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n:04d}"

    def write(self, rec: dict) -> dict:
        self.t += 1
        rec = {"timestamp": iso(self.t), **rec}
        with open(self.path, "ab") as fh:
            fh.write(json.dumps(rec).encode() + b"\n")
        return rec

    def item(self, payload: dict) -> dict:
        return self.write({"type": "response_item", "payload": payload})

    def message(self, role: str, text: str) -> dict:
        key = "output_text" if role == "assistant" else "input_text"
        return self.item({"type": "message", "id": self._id("msg"), "role": role,
                          "content": [{"type": key, "text": text}]})

    def reasoning(self, summary: str = "") -> dict:
        return self.item({"type": "reasoning", "id": self._id("rs"),
                          "summary": [{"type": "summary_text", "text": summary}] if summary else [],
                          "encrypted_content": "gAAAA"})

    def exec_call(self, cmd: str, call_id: str) -> dict:
        return self.item({"type": "custom_tool_call", "id": self._id("ctc"), "name": "exec",
                          "call_id": call_id,
                          "input": f'text(await tools.exec_command({{cmd:"{cmd}",max_output_tokens:3000}}));'})

    def fn_call(self, name: str, args: dict, call_id: str) -> dict:
        return self.item({"type": "function_call", "id": self._id("fc"), "name": name,
                          "arguments": json.dumps(args), "call_id": call_id})

    def output(self, call_id: str, text: str) -> dict:
        return self.item({"type": "custom_tool_call_output", "id": self._id("ctco"),
                          "call_id": call_id, "output": [{"type": "input_text", "text": text}]})

    def task_complete(self) -> dict:
        return self.write({"type": "event_msg", "payload": {"type": "task_complete"}})


class PiLog:
    """A pi session file, a record at a time."""

    def __init__(self, path):
        self.path, self.n, self.t = path, 0, 1_790_000_000.0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def _id(self) -> str:
        self.n += 1
        return f"{self.n:08x}"

    def write(self, rec: dict) -> dict:
        self.t += 1
        rec = {"id": self._id(), "timestamp": iso(self.t), **rec}
        with open(self.path, "ab") as fh:
            fh.write(json.dumps(rec).encode() + b"\n")
        return rec

    def msg(self, message: dict) -> dict:
        return self.write({"type": "message", "message": message})

    def prompt(self, text: str) -> dict:
        return self.msg({"role": "user", "content": [{"type": "text", "text": text}]})

    def say(self, *blocks: dict) -> dict:
        return self.msg({"role": "assistant", "content": list(blocks)})

    def result(self, call_id: str, text: str, error: bool = False) -> dict:
        return self.msg({"role": "toolResult", "toolCallId": call_id, "toolName": "bash",
                         "isError": error, "content": [{"type": "text", "text": text}]})


@pytest.fixture()
def codex(tmp_path):
    return Rollout(tmp_path / "codex" / "sessions" / "2026" / "09" / "16"
                   / f"rollout-2026-09-16T07-21-07-{CX}.jsonl")


@pytest.fixture()
def pi(tmp_path):
    return PiLog(tmp_path / "pi" / "sessions" / "--home-x-scratch--"
                 / f"2026-09-19T21-29-33-432Z_{PI}.jsonl")


def parts(m) -> list:
    return [(p["type"], p.get("text") or p.get("input_summary") or "") for p in m["parts"]]


# --- Codex ------------------------------------------------------------------------

def test_a_codex_turn_is_read_from_its_rollout(codex):
    codex.message("developer", "<skills_instructions>\nSkills…\n</skills_instructions>")
    codex.message("user", "<recommended_plugins>\nhere are some plugins\n</recommended_plugins>")
    codex.message("user", "what broke the build")
    codex.reasoning()
    codex.exec_call("pytest -q", "call_1")
    codex.output("call_1", "Script completed\n1 failed")
    codex.message("assistant", "One test fails.")
    codex.task_complete()
    got = T.messages(CX)
    assert got is not None, "a Codex session has a reader now"
    msgs, _ = got
    # The developer instructions and the plugin preamble are not the conversation.
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert parts(msgs[0]) == [("text", "what broke the build")]
    assert parts(msgs[1]) == [("reasoning", ""), ("tool", "pytest -q"), ("text", "One test fails.")]
    tool = msgs[1]["parts"][1]
    # The `exec` script's command is pulled out, so it reads as what it ran.
    assert tool["name"] == "Bash" and tool["title"] == "Run the tests"
    assert tool["status"] == "done" and "1 failed" in tool["result_summary"]
    # Encrypted reasoning is "there was thinking here", as a signature-only
    # Claude block is.
    assert msgs[1]["parts"][0]["redacted"] is True
    assert msgs[1]["turn"]["running"] is False


def test_codex_reasoning_with_words_is_kept(codex):
    codex.message("user", "go on")
    codex.reasoning("Checking the build log first.")
    codex.message("assistant", "Right.")
    msgs = T.messages(CX)[0]
    assert msgs[1]["parts"][0] == {"type": "reasoning", "text": "Checking the build log first.",
                                   "redacted": False}


def test_a_codex_function_call_waits_for_its_output(codex):
    codex.message("user", "sleep a moment")
    codex.fn_call("sleep", {"duration_ms": 10000}, "call_2")
    msgs = T.messages(CX)[0]
    step = msgs[1]["parts"][0]
    assert step["type"] == "tool" and step["status"] == "running"
    assert msgs[1]["turn"]["running"] is True, "a call with no answer yet is still going"
    codex.output("call_2", "Sleep completed.")
    msgs = T.messages(CX)[0]
    assert msgs[1]["parts"][0]["status"] == "done"
    assert msgs[1]["turn"]["running"] is False


# --- pi ---------------------------------------------------------------------------

def test_a_pi_turn_is_read_from_its_session_file(pi):
    pi.prompt("play my book")
    pi.say({"type": "thinking", "thinking": "Find the player first.", "thinkingSignature": "x"},
           {"type": "text", "text": "I'll look at the player."},
           {"type": "toolCall", "id": "toolu_1", "name": "bash",
            "arguments": {"command": "media book play"}})
    pi.result("toolu_1", "playing")
    got = T.messages(PI)
    assert got is not None
    msgs, _ = got
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert parts(msgs[0]) == [("text", "play my book")]
    assert parts(msgs[1]) == [("reasoning", "Find the player first."),
                              ("text", "I'll look at the player."),
                              ("tool", "media book play")]
    step = msgs[1]["parts"][2]
    assert step["name"] == "Bash" and step["status"] == "done" and step["result_summary"] == "playing"
    # pi names every record, and the message keeps that name.
    assert msgs[0]["id"] and msgs[1]["id"] and msgs[0]["id"] != msgs[1]["id"]


def test_a_failed_pi_tool_is_marked(pi):
    pi.prompt("try it")
    pi.say({"type": "toolCall", "id": "toolu_2", "name": "read", "arguments": {"path": "/tmp/x.py"}})
    pi.result("toolu_2", "No such file", error=True)
    step = T.messages(PI)[0][1]["parts"][0]
    assert step["status"] == "error" and step["title"] == "Read x.py"
    assert step["result_summary"] == "No such file"


def test_a_pi_compaction_ends_the_turn(pi):
    pi.prompt("a long one")
    pi.say({"type": "text", "text": "Working."})
    pi.write({"type": "compaction", "summary": "## Goal\nEverything so far."})
    pi.say({"type": "text", "text": "Carrying on."})
    msgs = T.messages(PI)[0]
    assert [m["role"] for m in msgs] == ["user", "assistant", "assistant"]


# --- both, and the machinery around them ------------------------------------------

def test_growth_is_read_a_record_at_a_time(codex, pi):
    for sid, add in ((CX, lambda n: codex.message("user", f"question {n}")),
                     (PI, lambda n: pi.prompt(f"question {n}"))):
        add(1)
        first = T.messages(sid)[0]
        add(2)
        after = T.messages(sid)[0]
        assert len(after) == len(first) + 1
        assert [m["id"] for m in after[:len(first)]] == [m["id"] for m in first]


def test_older_messages_are_read_when_asked_for(codex, monkeypatch):
    monkeypatch.setattr(T, "TAIL_PROMPTS", 1)
    for n in range(3):
        codex.message("user", f"question {n}")
        codex.message("assistant", f"answer {n}")
        codex.task_complete()
    msgs, more = T.messages(CX)
    assert [p for m in msgs for p in parts(m)] == [("text", "question 2"), ("text", "answer 2")]
    assert more is True, "the tail scan found where the last prompt starts"
    msgs, more = T.messages(CX, before=msgs[0]["id"])
    assert [p for m in msgs for p in parts(m)][:2] == [("text", "question 0"), ("text", "answer 0")]
    assert more is False


def test_hermes_still_answers_from_its_spoken_lines():
    """It keeps conversations in a database, so there is no file to read."""
    assert T.transcript_of("20260921_102508_f74b02") == ("", "")
    assert T.messages("20260921_102508_f74b02") is None
