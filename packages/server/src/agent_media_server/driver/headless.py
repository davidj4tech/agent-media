"""The headless driver: a session sessiond holds, reached over its socket.

The canvas side of docs/proposals/2026-09-22-headless-sessions.md. Nothing
here holds a process: `media-sessiond` (sessiond.py) does, and this module
speaks its JSON-lines socket, turns its answers into the routes' shapes, and
turns a pending `can_use_tool` request into the thread's `approval` —
structured (server-contract.md §6.2 "approval", headless form) with the v0
numbered options alongside, so a client that only knows `choice` still works.

Which sessions are headless is read from sessiond's records on disk
(`owns`), not asked over the socket: a headless thread stays headless while
sessiond is down (its routes then answer 503), and is never revived in a pane
by accident.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sys

from . import HEADLESS, Caps

#: sessiond's refusal codes, as the routes' statuses.
_STATUS = {"not_found": 404, "not_live": 404, "bad_cwd": 404, "busy": 503, "down": 503,
           "spawn_failed": 500, "internal": 500, "empty_text": 400, "bad_request": 400,
           "unsupported": 400, "exists": 409, "not_pending": 409, "lost": 409,
           "refused": 400}

#: sessiond's states as the contract names them (§6.1 /sessions/state, §11).
_CONTRACT_STATE = {"starting": "working", "working": "working", "waiting": "waiting",
                   "approval": "approval"}

#: A tool approval's numbered options, for a v0 client.
_ALLOW, _DENY = 1, 2
_TOOL_OPTIONS = [{"n": _ALLOW, "label": "Allow", "detail": ""},
                 {"n": _DENY, "label": "Deny", "detail": ""}]
#: What the agent is told when a tool is refused from the phone with no words.
DENY_MESSAGE = "The user declined this from the phone. Do not retry it; ask what to do instead."
#: Longest string kept in an approval's `input`.
_INPUT_MAX = 300


def call(op: str, *, timeout: float = 30.0, **kw) -> dict:
    """One request to sessiond. A sessiond that is not there answers like a
    refusal (`code: "down"`), never raises."""
    from .. import sessiond

    path = sessiond.socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall((json.dumps({"op": op, **kw}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        out = json.loads(buf or b"{}")
    except (OSError, ValueError) as e:
        print(f"headless: sessiond {op} failed ({e})", file=sys.stderr)
        return {"ok": False, "code": "down",
                "error": "the session host (media-sessiond) is not running"}
    return out if isinstance(out, dict) else {"ok": False, "code": "internal",
                                              "error": "sessiond answered nonsense"}


def _failed(r: dict, **extra) -> tuple[bool, dict]:
    code = str(r.get("code") or "internal")
    return False, {"error": str(r.get("error") or "the session host refused"),
                   "status": _STATUS.get(code, 500), "code": code, **extra}


def compose(text: str, quote: str = "") -> str:
    """The message as the agent gets it. Unlike a pane, a headless session
    takes newlines as they are, so nothing is flattened and a quote rides as
    its own paragraph (a Markdown quote) instead of `Re: "…" —`."""
    text = (text or "").strip()
    quote = (quote or "").strip()
    if not quote:
        return text
    quoted = "\n".join("> " + ln for ln in quote.splitlines())
    return f"{quoted}\n\n{text}"


def contract_state(view: dict) -> str:
    if not view.get("live"):
        return "ended"
    return _CONTRACT_STATE.get(str(view.get("state") or ""), "waiting")


def _trim(value):
    if isinstance(value, str):
        return value if len(value) <= _INPUT_MAX else value[:_INPUT_MAX - 1] + "…"
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim(v) for v in value[:20]]
    return value


def approval_of(entry: dict) -> dict:
    """A pending `can_use_tool` request as the thread's `approval`.

    The headless form (server-contract.md §6.2): `id` is the CLI's own request
    id, which `/session/answer` echoes; `kind` is `"tool"` or `"question"`
    (AskUserQuestion); `tool`, `display_name`, `input_summary` (the same one-
    line summary a message's tool part carries), `input` (trimmed),
    `description`, `blocked_path`, `tool_use_id` and `suggestions` are the
    request's own fields; a question carries `questions` in AskUserQuestion's
    shape. The v0 fields — `question`, numbered `options`, `key`, `partial`,
    `agent` — are filled too, so a client that answers by `choice` works.
    """
    from .. import transcript

    req = entry.get("request") or {}
    rid = str(entry.get("request_id") or "")
    tool = str(req.get("tool_name") or "")
    inp = req.get("input") if isinstance(req.get("input"), dict) else {}
    out = {"id": rid, "tool": tool, "display_name": str(req.get("display_name") or tool),
           "input_summary": transcript.input_summary(tool, inp), "input": _trim(inp),
           "description": req.get("description") or "",
           "blocked_path": req.get("blocked_path"),
           "tool_use_id": req.get("tool_use_id") or "",
           "suggestions": req.get("permission_suggestions") or [],
           "at": entry.get("at"), "partial": False,
           "key": hashlib.sha1(rid.encode()).hexdigest()[:12], "agent": "claude"}
    if tool == "AskUserQuestion":
        qs = []
        for q in inp.get("questions") or []:
            if not isinstance(q, dict):
                continue
            qs.append({"question": str(q.get("question") or ""),
                       "header": str(q.get("header") or ""),
                       "options": [{"label": str(o.get("label") or ""),
                                    "description": str(o.get("description") or "")}
                                   for o in q.get("options") or [] if isinstance(o, dict)],
                       "multiSelect": bool(q.get("multiSelect"))})
        out.update(kind="question", questions=qs,
                   question=qs[0]["question"] if qs else "")
        one = len(qs) == 1 and not qs[0]["multiSelect"]
        # Numbered only when a number can answer it: one single-select
        # question. Several questions, multi-select or free text need the
        # structured answer.
        out["options"] = ([{"n": i + 1, "label": o["label"], "detail": o["description"]}
                           for i, o in enumerate(qs[0]["options"])] if one else [])
        out["partial"] = not one
    else:
        summary = out["input_summary"]
        out.update(kind="tool",
                   question=f"Allow {out['display_name']}" + (f": {summary}?" if summary else "?"),
                   options=list(_TOOL_OPTIONS))
    return out


def _answers_for(qs: list[dict], answers) -> dict | str:
    """`answers` checked against the questions: `{question: label | [labels]
    | free text}` → `{question: "label, label" | text}` (the spike's shape:
    multi-select joined with ", "). A string is a reason it will not do."""
    if not isinstance(answers, dict) or not answers:
        return "answers needed: {question: label, [labels] or your own words}"
    known = {q["question"] for q in qs}
    out = {}
    for q, a in answers.items():
        if q not in known:
            return f"no such question {q!r}"
        if isinstance(a, list):
            a = ", ".join(str(x) for x in a if str(x).strip())
        a = str(a if a is not None else "").strip()
        if not a:
            return f"no answer for {q!r}"
        out[q] = a
    missing = [q for q in known if q not in out]
    if missing:
        return f"no answer for {missing[0]!r}"
    return out


class HeadlessDriver:
    kind = HEADLESS
    caps = Caps(interrupt=True, structured_approvals=True, multi_select=True,
                free_text_answers=True, live_events=True)
    #: Harnesses with a headless adapter.
    agents = ("claude",)

    # -- ownership and reading --

    def record(self, session: str) -> dict | None:
        from .. import sessiond

        return sessiond.read_record(session)

    def owns(self, session: str) -> bool:
        return self.record(session) is not None

    def cwd_of(self, session: str) -> str:
        return str((self.record(session) or {}).get("cwd") or "")

    def _get(self, session: str) -> dict:
        r = call("get", session=session, timeout=5.0)
        if r.get("ok"):
            return r
        rec = self.record(session) or {}
        # sessiond down (or it lost the session): the record on disk, and
        # nothing is live without the host that would be holding it.
        return {**rec, "live": False, "pending": [], "down": r.get("code") == "down"}

    def view(self, session: str) -> dict:
        """`{"state", "live", "pane": None, "approval", "pid", "raw"}`."""
        v = self._get(session)
        pending = v.get("pending") or []
        return {"state": contract_state(v), "live": bool(v.get("live")), "pane": None,
                "approval": approval_of(pending[0]) if pending and v.get("live") else None,
                "pid": v.get("pid"), "raw": v.get("state"),
                "lost": v.get("lost") or [], "down": bool(v.get("down"))}

    def rows(self) -> list[dict]:
        """Every session sessiond holds, as its views (the socket's `list`,
        else the records on disk, none of them live)."""
        from .. import sessiond

        r = call("list", timeout=5.0)
        if r.get("ok"):
            return list(r.get("sessions") or [])
        return [{**rec, "live": False} for rec in sessiond.records()]

    def state(self, session: str) -> dict:
        v = self.view(session)
        return {"state": v["state"], "live": v["live"], "pane": None}

    def approval(self, session: str) -> dict | None:
        return self.view(session)["approval"]

    # -- acting --

    def start(self, *, agent, cwd, text, host="", flags=(), quote=""):
        """A fresh headless session. `host` (the tmux session a pane would
        have opened in) becomes its workspace — what speech is filed and
        voiced under. `flags` are amux's pane flags and are not used: the
        permission profile is sessiond's (permissions.py)."""
        from .. import send

        r = call("start", cwd=cwd, text=compose(text, quote), agent=agent, workspace=host,
                 timeout=30.0)
        if not r.get("ok"):
            return _failed(r, pane=None)
        session = str(r.get("session") or "")
        send._record_turn(session, text)
        return True, {"session": session, "pane": None, "opened": True, "fresh": True,
                      "tmux": None, "agent": agent, "submitted": True,
                      "driver": HEADLESS, "acked": bool(r.get("acked"))}

    def send(self, session, body, text, *, quote=""):
        from .. import archive, rest, send

        r = call("send", session=session, text=compose(text, quote), timeout=30.0)
        if not r.get("ok"):
            return _failed(r, session=session, pane=None)
        send._record_turn(session, text)
        archive.unarchive_quietly(session)
        rest.clear_quietly(session)
        return True, {"session": session, "pane": None, "opened": bool(r.get("resumed")),
                      "submitted": True, "driver": HEADLESS,
                      "queued": bool(r.get("queued")), "acked": bool(r.get("acked")),
                      "uuid": r.get("uuid")}

    def resume(self, session):
        from .. import rest

        r = call("resume", session=session, timeout=30.0)
        if not r.get("ok"):
            return _failed(r, pane=None)
        if r.get("opened"):
            rest.clear_quietly(session)
        return True, {"session": session, "pane": None, "live": True,
                      "opened": bool(r.get("opened")), "driver": HEADLESS}

    def close(self, session):
        r = call("close", session=session, timeout=15.0)
        if not r.get("ok"):
            return _failed(r)
        return True, {"session": session, "pane": None, "live": False,
                      "closed": bool(r.get("closed")), "driver": HEADLESS}

    def interrupt(self, session):
        r = call("interrupt", session=session, timeout=15.0)
        if not r.get("ok"):
            return _failed(r, interrupted=False)
        return True, {"interrupted": bool(r.get("interrupted")), "why": r.get("why"),
                      "state": contract_state(r), "pane": None,
                      "receipt": r.get("receipt")}

    def answer(self, session, request):
        """Answer a pending request. Structured — `request_id` + `decision`
        ("allow" | "deny") + `answers` for a question + `message` for a deny —
        or numbered (`choice` + `key`, the v0 form: 1 allow, 2 deny; a single
        single-select question by its option's number)."""
        v = self._get(session)
        if not v.get("live"):
            if v.get("down"):
                return False, {"error": "the session host (media-sessiond) is not running",
                               "status": 503, "code": "down"}
            return False, {"error": f"session {session[:8]} is not live", "status": 404}
        pending = v.get("pending") or []
        current = approval_of(pending[0]) if pending else None
        rid = str(request.get("request_id") or "")
        if rid:
            entry = next((p for p in pending if p.get("request_id") == rid), None)
            if entry is None:
                lost = next((p for p in v.get("lost") or [] if p.get("request_id") == rid), None)
                if lost:
                    return False, {"error": f"that request was lost when {lost.get('why') or 'the session stopped'}; "
                                            "send a message to carry on",
                                   "status": 409, "code": "lost", "approval": current}
                if not pending:
                    return False, {"error": "that session is not waiting on a question",
                                   "status": 409}
                return False, {"error": "the question has changed", "status": 409,
                               "approval": current}
            decision = str(request.get("decision") or "").strip().lower()
            if not decision and request.get("answers"):
                decision = "allow"
        else:
            if not current:
                return False, {"error": "that session is not waiting on a question",
                               "status": 409}
            key = str(request.get("key") or "")
            if key and key != current["key"]:
                return False, {"error": "the question has changed", "status": 409,
                               "approval": current}
            entry = pending[0]
            rid = current["id"]
            choice = int(request.get("choice") or 0)
            if choice not in {o["n"] for o in current["options"]}:
                return False, {"error": f"no option {choice}", "status": 400,
                               "approval": current}
            if current["kind"] == "tool":
                decision = "allow" if choice == _ALLOW else "deny"
            else:
                decision = "allow"
                label = next(o["label"] for o in current["options"] if o["n"] == choice)
                request = {**request, "answers": {current["questions"][0]["question"]: label}}
        appr = approval_of(entry)
        if decision not in ("allow", "deny"):
            return False, {"error": "decision must be allow or deny", "status": 400,
                           "approval": appr}
        req = entry.get("request") or {}
        inp = req.get("input") if isinstance(req.get("input"), dict) else {}
        if decision == "deny":
            response = {"behavior": "deny",
                        "message": str(request.get("message") or "").strip() or DENY_MESSAGE}
        elif appr["kind"] == "question":
            got = _answers_for(appr["questions"], request.get("answers"))
            if isinstance(got, str):
                return False, {"error": got, "status": 400, "approval": appr}
            response = {"behavior": "allow",
                        "updatedInput": {"questions": inp.get("questions") or [], "answers": got}}
        else:
            response = {"behavior": "allow", "updatedInput": inp}
        r = call("answer", session=session, request_id=rid, response=response, timeout=10.0)
        if not r.get("ok"):
            return _failed(r, approval=current)
        left = r.get("pending") or []
        nxt = approval_of(left[0]) if left else None
        out = {"session": session, "pane": None, "request_id": rid, "decision": decision,
               "waiting": bool(nxt), "approval": nxt, "driver": HEADLESS}
        if "choice" in request and not request.get("request_id"):
            out["answered"] = int(request.get("choice") or 0)
            out["label"] = next((o["label"] for o in appr["options"]
                                 if o["n"] == out["answered"]), "")
        if appr["kind"] == "question" and decision == "allow":
            out["answers"] = response["updatedInput"]["answers"]
        return True, out
