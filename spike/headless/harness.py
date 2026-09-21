#!/usr/bin/env python3
"""Headless Claude Code spike driver (docs/proposals/2026-09-22-headless-sessions.md).

Throwaway, standard library only. It drives ONE `claude -p` stream-json process
per scenario and records every byte both ways, so the adapter in step 2 can be
written against observed envelopes rather than the SDK's minified source.

    python3 spike/headless/harness.py <scenario> [--model haiku]

Scenarios (each writes logs/<scenario>-<n>.jsonl, one record per line:
``{"t": seconds-since-spawn, "dir": "in"|"out"|"err"|"note", "obj"|"line": ...}``):

  basic      initialize, one tiny turn; spawn->init, ->first stream event,
             ->first assistant; prompt_suggestion timing; idle RSS
  queue      a message sent mid-turn (while a Bash `sleep` runs) and one after
  interrupt  interrupt control request mid-tool; the receipt; the next turn
  perms      Write allowed once, denied once; AskUserQuestion answered with
             multi-select + free text via updatedInput.answers
  bash       Bash approvals (allow after a 45 s hold, deny) + re-initialize
             while one is pending
  resume     `--resume <id>` of a finished session from a new process (cold)
  kill       SIGTERM mid-tool, then resume and see what comes back
  hooked     THE ONE run with David's user settings (hooks live, speech ON),
             --include-hook-events, a one-sentence reply
  suggest    three turns with CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=true
  tui-fork   `--resume <tui-session> --fork-session` from this cwd

Safety, because the Stop hook on red5 speaks on David's phone:

* Every run except ``hooked`` passes ``--setting-sources ""`` (what Meridian's
  SDK does by default: no user settings, so no settings.json hooks and none of
  the user `allow` rules that would pre-approve Write/Bash) AND sets
  ``MEDIA_HOOK_ENABLED=0`` as a second guard.
* The child env drops ``TMUX*``, ``CLAUDE*``, ``AI_AGENT``, ``HERDR*`` and
  ``ANTHROPIC_*`` so it neither thinks it is nested in this session nor
  registers itself against this agent's tmux pane, and so auth is the
  subscription (OAuth) login only.
* cwd is ``spike/headless/work`` and the prompts only ever name files there.

Companions: ``show.py`` prints a compact timeline of a log; ``sanitize.py``
scrubs the account e-mail, hook outputs and thinking signatures before commit.
Findings: docs/notes/2026-09-22-headless-spike.md.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / "work"
LOGS = HERE / "logs"
RESULTS = HERE / "results.json"

DROP_PREFIXES = ("TMUX", "CLAUDE", "ANTHROPIC_", "AI_AGENT", "HERDR")


def child_env(hooked: bool = False) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(DROP_PREFIXES)}
    if not hooked:
        env["MEDIA_HOOK_ENABLED"] = "0"
    return env


def tree_rss_kb(pid: int) -> dict:
    """VmRSS (and VmSwap, as `<pid>:<name>:swap`) of pid and all descendants."""
    kids: dict[int, list[int]] = {}
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            fields = (p / "stat").read_text().rsplit(")", 1)[1].split()
            kids.setdefault(int(fields[1]), []).append(int(p.name))
        except OSError:
            continue
    out, todo = {}, [pid]
    while todo:
        q = todo.pop()
        try:
            status = Path(f"/proc/{q}/status").read_text()
            name = next(l.split()[1] for l in status.splitlines() if l.startswith("Name:"))
            rss = next((int(l.split()[1]) for l in status.splitlines()
                        if l.startswith("VmRSS:")), 0)
            swap = next((int(l.split()[1]) for l in status.splitlines()
                         if l.startswith("VmSwap:")), 0)
            out[f"{q}:{name}"] = rss
            if swap:  # red5 swaps hard; RSS alone understates an idle process
                out[f"{q}:{name}:swap"] = swap
        except (OSError, StopIteration):
            pass
        todo += kids.get(q, [])
    return out


class Session:
    """One `claude -p` stream-json process, fully logged."""

    def __init__(self, name: str, extra: list[str], *, model: str = "haiku",
                 hooked: bool = False, session_id: str | None = None,
                 resume: str | None = None, env: dict | None = None):
        LOGS.mkdir(exist_ok=True)
        WORK.mkdir(exist_ok=True)
        n = 1
        while (LOGS / f"{name}-{n}.jsonl").exists():
            n += 1
        self.log_path = LOGS / f"{name}-{n}.jsonl"
        self.log = self.log_path.open("w")
        self.lock = threading.Lock()
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.all: list[tuple[float, dict]] = []
        self.session_id = session_id or (None if resume else str(uuid.uuid4()))
        self.auto: dict = {}  # tool_name -> callable(req) -> response dict
        argv = ["claude", "-p", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose",
                "--permission-prompt-tool", "stdio",
                "--permission-mode", "default",
                "--include-partial-messages", "--prompt-suggestions",
                "--model", model]
        if not hooked:
            argv += ["--setting-sources", ""]
        else:
            argv += ["--include-hook-events"]
        if resume:
            argv += ["--resume", resume]
        else:
            argv += ["--session-id", self.session_id]
        argv += extra
        self.argv = argv
        self.t0 = time.monotonic()
        penv = {**child_env(hooked), **(env or {})}
        self.proc = subprocess.Popen(argv, cwd=WORK, env=penv,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self.note("spawn", argv=argv, pid=self.proc.pid, env_extra=env or {})
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    # --- logging -----------------------------------------------------------
    def now(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def _rec(self, rec: dict) -> None:
        with self.lock:
            self.log.write(json.dumps(rec) + "\n")
            self.log.flush()

    def note(self, what: str, **kw) -> None:
        self._rec({"t": self.now(), "dir": "note", "obj": {"note": what, **kw}})

    def _read_out(self) -> None:
        for line in self.proc.stdout:
            t = self.now()
            try:
                obj = json.loads(line)
            except ValueError:
                self._rec({"t": t, "dir": "out", "line": line.rstrip("\n")})
                continue
            self._rec({"t": t, "dir": "out", "obj": obj})
            self.all.append((t, obj))
            if obj.get("type") == "system" and obj.get("subtype") == "init":
                self.session_id = obj.get("session_id") or self.session_id
            if (obj.get("type") == "control_request"
                    and obj.get("request", {}).get("subtype") == "can_use_tool"):
                fn = self.auto.get(obj["request"].get("tool_name"))
                if fn:
                    self.respond(obj["request_id"], fn(obj["request"]))
            self.events.put({"t": t, **obj})
        self.events.put({"t": self.now(), "type": "__eof__"})

    def _read_err(self) -> None:
        for line in self.proc.stderr:
            self._rec({"t": self.now(), "dir": "err", "line": line.rstrip("\n")})

    # --- writing -----------------------------------------------------------
    def send(self, obj: dict) -> float:
        t = self.now()
        self._rec({"t": t, "dir": "in", "obj": obj})
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        return t

    def user(self, text: str) -> float:
        # A client-chosen uuid is what the interrupt receipt names a queued
        # message by (without one, still_queued/cancelled come back empty).
        return self.send({"type": "user", "session_id": "", "uuid": str(uuid.uuid4()),
                          "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                          "parent_tool_use_id": None})

    def control(self, subtype: str, **kw) -> str:
        rid = "req_" + uuid.uuid4().hex[:12]
        self.send({"type": "control_request", "request_id": rid,
                   "request": {"subtype": subtype, **kw}})
        return rid

    def respond(self, request_id: str, response: dict) -> None:
        self.send({"type": "control_response",
                   "response": {"subtype": "success", "request_id": request_id,
                                "response": response}})

    # --- waiting -----------------------------------------------------------
    def wait(self, pred, timeout: float = 120.0) -> dict | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                ev = self.events.get(timeout=max(0.05, end - time.monotonic()))
            except queue.Empty:
                break
            if ev.get("type") == "__eof__":
                self.note("eof-while-waiting")
                return ev if pred(ev) else None
            if pred(ev):
                return ev
        self.note("wait-timeout")
        return None

    def first(self, typ: str, sub: str | None = None, after: float = 0.0) -> float | None:
        for t, o in self.all:
            if t >= after and o.get("type") == typ and (sub is None or o.get("subtype") == sub):
                return t
        return None

    def rss(self) -> dict:
        d = tree_rss_kb(self.proc.pid)
        self.note("rss", kb=d, total_mb=round(sum(d.values()) / 1024, 1))
        return d

    def close(self, grace: float = 10.0) -> int | None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        t = self.now()
        try:
            rc = self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            self.proc.send_signal(signal.SIGTERM)
            try:
                rc = self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                rc = self.proc.wait()
        self.note("exit", rc=rc, after_stdin_close_s=round(self.now() - t, 3))
        time.sleep(0.2)
        self.log.close()
        return rc


def is_(typ, sub=None):
    return lambda e: e.get("type") == typ and (sub is None or e.get("subtype") == sub)


def save(key: str, value) -> None:
    data = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    data[key] = value
    RESULTS.write_text(json.dumps(data, indent=1))
    print(json.dumps({key: value}, indent=1))


# --- scenarios ---------------------------------------------------------------

def sc_basic(a) -> None:
    s = Session("basic", [], model=a.model)
    init_rid = s.control("initialize", hooks=None)
    init_resp = s.wait(lambda e: e.get("type") == "control_response", 60)
    t_init_resp = init_resp and init_resp["t"]
    # Does system/init arrive before any user message?
    pre = s.wait(is_("system", "init"), 8)
    t_user = s.user("Reply with exactly the word: pineapple")
    res = s.wait(is_("result"), 120)
    sug = s.wait(lambda e: e.get("type") == "prompt_suggestion", 30)
    time.sleep(10)
    idle = s.rss()
    time.sleep(20)
    idle2 = s.rss()
    s.close()
    save(f"basic:{a.model}", {
        "session": s.session_id, "log": s.log_path.name,
        "spawn_to_initialize_response_s": t_init_resp,
        "system_init_before_user_message": bool(pre),
        "user_sent_at": t_user,
        "system_init_at": s.first("system", "init"),
        "first_stream_event_at": s.first("stream_event", after=t_user),
        "first_assistant_at": s.first("assistant", after=t_user),
        "result_at": res and res["t"],
        "prompt_suggestion_at": sug and sug["t"],
        "prompt_suggestion": sug and sug.get("suggestion"),
        "result_cost_usd": res and res.get("total_cost_usd"),
        "idle_rss_mb_10s": round(sum(idle.values()) / 1024, 1),
        "idle_rss_mb_30s": round(sum(idle2.values()) / 1024, 1),
        "rss_tree": idle2,
    })


def sc_queue(a) -> None:
    s = Session("queue", ["--allowedTools", "Bash(python3:*)"], model=a.model)
    s.user("Run exactly this shell command in the foreground with the Bash tool: python3 -c 'import time; time.sleep(12)' --, then reply with exactly: A done")
    s.wait(lambda e: e.get("type") == "assistant" and any(
        c.get("type") == "tool_use" for c in e["message"]["content"]), 60)
    time.sleep(2)
    t_b = s.user("Second message: also reply with exactly the word: banana")
    r1 = s.wait(is_("result"), 90)
    r2 = s.wait(is_("result"), 60)
    t_c = s.user("Third message, after the turn: reply with exactly the word: cherry")
    r3 = s.wait(is_("result"), 60)
    s.close()
    save(f"queue:{a.model}", {
        "session": s.session_id, "log": s.log_path.name, "B_sent_mid_turn_at": t_b,
        "results": [r and {"t": r["t"], "num_turns": r.get("num_turns"),
                           "result": r.get("result")} for r in (r1, r2, r3)],
        "C_sent_at": t_c,
    })


def sc_interrupt(a) -> None:
    """Interrupt a foreground tool with one message queued behind it.

    A bare `sleep N` is refused by Claude Code's own guard (the model then
    backgrounds it), so the running tool is a python sleep.  `--cancel-queued`
    sends `cancel_queued: true` on the interrupt."""
    s = Session("interrupt", ["--allowedTools", "Bash(python3:*)"], model=a.model)
    s.user("Run exactly this shell command in the foreground with the Bash tool: "
           "python3 -c 'import time; time.sleep(30)'  -- then reply: slept")
    s.wait(lambda e: e.get("type") == "system" and e.get("subtype") == "task_started", 60)
    time.sleep(1)
    t_q = s.user("Queued: reply with exactly the word: plum")
    time.sleep(2)
    t_i = s.now()
    kw = {"cancel_queued": True} if a.cancel_queued else {}
    rid = s.control("interrupt", **kw)
    receipt = s.wait(lambda e: e.get("type") == "control_response"
                     and e["response"].get("request_id") == rid, 30)
    r1 = s.wait(is_("result"), 60)
    r2 = s.wait(is_("result"), 30)
    t_n = s.user("After the interrupt: reply with exactly the word: mango")
    r3 = s.wait(is_("result"), 60)
    s.close()
    save(f"interrupt:{a.model}:cancel={a.cancel_queued}", {
        "session": s.session_id, "log": s.log_path.name, "queued_sent_at": t_q,
        "interrupt_sent_at": t_i, "receipt": receipt,
        "results": [r and {k: r.get(k) for k in (
            "t", "subtype", "is_error", "result", "stop_reason", "terminal_reason",
            "num_turns")} for r in (r1, r2, r3)],
        "next_sent_at": t_n,
    })


def sc_perms(a) -> None:
    s = Session("perms", [], model=a.model)
    seen = []

    def write_rule(req):
        seen.append(req)
        path = str(req.get("input", {}).get("file_path", ""))
        if path.endswith("allowed.txt"):
            return {"behavior": "allow", "updatedInput": req["input"]}
        return {"behavior": "deny", "message": "Denied by the spike: do not retry."}

    def ask_rule(req):
        seen.append(req)
        qs = req["input"]["questions"]
        answers = {}
        for q in qs:
            labels = [o["label"] for o in q.get("options", [])]
            answers[q["question"]] = (", ".join(labels[:2]) if q.get("multiSelect")
                                      else "Something else entirely: a free-text answer")
        return {"behavior": "allow", "updatedInput": {"questions": qs, "answers": answers}}

    s.auto = {"Write": write_rule, "AskUserQuestion": ask_rule}
    s.user("Use the Write tool to create the file allowed.txt in the current directory "
           "containing the word yes. Then use the Write tool to create denied.txt containing no. "
           "Then reply with one short line saying which files exist.")
    r1 = s.wait(is_("result"), 120)
    s.user("Now call the AskUserQuestion tool exactly once with two questions: "
           "(1) header 'Fruit', question 'Which fruits do you like?', multiSelect true, "
           "options apple, pear, fig; (2) header 'Name', question 'What should I call you?', "
           "multiSelect false, options Dave, David. Then reply with one line repeating my answers.")
    r2 = s.wait(is_("result"), 120)
    s.close()
    save(f"perms:{a.model}", {
        "session": s.session_id, "log": s.log_path.name,
        "requests": seen, "files": sorted(p.name for p in WORK.iterdir()),
        "results": [r and r.get("result") for r in (r1, r2)],
    })


def sc_bash(a) -> None:
    """Bash approvals: the first answered allow only after 45 s (does a pending
    request time out?), the second denied. A second initialize is sent while
    the first request is pending, to see pending_permission_requests."""
    s = Session("bash", [], model=a.model)
    if a.init_first:  # so the pending-time initialize is a *second* one (a reconnect)
        s.control("initialize", hooks=None)
        s.wait(lambda e: e.get("type") == "control_response", 30)
    s.user("Use the Bash tool to run: touch bash-ok.txt  -- then use the Bash tool to run: "
           "touch bash-no.txt  -- then reply with one short line saying which ran.")
    req = s.wait(lambda e: e.get("type") == "control_request", 90)
    time.sleep(5)
    rid = s.control("initialize", hooks=None)
    init2 = s.wait(lambda e: e.get("type") == "control_response"
                   and e["response"].get("request_id") == rid, 30)
    time.sleep(5 if a.init_first else 40)
    s.respond(req["request_id"], {"behavior": "allow", "updatedInput": req["request"]["input"]})
    req2 = s.wait(lambda e: e.get("type") == "control_request", 90)
    if req2:
        s.respond(req2["request_id"], {"behavior": "deny", "message": "Not this one."})
    r = s.wait(is_("result"), 90)
    s.close()
    ir = (init2 or {}).get("response", {})
    save(f"bash:{a.model}:init_first={a.init_first}", {
        "session": s.session_id, "log": s.log_path.name,
        "request1": req and {k: req[k] for k in ("t", "request")},
        "request2": req2 and {k: req2[k] for k in ("t", "request")},
        "reinitialize_response_keys": sorted((ir.get("response") or {}).keys()) or ir,
        "reinitialize_pending": (ir.get("response") or {}).get("pending_permission_requests"),
        "result": r and {k: r.get(k) for k in ("t", "result", "permission_denials")},
        "files": sorted(p.name for p in WORK.iterdir()),
    })


def sc_resume(a) -> None:
    sid = a.session or json.loads(RESULTS.read_text())[f"basic:{a.model}"]["session"]
    s = Session("resume", [], model=a.model, resume=sid)
    t_u = s.user("What single word did I ask you to reply with earlier? Reply with just it.")
    r = s.wait(is_("result"), 120)
    s.close()
    modal = [o for _, o in s.all if "summary" in json.dumps(o).lower()
             and o.get("type") not in ("assistant", "stream_event", "result")]
    save(f"resume:{a.model}", {
        "session": s.session_id, "resumed": sid, "log": s.log_path.name,
        "system_init_at": s.first("system", "init"), "user_sent_at": t_u,
        "first_stream_event_at": s.first("stream_event", after=t_u),
        "result_at": r and r["t"], "result": r and r.get("result"),
        "usage": r and r.get("usage"), "summary_like_events": modal,
    })


def sc_kill(a) -> None:
    s = Session("kill", ["--allowedTools", "Bash(python3:*)"], model=a.model)
    s.user("Run exactly this shell command in the foreground with the Bash tool: python3 -c 'import time; time.sleep(40)'  -- then reply: woke")
    s.wait(lambda e: e.get("type") == "assistant" and any(
        c.get("type") == "tool_use" for c in e["message"]["content"]), 60)
    time.sleep(3)
    s.note("sigterm")
    s.proc.send_signal(signal.SIGTERM if a.sig == "TERM" else signal.SIGKILL)
    rc = s.proc.wait(20)
    tail = [o for _, o in s.all[-5:]]
    s.note("exit", rc=rc)
    s.log.close()
    sid = s.session_id
    r2 = Session("kill-resume", [], model=a.model, resume=sid)
    t_u = r2.user("What happened to the previous command? One short line.")
    res = r2.wait(is_("result"), 120)
    r2.close()
    save(f"kill:{a.sig}:{a.model}", {
        "session": sid, "logs": [s.log_path.name, r2.log_path.name], "rc": rc,
        "last_events_before_kill": [o.get("type") + "/" + str(o.get("subtype", "")) for o in tail],
        "resume_first_events": [o.get("type") + "/" + str(o.get("subtype", "")) for _, o in r2.all[:6]],
        "resume_result": res and res.get("result"),
    })


def sc_suggest(a) -> None:
    """--prompt-suggestions alone emits nothing in -p: the CLI's init gate returns
    "non_interactive" unless CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION is set, and the
    generator skips a conversation with fewer than two assistant messages."""
    dbg = ["--debug-file", a.debug_file] if a.debug_file else []
    s = Session("suggest", dbg, model=a.model,
                env={"CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "true"})
    out = []
    for word in ("kiwi", "lemon", "lime"):
        s.user(f"Let's plan a small fruit salad. Reply with one short sentence naming {word} as an ingredient.")
        r = s.wait(is_("result"), 90)
        sg = s.wait(lambda e: e.get("type") == "prompt_suggestion", 25)
        out.append({"result_at": r and r["t"], "suggestion_at": sg and sg["t"],
                    "suggestion": sg and sg.get("suggestion")})
    s.close()
    save(f"suggest:{a.model}", {"session": s.session_id, "log": s.log_path.name, "turns": out})


def sc_hooked(a) -> None:
    s = Session("hooked", [], model=a.model, hooked=True)
    t_u = s.user("Reply with exactly this one sentence and nothing else: Headless spike, hooks check.")
    r = s.wait(is_("result"), 120)
    time.sleep(8)  # let async Stop hooks run before stdin closes
    s.close(grace=30)
    hooks = [o for _, o in s.all if o.get("type") == "system"
             and str(o.get("subtype", "")).startswith("hook")]
    act = Path.home() / ".local/state/agent-media/activity" / f"{s.session_id}.jsonl"
    save("hooked", {
        "session": s.session_id, "log": s.log_path.name, "result": r and r.get("result"),
        "hook_events": [(o.get("subtype"), o.get("hook_event") or o.get("hook_name"),
                         o.get("exit_code")) for o in hooks],
        "activity_file": str(act), "activity_exists": act.exists(),
        "activity_lines": act.read_text().splitlines()[:10] if act.exists() else [],
    })


def sc_tui_fork(a) -> None:
    s = Session("tui-fork", ["--fork-session"], model=a.model, resume=a.session)
    t_u = s.user("Reply with exactly the word: forked")
    r = s.wait(is_("result"), 120)
    s.close()
    save("tui-fork", {"resumed": a.session, "session": s.session_id, "log": s.log_path.name,
                      "system_init_at": s.first("system", "init"),
                      "result": r and {k: r.get(k) for k in ("subtype", "is_error", "result")}})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenario", choices=["basic", "queue", "interrupt", "perms", "resume",
                                         "kill", "hooked", "tui-fork", "suggest", "bash"])
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--session", default="")
    ap.add_argument("--debug-file", default="", help="suggest: CLI debug log (kept out of git)")
    ap.add_argument("--init-first", action="store_true")
    ap.add_argument("--cancel-queued", action="store_true")
    ap.add_argument("--sig", default="TERM", choices=["TERM", "KILL"])
    a = ap.parse_args()
    globals()["sc_" + a.scenario.replace("-", "_")](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
