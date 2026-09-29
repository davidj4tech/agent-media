#!/usr/bin/env python3
"""A stand-in for `opencode serve`, for the headless opencode tests.
Standard library only.

It answers the routes sessiond_opencode.py uses, as opencode 1.18.33 did in
the spike (29 Sep 2026) — the password as HTTP basic auth, `?directory=` on
every session route, `/global/event` as SSE with `{directory, payload}` — with
none of the model behind them. What a prompt does is chosen by its words:

  tool: CMD   a bash permission request; `once` runs it, `reject` does not
  ask         a question (one single-select)
  slow        busy until aborted
  anything    busy, then idle

Every request is appended to $FAKE_OPENCODE_LOG as `METHOD PATH BODY`.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import queue
import sys
import threading
import time
import urllib.parse
import uuid

LOG = os.environ.get("FAKE_OPENCODE_LOG", "")
PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
SUBS: list[queue.Queue] = []
LOCK = threading.Lock()
SESSIONS: dict[str, dict] = {}
PENDING: dict[str, dict] = {}       # request id → {kind, sid, props, event}
STATUS: dict[str, str] = {}
ABORT: dict[str, threading.Event] = {}


def emit(directory: str, etype: str, props: dict) -> None:
    msg = {"directory": directory, "payload": {"id": "evt_" + uuid.uuid4().hex[:12],
                                               "type": etype, "properties": props}}
    with LOCK:
        for q in SUBS:
            q.put(msg)


def status(directory: str, sid: str, kind: str) -> None:
    STATUS[sid] = kind
    emit(directory, "session.status", {"sessionID": sid, "status": {"type": kind}})
    if kind == "idle":
        emit(directory, "session.idle", {"sessionID": sid})


def run_prompt(directory: str, sid: str, text: str) -> None:
    status(directory, sid, "busy")
    stop = ABORT.setdefault(sid, threading.Event())
    stop.clear()
    if text.startswith("tool: "):
        rid = "per_" + uuid.uuid4().hex[:12]
        cmd = text[6:].strip()
        props = {"id": rid, "sessionID": sid, "permission": "bash", "patterns": [cmd],
                 "metadata": {"command": cmd}, "always": [cmd.split()[0] + " *"],
                 "tool": {"messageID": "msg_x", "callID": "call_x"}}
        done = threading.Event()
        PENDING[rid] = {"kind": "permission", "sid": sid, "props": props, "done": done}
        emit(directory, "permission.asked", props)
        done.wait(30)
    elif text.strip() == "ask":
        rid = "que_" + uuid.uuid4().hex[:12]
        props = {"id": rid, "sessionID": sid,
                 "questions": [{"question": "Which colour?", "header": "Colour",
                                "options": [{"label": "Red", "description": "warm"},
                                            {"label": "Blue", "description": "cool"}],
                                "multiple": False}]}
        done = threading.Event()
        PENDING[rid] = {"kind": "question", "sid": sid, "props": props, "done": done}
        emit(directory, "question.asked", props)
        done.wait(30)
    elif text.strip() == "slow":
        stop.wait(30)
    else:
        time.sleep(0.05)
    status(directory, sid, "idle")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw else None

    def _send(self, code: int, obj=None):
        raw = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _route(self, method: str):
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        body = self._body() if method in ("POST", "PATCH") else None
        if LOG:
            with open(LOG, "a") as f:
                f.write(f"{method} {self.path} {json.dumps(body)}\n")
        want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if PASSWORD and self.headers.get("Authorization") != want:
            return self._send(401, {"error": "unauthorized"})
        parts = [p for p in url.path.split("/") if p]
        directory = q.get("directory", "")
        if parts == ["global", "health"]:
            return self._send(200, {"healthy": True})
        if parts == ["global", "event"]:
            return self._events()
        if method == "POST" and parts == ["session"]:
            sid = "ses_" + uuid.uuid4().hex[:26]
            SESSIONS[sid] = {"id": sid, "directory": directory, "body": body}
            return self._send(200, {"id": sid, "directory": directory})
        if method == "GET" and parts == ["session", "status"]:
            return self._send(200, {s: {"type": k} for s, k in STATUS.items() if k != "idle"})
        if method == "GET" and parts in (["permission"], ["question"]):
            kind = parts[0]
            return self._send(200, [p["props"] for p in PENDING.values() if p["kind"] == kind])
        if len(parts) == 3 and parts[0] == "session" and parts[2] == "prompt_async":
            text = " ".join(p.get("text", "") for p in (body or {}).get("parts", []))
            threading.Thread(target=run_prompt, args=(directory, parts[1], text),
                             daemon=True).start()
            return self._send(204)
        if len(parts) == 3 and parts[0] == "session" and parts[2] == "abort":
            ABORT.setdefault(parts[1], threading.Event()).set()
            return self._send(200, True)
        if method == "PATCH" and len(parts) == 2 and parts[0] == "session":
            SESSIONS.setdefault(parts[1], {}).update(body or {})
            return self._send(200, {"id": parts[1], **(body or {})})
        if len(parts) == 3 and parts[0] in ("permission", "question") and parts[2] in ("reply", "reject"):
            p = PENDING.pop(parts[1], None)
            if p is None:
                return self._send(404, {"message": "no such request"})
            if parts[0] == "permission":
                emit(directory, "permission.replied", {"sessionID": p["sid"], "requestID": parts[1],
                                                       "reply": (body or {}).get("reply")})
            elif parts[2] == "reply":
                emit(directory, "question.replied", {"sessionID": p["sid"], "requestID": parts[1],
                                                     "answers": (body or {}).get("answers")})
            else:
                emit(directory, "question.rejected", {"sessionID": p["sid"], "requestID": parts[1]})
            p["done"].set()
            return self._send(200, True)
        return self._send(404, {"message": f"no route {method} {url.path}"})

    def _events(self):
        q: queue.Queue = queue.Queue()
        with LOCK:
            SUBS.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.wfile.write(b'data: {"payload":{"type":"server.connected","properties":{}}}\n\n')
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=10)
                except queue.Empty:
                    continue
                self.wfile.write(b"data: " + json.dumps(msg).encode() + b"\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with LOCK:
                SUBS.remove(q)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] != "serve":
        print("fake opencode: only `serve`", file=sys.stderr)
        return 2
    port = int(args[args.index("--port") + 1])
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"opencode server listening on http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
