"""media-sessiond: the host that owns headless agent sessions.

docs/proposals/2026-09-22-headless-sessions.md §4, built on what the spike
measured (docs/notes/2026-09-22-headless-spike.md). Standard library only.

A headless session is a `claude -p --input-format stream-json --output-format
stream-json` process. Its stdin is its only way in, so it cannot belong to the
canvas, which restarts after every pull: it belongs to this small service,
which restarts only when its own code changes. The canvas (driver/headless.py)
talks to it over a unix socket, one JSON object per line each way; the socket
is mode 0600 in a 0700 directory, and that is the whole of its auth.

What it does per session:

* **spawns** `claude -p … --permission-prompt-tool stdio --session-id <id>`
  (or `--resume <id>`), with the environment scrubbed the way the spike found
  necessary — no `TMUX*`, `HERDR*`, `ANTHROPIC_*` or `CLAUDE*` (bar
  `CLAUDE_CONFIG_DIR`), so it is the subscription login and never registers
  itself against whatever pane sessiond was started from — and
  `MEDIA_SOURCE_KIND=headless` / `MEDIA_SOURCE_WORKSPACE` marking it for the
  speech hooks;
* **writes** user messages, each with a client `uuid` (without one the
  interrupt receipt and `command_lifecycle` cannot name it), control responses
  and interrupts;
* **reads** every event and keeps the session's state from them, never from
  what it sent — a finished background task starts a turn by itself:
  `system/init` or `command_lifecycle started` → working; `control_request
  can_use_tool` → approval (kept here, since the CLI does not re-send pending
  requests to a reconnecting client); `result` → waiting;
* **keeps** the events (bar `stream_event`s) in memory and in
  `<state>/sessiond/<id>.events.jsonl`, trimmed, with a sequence number the
  thread stream polls (`events`), and a record per session in
  `<state>/sessiond/<id>.json` that the canvas reads directly — which
  sessions are headless is answered from disk even while this is down;
* **parks** a session that has been waiting `MEDIA_SESSIOND_IDLE` seconds
  (default 1800) — stdin closed, the process gone, the transcript on disk —
  and resumes it with `--resume` on the next message. Never one waiting on an
  approval, working, or holding queued messages. At most `MEDIA_SESSIOND_MAX`
  (default 4) run at once; a fifth parks the least recently used idle one, or
  is refused with a sentence when none is idle.

It writes nothing to tmux. Transcripts land where Claude Code puts them
(`~/.claude/projects/…`), so the canvas's transcript reader works unchanged.

**A restart of this service** ends every child (systemd's cgroup kill, or the
SIGTERM handler here, which closes each stdin and waits `CLOSE_GRACE_S`). A
permission request that was pending then is **not** re-sent by the CLI on
`--resume` (spike §4, §8: the dangling tool call gets a synthetic
"interrupted" result). So the record keeps it under `lost`, an answer to it is
refused with `code: "lost"` and a sentence, and the next message resumes the
session — the model sees the interruption and asks again if it still wants
the tool. At startup any child left running from a crashed instance (matched
by `MEDIA_SESSIOND_SESSION` in its environment) is terminated, so a session
never has two writers.

Ops (request `{"op", …}` → `{"ok": true, …}` or `{"ok": false, "error",
"code"}`): `ping`, `list`, `get`, `start`, `send`, `resume`, `interrupt`,
`answer`, `close`, `park`, `events`.

Config (env): MEDIA_SESSIOND_SOCKET, MEDIA_SESSIOND_IDLE, MEDIA_SESSIOND_MAX,
MEDIA_SESSIOND_CLOSE_GRACE, MEDIA_SESSIOND_CLAUDE (the binary; default
`harnesses.program("claude")`), MEDIA_HEADLESS_MODEL (`--model`),
MEDIA_HEADLESS_PERMISSIONS (`strict` | `normal`, see permissions.py),
MEDIA_HEADLESS_EXTRA_ARGS (more `claude` flags; debugging and smoke runs).
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import signal
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("agent-media.sessiond")

#: The environment a child must not inherit (spike change 9). `CLAUDE*` goes
#: because a parent Claude's `CLAUDECODE`, session id and messaging socket
#: would make the child think it is nested; `CLAUDE_CONFIG_DIR` is kept, as
#: it is configuration rather than inheritance.
DROP_PREFIXES = ("TMUX", "CLAUDE", "ANTHROPIC_", "AI_AGENT", "HERDR")
KEEP = frozenset({"CLAUDE_CONFIG_DIR"})

#: States in which the process is (or is becoming) alive.
LIVE_STATES = frozenset({"starting", "working", "waiting", "approval"})
#: In-memory events kept per session, and the file's trim bounds.
EVENTS_KEEP = 2000
_FILE_TRIM_AT = 4000
#: Fields of `system/init` that are long and nobody here reads.
_INIT_DROP = ("tools", "mcp_servers", "slash_commands", "agents", "skills", "plugins")


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def idle_s() -> float:
    return _env_f("MEDIA_SESSIOND_IDLE", 1800.0)


def max_live() -> int:
    return int(_env_f("MEDIA_SESSIOND_MAX", 4))


def close_grace_s() -> float:
    """Exit after stdin closes took up to 7.2 s in the spike; 10 s by default."""
    return _env_f("MEDIA_SESSIOND_CLOSE_GRACE", 10.0)


#: How long `send` waits for the CLI to acknowledge a message
#: (`command_lifecycle queued`/`started`): ~0.1 s warm, 1.3–3.2 s cold.
ACK_S = 6.0
#: How long `interrupt` waits for its receipt, then for the turn to end.
RECEIPT_S = 5.0
SETTLE_S = 3.0
#: The deny a pending request gets when the user types a message instead.
TYPED_INSTEAD = ("The user answered by typing a message instead of choosing; "
                 "their message follows. Take it as the answer.")


# --- where things are -------------------------------------------------------------

def state_root() -> Path:
    from agent_media_core._paths import state_dir

    return state_dir() / "sessiond"


def socket_path() -> Path:
    """`MEDIA_SESSIOND_SOCKET`, else `$XDG_RUNTIME_DIR/agent-media/sessiond.sock`,
    else the state dir (a host with no runtime dir)."""
    env = (os.environ.get("MEDIA_SESSIOND_SOCKET") or "").strip()
    if env:
        return Path(env).expanduser()
    run = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if run:
        return Path(run) / "agent-media" / "sessiond.sock"
    return state_root() / "sessiond.sock"


def record_path(session: str, root: Path | None = None) -> Path:
    return (root or state_root()) / f"{session}.json"


def read_record(session: str, root: Path | None = None) -> dict | None:
    """sessiond's record of `session`, or None. The canvas reads these
    directly: which sessions are headless is known even while sessiond is
    down."""
    try:
        return json.loads(record_path(session, root).read_text())
    except (OSError, ValueError):
        return None


def records(root: Path | None = None) -> list[dict]:
    out = []
    d = root or state_root()
    try:
        names = sorted(p for p in d.iterdir() if p.suffix == ".json"
                       and not p.name.endswith(".settings.json"))
    except OSError:
        return []
    for p in names:
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("session"):
            out.append(rec)
    return out


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


class Refused(Exception):
    """A request this host will not carry out; `code` is for the canvas."""

    def __init__(self, error: str, code: str = "refused", **extra) -> None:
        super().__init__(error)
        self.error, self.code, self.extra = error, code, extra


# --- one session --------------------------------------------------------------------

class Session:
    def __init__(self, session: str, cwd: str, *, agent: str = "claude",
                 workspace: str = "", permissions: str = "strict") -> None:
        self.id = session
        self.cwd = cwd
        self.agent = agent
        self.workspace = workspace
        self.permissions = permissions
        self.proc: subprocess.Popen | None = None
        self.state = "parked"
        self.since = time.time()
        self.created = time.time()
        self.last_event_at = time.time()
        self.pending: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.lost: list[dict] = []
        self.queued: set[str] = set()
        self.acked: set[str] = set()
        self.responses: dict[str, dict] = {}
        self.events: collections.deque = collections.deque(maxlen=EVENTS_KEEP)
        self.seq = 0
        self.closing = False
        self.parking = False
        self.exit_code: int | None = None
        self.first_text = ""
        self.turns = 0
        self.last_result: dict | None = None
        self.stderr_tail: collections.deque = collections.deque(maxlen=40)
        self.write_lock = threading.Lock()
        self.file_lines = 0
        self.auto_titled = False

    @property
    def live(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def running(self) -> bool:
        """Live and staying so: not being parked or closed."""
        return self.live and not self.parking and not self.closing

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.since = time.time()

    def record(self) -> dict:
        return {"session": self.id, "agent": self.agent, "cwd": self.cwd,
                "workspace": self.workspace, "permissions": self.permissions,
                "driver": "headless", "state": self.state, "since": round(self.since, 3),
                "created": round(self.created, 3),
                "last_event_at": round(self.last_event_at, 3),
                "pid": self.proc.pid if self.live else None, "exit_code": self.exit_code,
                "pending": list(self.pending.values()), "lost": self.lost,
                "queued": sorted(self.queued), "first_text": self.first_text,
                "turns": self.turns, "seq": self.seq, "last_result": self.last_result}

    def view(self) -> dict:
        return {**self.record(), "live": self.live}

    @classmethod
    def from_record(cls, rec: dict) -> "Session":
        s = cls(str(rec["session"]), str(rec.get("cwd") or ""),
                agent=str(rec.get("agent") or "claude"),
                workspace=str(rec.get("workspace") or ""),
                permissions=str(rec.get("permissions") or "strict"))
        s.state = str(rec.get("state") or "parked")
        s.since = float(rec.get("since") or time.time())
        s.created = float(rec.get("created") or s.since)
        s.last_event_at = float(rec.get("last_event_at") or s.since)
        s.exit_code = rec.get("exit_code")
        s.first_text = str(rec.get("first_text") or "")
        s.turns = int(rec.get("turns") or 0)
        s.seq = int(rec.get("seq") or 0)
        s.last_result = rec.get("last_result")
        s.lost = list(rec.get("lost") or [])
        for p in rec.get("pending") or []:
            if isinstance(p, dict) and p.get("request_id"):
                s.pending[str(p["request_id"])] = p
        return s


def _compact(obj: dict) -> dict:
    """An event as kept: the long lists of `system/init` and the whole-file
    `tool_use_result` of a tool's result dropped."""
    if obj.get("type") == "system" and obj.get("subtype") == "init":
        return {k: v for k, v in obj.items() if k not in _INIT_DROP}
    if obj.get("type") == "user" and "tool_use_result" in obj:
        return {k: v for k, v in obj.items() if k != "tool_use_result"}
    return obj


def _result_summary(obj: dict) -> dict:
    return {k: obj.get(k) for k in ("subtype", "is_error", "stop_reason", "terminal_reason",
                                    "num_turns", "ttft_ms", "duration_ms", "total_cost_usd")}


# --- the supervisor -----------------------------------------------------------------

class Supervisor:
    """Every headless session this host holds, and the processes behind them."""

    def __init__(self, root: Path | None = None, program: str = "") -> None:
        self.root = root or state_root()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.program = program
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.sessions: dict[str, Session] = {}

    # -- start-up and shut-down --

    def load(self) -> None:
        """Take over the records a previous instance left. Nothing it ran is
        still ours: a session it left live is `ended`, and a permission
        request it left pending is `lost` (the CLI will not re-send it)."""
        self._kill_orphans({r["session"] for r in records(self.root)})
        with self.lock:
            for rec in records(self.root):
                s = Session.from_record(rec)
                if s.state in LIVE_STATES:
                    s.set_state("ended")
                if s.pending:
                    now = time.time()
                    s.lost.extend({**p, "lost_at": round(now, 3),
                                   "why": "the session host restarted"}
                                  for p in s.pending.values())
                    s.pending.clear()
                s.queued.clear()
                self.sessions[s.id] = s
                self._save(s)

    def _kill_orphans(self, ours: set[str]) -> None:
        """Terminate agent processes a crashed instance left running for one of
        our sessions: two writers on one session interleave its transcript."""
        me = os.getpid()
        for d in Path("/proc").iterdir() if Path("/proc").is_dir() else []:
            if not d.name.isdigit() or int(d.name) == me:
                continue
            try:
                env = (d / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            for e in env:
                if e.startswith(b"MEDIA_SESSIOND_SESSION="):
                    sid = e.split(b"=", 1)[1].decode(errors="replace")
                    if sid in ours:
                        log.warning("sessiond: ending orphan %s for %s", d.name, sid[:8])
                        try:
                            os.kill(int(d.name), signal.SIGTERM)
                        except OSError:
                            pass
                    break

    def shutdown(self) -> None:
        """Park everything live, as gently as a close: stdin, then signals."""
        with self.lock:
            live = [s for s in self.sessions.values() if s.live]
            for s in live:
                s.parking = True
                self._close_stdin(s)
        deadline = time.monotonic() + close_grace_s()
        for s in live:
            proc = s.proc
            if proc is None:
                continue
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._terminate(proc)

    # -- helpers --

    def _get(self, session: str) -> Session:
        s = self.sessions.get(session)
        if s is None:
            raise Refused(f"no headless session {session[:8]}", "not_found")
        return s

    def _save(self, s: Session) -> None:
        try:
            _write_atomic(record_path(s.id, self.root), s.record())
        except OSError as e:
            log.warning("sessiond: could not write the record for %s (%s)", s.id[:8], e)

    def _log_event(self, s: Session, ev: dict) -> None:
        path = self.root / f"{s.id}.events.jsonl"
        try:
            with open(path, "a") as fh:
                fh.write(json.dumps(ev) + "\n")
            s.file_lines += 1
            if s.file_lines > _FILE_TRIM_AT:
                keep = path.read_text().splitlines()[-EVENTS_KEEP:]
                tmp = path.with_name(f".{path.name}.trim")
                tmp.write_text("\n".join(keep) + "\n")
                os.replace(tmp, path)
                s.file_lines = len(keep)
        except OSError:
            pass

    def _program(self) -> str:
        if self.program:
            return self.program
        env = (os.environ.get("MEDIA_SESSIOND_CLAUDE") or "").strip()
        if env:
            return env
        from agent_media_core import harnesses

        return harnesses.program("claude") or "claude"

    def _env(self, s: Session, exe: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k in KEEP or not k.startswith(DROP_PREFIXES)}
        from agent_media_core import harnesses

        path = harnesses.bin_path()
        if os.path.isabs(exe):
            path = os.path.dirname(exe) + os.pathsep + path
        env["PATH"] = path
        env["MEDIA_SOURCE_KIND"] = "headless"
        env["MEDIA_SESSIOND_SESSION"] = s.id
        if s.workspace:
            env["MEDIA_SOURCE_WORKSPACE"] = s.workspace
        return env

    def _argv(self, s: Session, exe: str, resume: bool) -> list[str]:
        from . import permissions

        argv = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--permission-prompt-tool", "stdio"]
        argv += permissions.cli_args(s.permissions, s.cwd, s.id, self.root)
        model = (os.environ.get("MEDIA_HEADLESS_MODEL") or "").strip()
        if model:
            argv += ["--model", model]
        # More `claude` flags, for debugging and smoke runs — e.g.
        # `--setting-sources project,local` keeps the user's hooks (and
        # speech) out of a test session.
        extra = (os.environ.get("MEDIA_HEADLESS_EXTRA_ARGS") or "").strip()
        if extra:
            import shlex

            argv += shlex.split(extra)
        argv += ["--resume", s.id] if resume else ["--session-id", s.id]
        return argv

    def _spawn(self, s: Session, *, resume: bool) -> None:
        if not os.path.isdir(s.cwd):
            raise Refused(f"no such directory {s.cwd!r}", "bad_cwd")
        exe = self._program()
        argv = self._argv(s, exe, resume)
        try:
            proc = subprocess.Popen(argv, cwd=s.cwd, env=self._env(s, exe),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1,
                                    start_new_session=True)
        except OSError as e:
            raise Refused(f"could not start {os.path.basename(exe)} ({e})", "spawn_failed")
        s.proc = proc
        s.closing = s.parking = False
        s.exit_code = None
        s.queued.clear()
        s.acked.clear()
        s.set_state("starting")
        s.last_event_at = time.time()
        self._save(s)
        threading.Thread(target=self._read, args=(s, proc), daemon=True,
                         name=f"sessiond-out-{s.id[:8]}").start()
        threading.Thread(target=self._read_err, args=(s, proc), daemon=True,
                         name=f"sessiond-err-{s.id[:8]}").start()
        log.info("sessiond: %s %s pid %d in %s", "resumed" if resume else "started",
                 s.id[:8], proc.pid, s.cwd)

    def _read_err(self, s: Session, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            s.stderr_tail.append(line.rstrip("\n")[:500])

    def _read(self, s: Session, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                try:
                    self._on_event(s, obj)
                except Exception:  # noqa: BLE001 — one odd event must not end the reader
                    log.exception("sessiond: event for %s", s.id[:8])
        rc = proc.wait()
        self._on_exit(s, proc, rc)

    def _write(self, s: Session, obj: dict) -> None:
        proc = s.proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise Refused(f"session {s.id[:8]} is not running", "not_live")
        with s.write_lock:
            try:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise Refused(f"session {s.id[:8]} stopped taking input ({e})", "not_live")

    def _close_stdin(self, s: Session) -> None:
        proc = s.proc
        if proc is not None and proc.stdin is not None:
            with s.write_lock:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass

    def _wind_down(self, s: Session, proc: subprocess.Popen) -> None:
        """After stdin closed: the grace period, then SIGTERM, then SIGKILL."""
        def run() -> None:
            try:
                proc.wait(close_grace_s())
            except subprocess.TimeoutExpired:
                self._terminate(proc)
        threading.Thread(target=run, daemon=True, name=f"sessiond-close-{s.id[:8]}").start()

    # -- events --

    def _on_event(self, s: Session, obj: dict) -> None:
        t, sub = obj.get("type"), obj.get("subtype")
        if t == "stream_event":
            return
        with self.cond:
            now = time.time()
            s.seq += 1
            ev = {"seq": s.seq, "t": round(now, 3), **_compact(obj)}
            s.events.append(ev)
            self._log_event(s, ev)
            s.last_event_at = now
            before = (s.state, len(s.pending))
            if t == "system" and sub == "init":
                s.set_state("approval" if s.pending else "working")
            elif t == "command_lifecycle":
                cid = str(obj.get("command_uuid") or "")
                state = obj.get("state")
                if state in ("queued", "started"):
                    s.acked.add(cid)
                if state == "queued":
                    s.queued.add(cid)
                elif state == "started":
                    s.queued.discard(cid)
                    if not s.pending:
                        s.set_state("working")
                elif state in ("completed", "cancelled"):
                    s.queued.discard(cid)
            elif t == "control_request":
                req = obj.get("request") or {}
                rid = str(obj.get("request_id") or "")
                if req.get("subtype") == "can_use_tool" and rid:
                    s.pending[rid] = {"request_id": rid, "request": req, "at": round(now, 3)}
                    s.set_state("approval")
                elif rid:
                    # Nothing else is ours to answer (hook callbacks, MCP
                    # messages are for SDK hosts); say so, or the CLI waits.
                    self._reply_error(s, rid, f"{req.get('subtype')} is not supported here")
            elif t == "control_response":
                resp = obj.get("response") or {}
                rid = str(resp.get("request_id") or "")
                if rid:
                    s.responses[rid] = resp
            elif t == "result":
                s.last_result = _result_summary(obj)
                s.set_state("approval" if s.pending else "waiting")
                if s.turns == 1 and not s.auto_titled and not obj.get("is_error"):
                    s.auto_titled = True
                    threading.Thread(target=self._auto_title, args=(s,), daemon=True,
                                     name="auto-title").start()
            if (s.state, len(s.pending)) != before or t in ("result", "control_request"):
                self._save(s)
            self.cond.notify_all()

    def _auto_title(self, s: Session) -> None:
        """Name a thread nobody has named, its first turn done (threads.py).

        Off a thread: naming is a gateway call of a few seconds, and the
        turn it follows is already answered. The name is filed by
        `name_unnamed` (shelf, name file, the library item) and typed into
        the live session here, because a running Claude Code writes its own
        title again every turn and would put the old one back.
        """
        from . import threads

        try:
            title = threads.name_unnamed(s.id)
            if not title:
                return
            self.rename(s.id, title)
        except Refused as e:
            log.debug("sessiond: auto-title %s not typed in (%s)", s.id[:8], e.error)
        except Exception:  # noqa: BLE001 — a nameless thread is not a failure
            log.exception("sessiond: auto-title %s", s.id[:8])
        else:
            log.info("sessiond: %s named %r", s.id[:8], title)

    def _reply_error(self, s: Session, rid: str, error: str) -> None:
        try:
            self._write(s, {"type": "control_response",
                            "response": {"subtype": "error", "request_id": rid, "error": error}})
        except Refused:
            pass

    def _on_exit(self, s: Session, proc: subprocess.Popen, rc: int) -> None:
        with self.cond:
            if s.proc is not proc:
                return            # a newer process has already replaced it
            s.proc = None
            s.exit_code = rc
            if s.pending:
                now = time.time()
                why = ("the session was closed" if s.closing else
                       "the session was parked" if s.parking else "the agent process ended")
                s.lost.extend({**p, "lost_at": round(now, 3), "why": why}
                              for p in s.pending.values())
                s.pending.clear()
            s.queued.clear()
            s.set_state("closed" if s.closing else "parked" if s.parking else "ended")
            self._save(s)
            self.cond.notify_all()
        log.info("sessiond: %s exited %s (%s)", s.id[:8], rc, s.state)

    # -- capacity --

    def _ensure_room(self, exclude: Session | None = None) -> None:
        """Make room for one more live process, parking the least recently
        used idle one if the cap is reached; refuse when none is idle."""
        live = [x for x in self.sessions.values() if x.running and x is not exclude]
        if len(live) < max_live():
            return
        idle = sorted((x for x in live if x.state == "waiting" and not x.pending
                       and not x.queued), key=lambda x: x.last_event_at)
        if not idle:
            raise Refused(f"{len(live)} phone sessions are busy on this host; "
                          "close one or wait for one to finish", "busy")
        self._park(idle[0])

    def _park(self, s: Session) -> None:
        proc = s.proc
        if proc is None:
            return
        s.parking = True
        self._close_stdin(s)
        self._wind_down(s, proc)

    def park_idle(self, now: float | None = None) -> list[str]:
        """Park every session idle past `MEDIA_SESSIOND_IDLE`. Never one that
        is working, waiting on an approval, or holding queued messages."""
        now = now or time.time()
        parked = []
        with self.lock:
            for s in list(self.sessions.values()):
                if (s.running and s.state == "waiting"
                        and not s.pending and not s.queued
                        and now - s.last_event_at >= idle_s()):
                    self._park(s)
                    parked.append(s.id)
        return parked

    # -- ops ---------------------------------------------------------------------

    def start(self, cwd: str, text: str, *, session: str = "", agent: str = "claude",
              workspace: str = "", permissions: str = "") -> dict:
        from . import permissions as perms

        if agent != "claude":
            raise Refused(f"no headless adapter for {agent}", "unsupported")
        if not (text or "").strip():
            raise Refused("empty message", "empty_text")
        session = session or str(uuid.uuid4())
        with self.lock:
            if session in self.sessions:
                raise Refused(f"session {session[:8]} already exists", "exists")
            self._ensure_room()
            s = Session(session, cwd, agent=agent, workspace=workspace,
                        permissions=perms.mode(permissions))
            s.first_text = " ".join(text.split())[:200]
            self._spawn(s, resume=False)
            self.sessions[session] = s
        return self.send(session, text, _fresh=True)

    def _await_exit(self, s: Session) -> None:
        """A process being parked or closed is on its way out: wait for it to
        go before starting its successor (two writers on one session)."""
        if s.proc is not None and (s.parking or s.closing):
            self.cond.wait_for(lambda: s.proc is None, close_grace_s() + 6.0)
            if s.proc is not None:
                raise Refused(f"session {s.id[:8]} is still shutting down", "busy")

    def send(self, session: str, text: str, *, uid: str = "", _fresh: bool = False) -> dict:
        if not (text or "").strip():
            raise Refused("empty message", "empty_text")
        uid = uid or str(uuid.uuid4())
        with self.lock:
            s = self._get(session)
            self._await_exit(s)
            resumed = False
            if not s.live:
                self._ensure_room(exclude=s)
                self._spawn(s, resume=True)
                resumed = True
            # A message typed while a question or permission is up answers
            # it: the CLI waits on the control response and would only queue
            # the message behind it, so the card sticks with nothing to press.
            # Decline each, saying the reply follows, and let the message run.
            for rid in list(s.pending):
                self._write(s, {"type": "control_response",
                                "response": {"subtype": "success", "request_id": rid,
                                             "response": {"behavior": "deny",
                                                          "message": TYPED_INSTEAD}}})
                del s.pending[rid]
            if s.state == "approval":
                s.set_state("working")
            busy = s.state == "working"
            self._write(s, {"type": "user", "session_id": "", "uuid": uid,
                            "message": {"role": "user",
                                        "content": [{"type": "text", "text": text}]},
                            "parent_tool_use_id": None})
            s.turns += 1
            if not s.first_text:
                s.first_text = " ".join(text.split())[:200]
            self._save(s)
            acked = self.cond.wait_for(lambda: uid in s.acked or not s.live, ACK_S)
            acked = uid in s.acked
        return {"session": session, "uuid": uid, "fresh": _fresh, "resumed": resumed,
                "queued": busy, "acked": acked, "state": s.state, "live": s.live,
                "pid": s.proc.pid if s.live else None}

    def rename(self, session: str, title: str) -> dict:
        """`/rename <title>` into a live session: Claude Code takes it under
        `-p` as a local command (no model call; it writes `custom-title` to
        the transcript and answers "Session renamed to: …" with a zero-cost
        `result` — measured 22 Sep 2026, 2.1.278). Not a turn: nothing counts
        it, and a parked session is not woken for it — the name is already in
        its transcript (`book_tracks.rename`), which its next resume reads.
        Behind a running turn it queues, like any message."""
        title = " ".join((title or "").split())
        if not title:
            raise Refused("no title", "empty_text")
        with self.lock:
            s = self._get(session)
            if not s.live:
                return {"session": session, "renamed": False,
                        "why": "headless sessions pick up the name on their next resume"}
            busy = s.state in ("working", "approval")
            uid = str(uuid.uuid4())
            self._write(s, {"type": "user", "session_id": "", "uuid": uid,
                            "message": {"role": "user",
                                        "content": [{"type": "text",
                                                     "text": f"/rename {title}"}]},
                            "parent_tool_use_id": None})
        return {"session": session, "renamed": True, "queued": busy, "uuid": uid}

    def resume(self, session: str) -> dict:
        with self.lock:
            s = self._get(session)
            self._await_exit(s)
            if s.live:
                return {"session": session, "opened": False, **self._brief(s)}
            self._ensure_room(exclude=s)
            self._spawn(s, resume=True)
            return {"session": session, "opened": True, **self._brief(s)}

    def interrupt(self, session: str, cancel_queued: bool = False) -> dict:
        """Interrupt the running turn, with a receipt. Only while working:
        a pending permission request is answered, never interrupted (§12)."""
        with self.lock:
            s = self._get(session)
            if not s.live:
                return {"session": session, "interrupted": False, "why": "not live",
                        **self._brief(s)}
            if s.state == "approval":
                return {"session": session, "interrupted": False,
                        "why": "waiting on a question", **self._brief(s)}
            if s.state not in ("working", "starting"):
                return {"session": session, "interrupted": False, "why": "not working",
                        **self._brief(s)}
            rid = "req_" + uuid.uuid4().hex[:12]
            req: dict = {"subtype": "interrupt"}
            if cancel_queued:
                req["cancel_queued"] = True
            self._write(s, {"type": "control_request", "request_id": rid, "request": req})
            self.cond.wait_for(lambda: rid in s.responses or not s.live, RECEIPT_S)
            receipt = (s.responses.pop(rid, None) or {}).get("response")
            self.cond.wait_for(lambda: s.state not in ("working", "starting") or not s.live,
                               SETTLE_S)
            return {"session": session, "interrupted": True, "why": None,
                    "receipt": receipt, **self._brief(s)}

    def answer(self, session: str, request_id: str, response: dict) -> dict:
        with self.lock:
            s = self._get(session)
            if request_id not in s.pending:
                lost = next((p for p in s.lost if p.get("request_id") == request_id), None)
                if lost:
                    raise Refused(f"that request was lost when {lost.get('why') or 'the session stopped'}; "
                                  "send a message to carry on", "lost", request=lost)
                raise Refused("that request is not pending", "not_pending",
                              pending=list(s.pending.values()))
            self._write(s, {"type": "control_response",
                            "response": {"subtype": "success", "request_id": request_id,
                                         "response": response}})
            del s.pending[request_id]
            if not s.pending and s.state == "approval":
                s.set_state("working")
            self._save(s)
            self.cond.notify_all()
            return {"session": session, "answered": request_id,
                    "pending": list(s.pending.values()), **self._brief(s)}

    def close(self, session: str) -> dict:
        with self.lock:
            s = self._get(session)
            proc = s.proc
            if not s.live or proc is None:
                if s.state != "closed":
                    s.set_state("closed")
                    self._save(s)
                return {"session": session, "closed": False, **self._brief(s)}
            s.closing = True
            self._close_stdin(s)
            self._wind_down(s, proc)
            return {"session": session, "closed": True, **self._brief(s)}

    def park(self, session: str) -> dict:
        with self.lock:
            s = self._get(session)
            if s.live:
                if s.pending:
                    raise Refused("it is waiting on an approval", "busy")
                self._park(s)
            return {"session": session, **self._brief(s)}

    def get(self, session: str) -> dict:
        with self.lock:
            return self._get(session).view()

    def list(self) -> list[dict]:
        with self.lock:
            return [s.view() for s in self.sessions.values()]

    def events(self, session: str, since: int = 0, wait: float = 0.0) -> dict:
        with self.cond:
            s = self._get(session)
            if wait > 0 and s.seq <= since:
                self.cond.wait_for(lambda: s.seq > since, min(wait, 30.0))
            evs = [e for e in s.events if e["seq"] > since][-200:]
            return {"session": session, "seq": s.seq, "events": evs, **self._brief(s)}

    @staticmethod
    def _brief(s: Session) -> dict:
        return {"state": s.state, "live": s.live, "pid": s.proc.pid if s.live else None,
                "pending": list(s.pending.values())}

    # -- the wire --

    def handle(self, req: dict) -> dict:
        op = str(req.get("op") or "")
        sid = str(req.get("session") or "")
        try:
            if op == "ping":
                return {"ok": True, "pid": os.getpid()}
            if op == "list":
                return {"ok": True, "sessions": self.list()}
            if op == "get":
                return {"ok": True, **self.get(sid)}
            if op == "start":
                return {"ok": True, **self.start(
                    str(req.get("cwd") or ""), str(req.get("text") or ""), session=sid,
                    agent=str(req.get("agent") or "claude"),
                    workspace=str(req.get("workspace") or ""),
                    permissions=str(req.get("permissions") or ""))}
            if op == "send":
                return {"ok": True, **self.send(sid, str(req.get("text") or ""),
                                                uid=str(req.get("uuid") or ""))}
            if op == "rename":
                return {"ok": True, **self.rename(sid, str(req.get("title") or ""))}
            if op == "resume":
                return {"ok": True, **self.resume(sid)}
            if op == "interrupt":
                return {"ok": True, **self.interrupt(sid, bool(req.get("cancel_queued")))}
            if op == "answer":
                resp = req.get("response")
                if not isinstance(resp, dict):
                    raise Refused("an answer needs a response", "bad_request")
                return {"ok": True, **self.answer(sid, str(req.get("request_id") or ""), resp)}
            if op == "close":
                return {"ok": True, **self.close(sid)}
            if op == "park":
                return {"ok": True, **self.park(sid)}
            if op == "events":
                return {"ok": True, **self.events(sid, int(req.get("since") or 0),
                                                  float(req.get("wait") or 0))}
            return {"ok": False, "error": f"unknown op {op!r}", "code": "bad_request"}
        except Refused as e:
            return {"ok": False, "error": e.error, "code": e.code, **e.extra}


# --- the socket ---------------------------------------------------------------------

class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: D401 — socketserver's name
        sup: Supervisor = self.server.supervisor  # type: ignore[attr-defined]
        for raw in self.rfile:
            try:
                req = json.loads(raw)
            except ValueError:
                req = None
            if not isinstance(req, dict):
                out = {"ok": False, "error": "not a JSON object", "code": "bad_request"}
            else:
                try:
                    out = sup.handle(req)
                except Exception as e:  # noqa: BLE001 — answer, and keep serving
                    log.exception("sessiond: %s failed", req.get("op"))
                    out = {"ok": False, "error": f"sessiond failed ({e})", "code": "internal"}
            try:
                self.wfile.write((json.dumps(out) + "\n").encode())
                self.wfile.flush()
            except OSError:
                return


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, supervisor: Supervisor) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        old = os.umask(0o177)
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self.supervisor = supervisor
        self.path = path


def serve(path: Path | None = None, supervisor: Supervisor | None = None,
          *, tick: float = 30.0) -> tuple[Server, threading.Thread]:
    """Start sessiond in this process (for tests and for `main`): the socket
    served on a thread, and the idle-parking tick on another."""
    sup = supervisor or Supervisor()
    srv = Server(path or socket_path(), sup)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                         daemon=True, name="sessiond-socket")
    t.start()

    def idle() -> None:
        while not getattr(srv, "_stopped", False):
            time.sleep(tick)
            try:
                sup.park_idle()
            except Exception:  # noqa: BLE001
                log.exception("sessiond: idle sweep")
    threading.Thread(target=idle, daemon=True, name="sessiond-idle").start()
    return srv, t


def stop(srv: Server) -> None:
    srv._stopped = True  # type: ignore[attr-defined]
    srv.shutdown()
    srv.server_close()
    try:
        srv.path.unlink()
    except OSError:
        pass
    srv.supervisor.shutdown()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="media sessiond",
                                 description="Own headless agent sessions (see sessiond.py).")
    ap.add_argument("--socket", default="", help="unix socket path (default: MEDIA_SESSIOND_SOCKET, "
                                                  "else $XDG_RUNTIME_DIR/agent-media/sessiond.sock)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    sup = Supervisor()
    sup.load()
    srv, _t = serve(Path(a.socket) if a.socket else None, sup)
    log.info("sessiond: listening on %s (%d records)", srv.path, len(sup.sessions))
    done = threading.Event()

    def on_term(*_a) -> None:
        done.set()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    done.wait()
    log.info("sessiond: stopping; parking %d live session(s)",
             sum(1 for s in sup.sessions.values() if s.live))
    stop(srv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
