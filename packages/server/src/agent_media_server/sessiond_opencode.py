"""opencode in sessiond: one shared `opencode serve`, no tmux.

docs/proposals/2026-09-29-single-binary.md ("Does it need a multiplexer?").
A headless Claude session is its own `claude -p` process; an opencode one is a
session inside **one** `opencode serve` that sessiond holds for all of them,
because each server takes ~480 MB (measured 29 Sep 2026, 1.18.33) — a process
per chat would not fit on a phone. The API addresses a session's folder with
`?directory=`, and `/global/event` streams every folder's events, so one server
does.

What sessiond keeps is the same as for Claude — a `Session` per chat, its
record on disk, its state, its pending requests, an event count — so the
canvas's headless driver, the thread stream and the app see no difference.
What differs is only where it comes from:

* **state** from opencode's events: `session.status` busy → working,
  `session.idle` → waiting; `permission.asked` / `question.asked` → approval,
  their `replied`/`rejected` → back;
* **a pending request** is kept in Claude's `can_use_tool` shape (`request`
  with `tool_name`, `input`), so `driver/headless.approval_of` draws the same
  card; the answer, Claude-shaped (`behavior` allow/deny, a question's
  `updatedInput.answers`), is turned back into opencode's reply here;
* **the transcript** is opencode's own database, which the server already
  reads (transcript.OpencodeBuilder), and **speech** is opencode's plugin
  (packages/core/opencode/agent-media.js), loaded by the server as by the TUI.

A session is *attached* while the server runs and it has not been parked or
closed: its `proc` is the server's, so `live` means what it does for Claude.
Parking detaches it (nothing to stop: it is a row in a database); when no
opencode session is attached the server is stopped, and the next message
starts it again. The server is bound to 127.0.0.1 with a password of its own
(`OPENCODE_SERVER_PASSWORD`): on a phone every app can reach loopback.

Config (env): MEDIA_SESSIOND_OPENCODE (the binary; default
`harnesses.program("opencode")`), MEDIA_SESSIOND_OPENCODE_START (seconds to
wait for it to answer; 60), MEDIA_HEADLESS_OPENCODE_MODEL (`provider/model`
for a chat that names none; else opencode's own default).
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("agent-media.sessiond")

#: The process marker `Supervisor._kill_orphans` looks for.
HOST_MARK = "MEDIA_SESSIOND_OPENCODE_HOST"

#: Allowed without asking under the strict profile (permissions.py's
#: SAFE_TOOLS, in opencode's names); everything else asks. opencode takes the
#: last rule that matches, so the catch-all comes first.
SAFE = ("read", "glob", "grep", "list", "todowrite", "todoread", "websearch",
        "codesearch", "lsp", "skill", "question")

#: opencode's permission names as the tools the phone's card knows
#: (transcript.input_summary), and what of its metadata is their input.
_TOOL = {"bash": "Bash", "edit": "Edit", "write": "Write", "webfetch": "WebFetch",
         "task": "Task", "external_directory": "ExternalDirectory"}

#: Events that move a session's state, and so are counted; the rest (text
#: deltas, part updates) reach the phone through the database.
_TRACKED = frozenset({"session.status", "session.idle", "session.error",
                      "permission.asked", "permission.replied",
                      "question.asked", "question.replied", "question.rejected"})


def ruleset(profile: str) -> list[dict] | None:
    """A new session's permission rules: strict asks for anything that is not
    reading; normal leaves opencode's own config in charge."""
    from . import permissions

    if profile != permissions.STRICT:
        return None
    return ([{"permission": "*", "pattern": "*", "action": "ask"}]
            + [{"permission": p, "pattern": "*", "action": "allow"} for p in SAFE])


def model_ref(model: str) -> dict | None:
    """`provider/model` as opencode's prompt takes it."""
    model = (model or os.environ.get("MEDIA_HEADLESS_OPENCODE_MODEL") or "").strip()
    if "/" not in model:
        return None
    provider, _, mid = model.partition("/")
    return {"providerID": provider, "modelID": mid}


def request_of(kind: str, props: dict) -> dict:
    """opencode's permission or question request, as Claude's `can_use_tool`
    request (driver/headless.approval_of reads this shape)."""
    if kind == "question":
        qs = [{"question": str(q.get("question") or ""), "header": str(q.get("header") or ""),
               "options": [{"label": str(o.get("label") or ""),
                            "description": str(o.get("description") or "")}
                           for o in q.get("options") or [] if isinstance(o, dict)],
               "multiSelect": bool(q.get("multiple"))}
              for q in props.get("questions") or [] if isinstance(q, dict)]
        return {"subtype": "can_use_tool", "tool_name": "AskUserQuestion",
                "input": {"questions": qs}, "tool_use_id": (props.get("tool") or {}).get("callID", "")}
    perm = str(props.get("permission") or "")
    meta = props.get("metadata") if isinstance(props.get("metadata"), dict) else {}
    patterns = [str(p) for p in props.get("patterns") or []]
    tool = _TOOL.get(perm, perm)
    if perm == "bash":
        inp = {"command": str(meta.get("command") or " ".join(patterns))}
    elif perm in ("edit", "write"):
        inp = {"file_path": str(meta.get("filepath") or meta.get("filePath")
                                or (patterns[0] if patterns else ""))}
    elif perm == "webfetch":
        inp = {"url": str(meta.get("url") or (patterns[0] if patterns else ""))}
    else:
        inp = {**meta, "patterns": patterns}
    return {"subtype": "can_use_tool", "tool_name": tool, "display_name": tool, "input": inp,
            "description": ", ".join(patterns),
            "tool_use_id": (props.get("tool") or {}).get("callID", "")}


def question_answers(questions: list[dict], answers: dict) -> list[list[str]]:
    """Claude's `{question: "label, label" | words}` as opencode's answers: a
    list of labels per question, in order. A multi-select's labels were
    joined with ", " (driver/headless._answers_for); words that are not
    labels are the listener's own answer."""
    out = []
    for q in questions:
        a = str(answers.get(q.get("question"), "") or "").strip()
        labels = {o.get("label") for o in q.get("options") or []}
        if a in labels:
            out.append([a])
            continue
        parts = [p.strip() for p in a.split(", ")]
        out.append(parts if len(parts) > 1 and all(p in labels for p in parts) else [a])
    return out


class Host:
    """The one `opencode serve` sessiond runs, started when a session needs it."""

    def __init__(self, sup) -> None:
        self.sup = sup
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.password = ""
        self.lock = threading.Lock()
        self.stderr_tail: collections.deque = collections.deque(maxlen=40)
        self.stopping = False
        #: Set while `/global/event` is being read: a prompt sent before it
        #: is would lose its first events (a fast turn's idle, a permission).
        self.listening = threading.Event()

    @property
    def up(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # -- the process --

    def _program(self) -> str:
        env = (os.environ.get("MEDIA_SESSIOND_OPENCODE") or "").strip()
        if env:
            return env
        from agent_media_core import harnesses

        return harnesses.program("opencode") or "opencode"

    def ensure(self) -> subprocess.Popen:
        """The running server, started if it is not. Raises Refused."""
        from .sessiond import Refused

        with self.lock:
            if self.up:
                return self.proc  # type: ignore[return-value]
            exe = self._program()
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                self.port = s.getsockname()[1]
            self.password = secrets.token_urlsafe(24)
            env = self.sup._base_env(exe)
            env[HOST_MARK] = "1"
            env["OPENCODE_SERVER_PASSWORD"] = self.password
            try:
                proc = subprocess.Popen([exe, "serve", "--hostname", "127.0.0.1",
                                         "--port", str(self.port)],
                                        cwd=os.path.expanduser("~"), env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1,
                                        start_new_session=True)
            except OSError as e:
                raise Refused(f"could not start opencode ({e})", "spawn_failed")
            self.proc, self.stopping = proc, False
            threading.Thread(target=self._drain, args=(proc,), daemon=True,
                             name="sessiond-opencode-log").start()
            deadline = time.monotonic() + float(os.environ.get("MEDIA_SESSIOND_OPENCODE_START") or 60)
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    if self._call("GET", "/global/health", timeout=2.0)[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.3)
            else:
                self._terminate(proc)
            if proc.poll() is not None:
                self.proc = None
                tail = " / ".join(list(self.stderr_tail)[-3:])
                raise Refused(f"opencode's server did not start{': ' + tail if tail else ''}",
                              "spawn_failed")
            self.listening.clear()
            threading.Thread(target=self._events, args=(proc,), daemon=True,
                             name="sessiond-opencode-events").start()
            if not self.listening.wait(10):
                log.warning("sessiond: opencode's event stream is not connected yet")
            log.info("sessiond: opencode serve pid %d on 127.0.0.1:%d", proc.pid, self.port)
            return proc

    def _drain(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout or []:
            self.stderr_tail.append(line.rstrip("\n")[:500])
        rc = proc.wait()
        self.sup._oc_host_exited(proc, rc)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass

    def stop(self) -> None:
        with self.lock:
            proc, self.proc = self.proc, None
            self.stopping = True
        if proc is not None and proc.poll() is None:
            log.info("sessiond: stopping opencode serve pid %d", proc.pid)
            self._terminate(proc)

    # -- HTTP --

    def _auth(self) -> str:
        return "Basic " + base64.b64encode(f"opencode:{self.password}".encode()).decode()

    def _call(self, method: str, path: str, body=None, *, directory: str = "",
              timeout: float = 30.0):
        q = f"?{urllib.parse.urlencode({'directory': directory})}" if directory else ""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}{q}", data=data,
                                     method=method, headers={"Authorization": self._auth()})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, (json.loads(raw) if raw else None)
        except ValueError:
            return status, raw.decode(errors="replace")

    def call(self, method: str, path: str, body=None, *, directory: str = "",
             timeout: float = 30.0):
        """A request to the running server: its JSON, or Refused."""
        from .sessiond import Refused

        try:
            status, out = self._call(method, path, body, directory=directory, timeout=timeout)
        except OSError as e:
            raise Refused(f"opencode did not answer ({e})", "not_live")
        if status >= 400:
            msg = out.get("message") or out.get("error") if isinstance(out, dict) else out
            raise Refused(f"opencode refused it ({status}: {str(msg)[:200]})",
                          "not_found" if status == 404 else "refused")
        return out

    def _events(self, proc: subprocess.Popen) -> None:
        """Follow `/global/event` while this server runs; reconnect if it drops."""
        while proc.poll() is None and self.proc is proc:
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/global/event",
                                         headers={"Authorization": self._auth(),
                                                  "Accept": "text/event-stream"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    self.listening.set()
                    # Connected (again): what happened while not listening.
                    self.sup._oc_reconcile()
                    for raw in r:
                        if not raw.startswith(b"data:"):
                            continue
                        try:
                            obj = json.loads(raw[5:])
                        except ValueError:
                            continue
                        payload = obj.get("payload") if isinstance(obj, dict) else None
                        if isinstance(payload, dict) and payload.get("type") in _TRACKED:
                            try:
                                self.sup._on_oc_event(payload)
                            except Exception:  # noqa: BLE001 — one odd event must not end the reader
                                log.exception("sessiond: opencode event %s", payload.get("type"))
            except (OSError, ValueError):
                pass
            self.listening.clear()
            time.sleep(0.5)
