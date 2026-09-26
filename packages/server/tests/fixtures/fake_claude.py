#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json --output-format
stream-json`, for the headless-session tests. Standard library only.

It speaks the envelopes recorded in the spike (spike/headless/logs/, notes in
docs/notes/2026-09-22-headless-spike.md) — `command_lifecycle`, `system/init`,
`assistant`, `user` tool results, `control_request can_use_tool`, the
interrupt receipt, `result` — with none of the model behind them. What it does
is chosen by the user message's words:

  reply: WORDS     answer WORDS (anything else is answered "ok: <text>")
  tool: CMD        a Bash tool call that needs permission; allow runs it (a
                   no-op), deny is reported as a denial
  ask              an AskUserQuestion (one multi-select, one single-select)
  slow: N          a Bash call that runs N seconds without asking (like
                   `--allowedTools`), so messages sent meanwhile queue and join
                   the turn, and an interrupt lands mid-tool
  crash            exit 3 at once, as a process that died would

It writes a small transcript to `$CLAUDE_CONFIG_DIR/projects/<cwd slug>/
<session>.jsonl` (user and assistant records), so the server finds the session
the way it finds a real one. Every start is logged as one JSON line to
`$FAKE_CLAUDE_LOG` (argv, cwd, the environment variables the tests look at).

`FAKE_CLAUDE_TICK` (seconds, default 0.02) is the pause between events.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

TICK = float(os.environ.get("FAKE_CLAUDE_TICK") or 0.02)
_OUT = threading.Lock()


def arg(name: str) -> str:
    argv = sys.argv[1:]
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else ""


SESSION = arg("--session-id") or arg("--resume") or str(uuid.uuid4())
CWD = os.getcwd()


def emit(obj: dict) -> None:
    obj.setdefault("session_id", SESSION)
    with _OUT:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def transcript_path() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    slug = CWD.replace("/", "-").replace(".", "-")
    d = os.path.join(base, "projects", slug)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{SESSION}.jsonl")


def record(kind: str, content, **extra) -> str:
    uid = str(uuid.uuid4())
    rec = {"type": kind, "uuid": uid, "timestamp": now_iso(), "sessionId": SESSION,
           "cwd": CWD, "entrypoint": "sdk-cli",
           "message": {"role": kind, "content": content}, **extra}
    with open(transcript_path(), "a") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")   # as Claude Code writes it
    return uid


def log_start() -> None:
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if not path:
        return
    keep = {k: v for k, v in os.environ.items()
            if k.startswith(("MEDIA_", "TMUX", "ANTHROPIC", "CLAUDE", "HERDR"))}
    with open(path, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cwd": CWD, "pid": os.getpid(),
                             "session": SESSION, "env": keep}) + "\n")


class Fake:
    def __init__(self) -> None:
        self.inbox: "queue.Queue[dict | None]" = queue.Queue()
        self.responses: dict[str, "queue.Queue[dict]"] = {}
        self.queued: list[dict] = []          # user messages that arrived mid-turn
        self.busy = False
        self.interrupted = threading.Event()
        self.cancel_queued = False
        self.eof = threading.Event()
        # `--model` / `--permission-mode`, then whatever set_model and
        # set_permission_mode say; each turn's init reports them.
        argv = sys.argv[1:]
        self.model = argv[argv.index("--model") + 1] if "--model" in argv else "fake"
        self.mode = (argv[len(argv) - 1 - argv[::-1].index("--permission-mode") + 1]
                     if "--permission-mode" in argv else "default")

    # -- stdin -----------------------------------------------------------------

    def read(self) -> None:
        for line in sys.stdin:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            t = obj.get("type")
            if t == "user":
                if self.busy:
                    self.queued.append(obj)
                    self.lifecycle(obj, "queued")
                else:
                    self.inbox.put(obj)
            elif t == "control_response":
                rid = (obj.get("response") or {}).get("request_id")
                q = self.responses.get(rid)
                if q is not None:
                    q.put(obj["response"])
            elif t == "control_request":
                self.control(obj)
        self.eof.set()
        self.inbox.put(None)
        for q in list(self.responses.values()):
            q.put({"subtype": "eof"})

    def control(self, obj: dict) -> None:
        rid = obj.get("request_id")
        req = obj.get("request") or {}
        sub = req.get("subtype")
        if sub == "interrupt":
            if not self.busy:
                emit({"type": "control_response",
                      "response": {"subtype": "success", "request_id": rid,
                                   "response": {"still_queued": [], "cancelled": []}}})
                return
            self.cancel_queued = bool(req.get("cancel_queued"))
            ids = [m.get("uuid") for m in self.queued]
            if self.cancel_queued:
                for m in self.queued:
                    self.lifecycle(m, "cancelled")
                self.queued = []
                receipt = {"still_queued": [], "cancelled": ids}
            else:
                receipt = {"still_queued": ids}
            emit({"type": "control_response",
                  "response": {"subtype": "success", "request_id": rid, "response": receipt}})
            self.interrupted.set()
            for q in list(self.responses.values()):
                q.put({"subtype": "interrupted"})
        elif sub == "set_model":
            self.model = req.get("model") or "fake"
            emit({"type": "control_response",
                  "response": {"subtype": "success", "request_id": rid}})
        elif sub == "set_permission_mode":
            self.mode = req.get("mode") or "default"
            emit({"type": "control_response",
                  "response": {"subtype": "success", "request_id": rid,
                               "response": {"mode": self.mode}}})
        elif sub == "initialize":
            emit({"type": "control_response",
                  "response": {"subtype": "success", "request_id": rid,
                               "response": {"commands": [], "pid": os.getpid(),
                                            "session_state": "idle"}}})

    # -- turns ---------------------------------------------------------------------

    def lifecycle(self, msg: dict, state: str) -> None:
        if msg.get("uuid"):
            emit({"type": "command_lifecycle", "command_uuid": msg["uuid"], "state": state,
                  "uuid": str(uuid.uuid4())})

    def run(self) -> None:
        while True:
            msg = self.inbox.get()
            if msg is None:
                return
            self.turn(msg)
            while self.queued and not self.eof.is_set():
                nxt = self.queued.pop(0)
                self.turn(nxt, already_queued=True)

    def text_of(self, msg: dict) -> str:
        content = (msg.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict))

    def assistant(self, blocks: list, stop: str | None = None) -> None:
        emit({"type": "assistant", "message": {
            "model": "fake", "id": "msg_" + uuid.uuid4().hex[:12], "type": "message",
            "role": "assistant", "content": blocks, "stop_reason": stop}})
        record("assistant", blocks)

    def result(self, text: str, *, subtype: str = "success", error: bool = False,
               terminal: str = "completed", turns: int = 1, denials=None) -> None:
        emit({"type": "result", "subtype": subtype, "is_error": error, "result": text,
              "stop_reason": "end_turn" if not error else "tool_use",
              "terminal_reason": terminal, "num_turns": turns, "ttft_ms": 5,
              "duration_ms": 10, "total_cost_usd": 0.0,
              "permission_denials": denials or [], "queued_turn_count": len(self.queued)})

    def ask_permission(self, tool: str, inp: dict, tool_use_id: str, **extra) -> dict:
        rid = str(uuid.uuid4())
        q: "queue.Queue[dict]" = queue.Queue()
        self.responses[rid] = q
        emit({"type": "control_request", "request_id": rid,
              "request": {"subtype": "can_use_tool", "tool_name": tool, "display_name": tool,
                          "input": inp, "tool_use_id": tool_use_id, **extra}})
        try:
            return q.get()
        finally:
            self.responses.pop(rid, None)

    def tool_result(self, tool_use_id: str, content: str, error: bool = False) -> None:
        block = {"tool_use_id": tool_use_id, "type": "tool_result", "content": content,
                 "is_error": error}
        emit({"type": "user", "message": {"role": "user", "content": [block]},
              "parent_tool_use_id": None, "uuid": str(uuid.uuid4())})
        record("user", [block])

    def interrupted_turn(self, tool_use_id: str) -> None:
        self.tool_result(tool_use_id, "The user doesn't want to proceed with this tool use.",
                         error=True)
        text = "[Request interrupted by user for tool use]"
        emit({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]},
              "parent_tool_use_id": None, "uuid": str(uuid.uuid4())})
        record("user", [{"type": "text", "text": text}])
        self.result("", subtype="error_during_execution", error=True, terminal="aborted_tools")

    def turn(self, msg: dict, already_queued: bool = False) -> None:
        self.busy = True
        self.interrupted.clear()
        try:
            text = self.text_of(msg).strip()
            if not already_queued:
                self.lifecycle(msg, "queued")
            self.lifecycle(msg, "started")
            record("user", text)
            time.sleep(TICK)
            emit({"type": "system", "subtype": "init", "cwd": CWD, "model": self.model,
                  "permissionMode": self.mode, "apiKeySource": "none",
                  "capabilities": ["interrupt_receipt_v1", "interrupt_cancel_queued_v1",
                                   "msg_lifecycle_v1"]})
            time.sleep(TICK)
            if text == "crash":
                os._exit(3)
            if text.startswith("/rename "):
                # A local command under -p (measured on 2.1.278): the name
                # into the transcript, a synthetic reply, a zero-cost result.
                title = text[len("/rename "):].strip()
                with open(transcript_path(), "a") as fh:
                    fh.write(json.dumps({"type": "custom-title", "customTitle": title,
                                         "sessionId": SESSION}) + "\n")
                words = f"Session renamed to: {title}"
                emit({"type": "assistant", "message": {
                    "model": "<synthetic>", "id": str(uuid.uuid4()), "type": "message",
                    "role": "assistant", "content": [{"type": "text", "text": words}],
                    "stop_reason": "end_turn"}})
                self.result(words, turns=0)
                self.lifecycle(msg, "completed")
                return
            if text.startswith("tool:"):
                self.tool_turn(text[5:].strip())
            elif text == "ask":
                self.ask_turn()
            elif text.startswith("slow:"):
                self.slow_turn(float(text[5:].strip() or 1), msg)
                return
            else:
                words = text[6:].strip() if text.startswith("reply:") else f"ok: {text}"
                self.assistant([{"type": "text", "text": words}], stop="end_turn")
                self.result(words)
            self.lifecycle(msg, "completed")
        finally:
            self.busy = False

    def tool_turn(self, cmd: str) -> None:
        tid = "toolu_" + uuid.uuid4().hex[:20]
        inp = {"command": cmd, "description": f"Run {cmd}"}
        self.assistant([{"type": "tool_use", "id": tid, "name": "Bash", "input": inp}],
                       stop="tool_use")
        resp = self.ask_permission(
            "Bash", inp, tid, description=f"Run {cmd}", blocked_path=None,
            permission_suggestions=[{"type": "addRules", "rules": [
                {"toolName": "Bash", "ruleContent": cmd}], "behavior": "allow",
                "destination": "localSettings"}])
        if resp.get("subtype") in ("eof", "interrupted"):
            if resp.get("subtype") == "interrupted":
                self.interrupted_turn(tid)
            return
        body = resp.get("response") or {}
        if body.get("behavior") == "allow":
            self.tool_result(tid, "(Bash completed with no output)")
            words = f"ran {cmd}"
            denials = []
        else:
            self.tool_result(tid, body.get("message") or "denied", error=True)
            words = f"not allowed to run {cmd}"
            denials = [{"tool_name": "Bash", "tool_use_id": tid, "tool_input": inp}]
        self.assistant([{"type": "text", "text": words}], stop="end_turn")
        self.result(words, denials=denials)

    def ask_turn(self) -> None:
        tid = "toolu_" + uuid.uuid4().hex[:20]
        questions = [
            {"question": "Which fruits do you like?", "header": "Fruit", "multiSelect": True,
             "options": [{"label": "apple", "description": ""},
                         {"label": "pear", "description": ""},
                         {"label": "fig", "description": ""}]},
            {"question": "What should I call you?", "header": "Name", "multiSelect": False,
             "options": [{"label": "Dave", "description": ""},
                         {"label": "David", "description": ""}]}]
        inp = {"questions": questions}
        self.assistant([{"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": inp}],
                       stop="tool_use")
        resp = self.ask_permission("AskUserQuestion", inp, tid, requires_user_interaction=True)
        if resp.get("subtype") in ("eof", "interrupted"):
            if resp.get("subtype") == "interrupted":
                self.interrupted_turn(tid)
            return
        body = resp.get("response") or {}
        if body.get("behavior") != "allow":
            self.tool_result(tid, body.get("message") or "declined", error=True)
            self.assistant([{"type": "text", "text": "no answers"}], stop="end_turn")
            self.result("no answers")
            return
        answers = (body.get("updatedInput") or {}).get("answers") or {}
        said = ", ".join(f'"{q}"="{a}"' for q, a in answers.items())
        self.tool_result(tid, f"The user answered: {said}")
        words = "answers: " + "; ".join(f"{a}" for a in answers.values())
        self.assistant([{"type": "text", "text": words}], stop="end_turn")
        self.result(words)

    def slow_turn(self, seconds: float, msg: dict) -> None:
        tid = "toolu_" + uuid.uuid4().hex[:20]
        inp = {"command": f"python3 -c 'import time; time.sleep({seconds})'",
               "description": f"Sleep for {seconds} seconds"}
        self.assistant([{"type": "tool_use", "id": tid, "name": "Bash", "input": inp}],
                       stop="tool_use")
        emit({"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": tid,
              "description": inp["description"], "task_type": "local_bash"})
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.interrupted.wait(0.02) or self.eof.is_set():
                break
        if self.interrupted.is_set():
            emit({"type": "system", "subtype": "task_notification", "task_id": "t1",
                  "tool_use_id": tid, "status": "stopped"})
            self.interrupted_turn(tid)
            self.lifecycle(msg, "cancelled")
            return
        emit({"type": "system", "subtype": "task_notification", "task_id": "t1",
              "tool_use_id": tid, "status": "completed"})
        self.tool_result(tid, "(Bash completed with no output)")
        # Messages that arrived meanwhile join this turn at the tool boundary.
        joined = self.queued
        self.queued = []
        words = "slept"
        for m in joined:
            self.lifecycle(m, "started")
            record("user", self.text_of(m).strip())
            t = self.text_of(m).strip()
            words += "\n" + (t[6:].strip() if t.startswith("reply:") else f"ok: {t}")
        self.assistant([{"type": "text", "text": words}], stop="end_turn")
        for m in joined:
            self.lifecycle(m, "completed")
        self.result(words, turns=1 + len(joined))
        self.lifecycle(msg, "completed")


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    log_start()
    fake = Fake()
    threading.Thread(target=fake.read, daemon=True).start()
    fake.run()
    time.sleep(TICK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
