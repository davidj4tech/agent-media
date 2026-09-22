"""Background agents in a thread: `GET /threads/{session}/agents` (§6.12).

Claude Code keeps each subagent a session spawns (the Agent / Task tool) in
its own transcript beside the session's:

    ~/.claude/projects/<proj>/<session>.jsonl              the thread
    ~/.claude/projects/<proj>/<session>/subagents/
        agent-<id>.jsonl                                   the agent's own turns
        agent-<id>.meta.json                               {agentType, isFork,
                                                            description, toolUseId,
                                                            parentAgentId, spawnDepth, …}

The agent's records are all sidechain records; the parser reads them with
`sidechain=True` (transcript.py) and the log is its messages, read-only.

**Status**, conservatively — an agent is only "done" or "failed" on the
harness's own word, and only "running" while something says it could be:

1. **A terminal record.** The newest `<task-notification>` naming the agent
   (by task-id = its id, or by tool-use-id), in the thread's transcript or
   any subagent's (a child's lands in its parent agent's file, and as a
   `queue-operation` in the thread's): `completed` → `done`, `failed` →
   `failed`, `killed` → `stopped`. Failing that, the Agent tool's own
   `tool_result` when it is not the "launched in the background" answer (a
   foreground agent's result is its end): `is_error` → `failed` (an
   interrupt → `stopped`), else `done`.
2. **Resumed after it.** A background agent can be sent another message and
   run again, so the same id notifies more than once. When the agent's own
   file has records more than `GRACE_S` newer than its terminal record, the
   terminal record is old news: step 3 decides.
3. **No (current) terminal record**: `running` while the thread's session is
   live (a pane or a headless session — a background agent dies with the
   process that runs it) **and** the agent's file changed within `STALE_S`
   (30 min, `MEDIA_AGENTS_STALE_S`; a single tool call is capped well under
   that). Otherwise the terminal status it had, or `stopped` when it never
   had one: it was cut off (the session ended, was resumed, or the agent
   went quiet for longer than any tool runs).

`started_at` is the Agent call's own record (a fork's file opens with a copy
of its parent's turns, at their original times, so its first record is not
when it started; those copied turns are neither its steps nor its log).
`steps` is the agent's tool calls so far; `current_step` the latest one's
title (`activity.describe`, as `working.current` words it) while running,
else null.

**Cost.** Every file is scanned once, forwards, and then only its new bytes
(append-only, the same (inode, offset, seam) test as transcript.py); a line
is JSON-parsed only when a byte test says it could matter (a notification,
an Agent/Task call, a result for one, a tool call in an agent's own file).
An unchanged file is a stat. Read-only: nothing here writes.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from . import transcript

#: An agent's id as the file names carry it (`agent-<id>.jsonl`).
AGENT_ID = re.compile(r"[0-9a-z]{6,64}")
#: An agent with no terminal record is running only while its file changed
#: this recently (and the thread's session is live).
STALE_S = 1800.0
#: Records this much newer than the terminal one mean the agent was resumed.
GRACE_S = 5.0
#: Files whose scan is kept (a thread can have dozens of agents).
_CACHE_MAX = 512

_NOTE_STATUS = {"completed": "done", "failed": "failed", "killed": "stopped"}
_TID = re.compile(rb'"tool_use_id":"(toolu_[A-Za-z0-9_-]+)"')
_TS = re.compile(rb'"timestamp":"([^"]{10,40})"')
_TAG = {k: re.compile(rf"<{k}>\s*([^<]*?)\s*</{k}>") for k in ("task-id", "tool-use-id", "status")}


def stale_s() -> float:
    try:
        return float(os.environ.get("MEDIA_AGENTS_STALE_S") or STALE_S)
    except ValueError:
        return STALE_S


# --- scanning a file ----------------------------------------------------------------


class _Scan:
    """What one transcript file says about agents, folded forwards."""

    __slots__ = ("ino", "size", "mtime", "offset", "seam", "lock", "own", "first_at", "last_at",
                 "notes", "calls", "results", "step_ats", "last_title")

    def __init__(self, own: bool) -> None:
        self.ino = self.size = self.offset = 0
        self.mtime = 0.0
        self.seam = b""
        self.lock = threading.Lock()
        #: An agent's own file: its tool calls and timestamps are counted.
        self.own = own
        self.first_at: float | None = None
        self.last_at: float | None = None
        #: `{task-id or tool-use-id: (at, status)}` — the newest notification.
        self.notes: dict[str, tuple[float, str]] = {}
        #: `{tool_use_id: at}` — Agent/Task calls made in this file.
        self.calls: dict[str, float] = {}
        #: `{tool_use_id: (at, kind)}`, kind "async" | "done" | "failed" | "stopped".
        self.results: dict[str, tuple[float, str]] = {}
        #: When each tool call was made (a fork's file starts with a copy of
        #: its parent's turns, which are not its steps).
        self.step_ats: list[float] = []
        self.last_title = ""


_SCANS: dict[str, _Scan] = {}
_LOCK = threading.Lock()


def _reset_for_tests() -> None:
    with _LOCK:
        _SCANS.clear()
        _METAS.clear()


def _record_text(rec: dict) -> str:
    """The prompt text a notification can arrive as: a user record's, a
    queued command's, or a queue operation's."""
    kind = rec.get("type")
    if kind == "user":
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(b.get("text") or "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
    elif kind == "attachment":
        att = rec.get("attachment") or {}
        if att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
            return att["prompt"]
    elif kind == "queue-operation" and rec.get("operation") == "enqueue":
        return str(rec.get("content") or "")
    return ""


def _notes_in(text: str) -> list[tuple[str, str, str]]:
    """`(task_id, tool_use_id, status)` for each notification `text` *is* —
    it must start with one: a notification quoted inside other words (a
    prompt that pastes one) is not one."""
    if not text.lstrip().startswith("<task-notification>"):
        return []
    out = []
    for block in text.split("<task-notification>")[1:]:
        block = block.split("</task-notification>", 1)[0]
        # The fields come before <summary>/<result>, whose words could hold
        # anything: only the head is read.
        head = block.split("<summary>", 1)[0]
        got = {k: (r.search(head).group(1) if r.search(head) else "") for k, r in _TAG.items()}
        if got["status"] and (got["task-id"] or got["tool-use-id"]):
            out.append((got["task-id"], got["tool-use-id"], got["status"]))
    return out


def _result_kind(block: dict, rec: dict) -> str:
    tur = rec.get("toolUseResult")
    if isinstance(tur, dict) and (tur.get("isAsync") or tur.get("status") == "async_launched"):
        return "async"
    text = transcript._result_text(block.get("content")).strip()
    if text.startswith(("Async agent launched", "Fork started")):
        return "async"
    if block.get("is_error"):
        return "stopped" if "interrupted" in text[:200].lower() else "failed"
    return "done"


def _line(s: _Scan, raw: bytes) -> None:
    ts = None
    if s.own:
        k = raw.rfind(b'"timestamp":"')
        if k >= 0:
            m = _TS.match(raw, k)
            if m:
                ts = transcript.epoch(m.group(1).decode(errors="replace"))
        if ts is not None:
            if s.first_at is None:
                s.first_at = ts
            s.last_at = ts
    want = (b"task-notification" in raw
            or (b'"tool_use"' in raw and (b'"name":"Agent"' in raw or b'"name":"Task"' in raw
                                          or (s.own and b'"type":"assistant"' in raw)))
            or (b'"tool_result"' in raw and any(m.group(1).decode() in s.calls
                                                for m in _TID.finditer(raw))))
    if not want:
        return
    try:
        rec = json.loads(raw)
    except ValueError:
        return
    if not isinstance(rec, dict):
        return
    at = transcript.epoch(rec.get("timestamp")) or ts or 0.0
    if b"task-notification" in raw:
        for task, tool, status in _notes_in(_record_text(rec)):
            for key in (task, tool):
                if key and (key not in s.notes or s.notes[key][0] <= at):
                    s.notes[key] = (at, status)
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and rec.get("type") == "assistant":
            name = str(b.get("name") or "")
            if name in ("Agent", "Task") and b.get("id"):
                s.calls[str(b["id"])] = at
            if s.own:
                s.step_ats.append(at)
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                s.last_title = transcript._title(name, inp)
        elif b.get("type") == "tool_result" and rec.get("type") == "user":
            tid = str(b.get("tool_use_id") or "")
            if tid in s.calls:
                s.results[tid] = (at, _result_kind(b, rec))


def _scan(path: str, own: bool) -> _Scan | None:
    """The file's scan, brought up to date; None when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _LOCK:
        s = _SCANS.get(path)
        if s is None:
            s = _SCANS[path] = _Scan(own)
            while len(_SCANS) > _CACHE_MAX:
                _SCANS.pop(next(iter(_SCANS)))
    with s.lock:
        if (s.ino, s.size, s.mtime) == (st.st_ino, st.st_size, st.st_mtime) and s.offset:
            return s            # unchanged: a stat
        try:
            with open(path, "rb") as fh:
                end = transcript._complete_end(fh, st.st_size)
                if s.offset == end and s.ino == st.st_ino:
                    return s
                appended = (s.offset and s.ino == st.st_ino and s.offset <= end
                            and transcript._seam(fh, s.offset) == s.seam)
                if not appended:
                    fresh = _Scan(own)
                    for k in _Scan.__slots__:
                        if k != "lock":
                            setattr(s, k, getattr(fresh, k))
                fh.seek(s.offset)
                left, carry = end - s.offset, b""
                while left > 0:
                    buf = carry + fh.read(min(1 << 20, left))
                    left = end - fh.tell()
                    lines = buf.split(b"\n")
                    carry = lines.pop()
                    for raw in lines:
                        if raw:
                            _line(s, raw)
                if carry.strip():
                    _line(s, carry)
                s.offset, s.ino = end, st.st_ino
                s.size, s.mtime = st.st_size, st.st_mtime
                s.seam = transcript._seam(fh, end)
        except OSError:
            return None
    return s


# --- the meta files ------------------------------------------------------------------

_METAS: dict[str, tuple[float, dict]] = {}


def _meta(path: Path) -> dict:
    try:
        mt = path.stat().st_mtime
    except OSError:
        return {}
    got = _METAS.get(str(path))
    if got and got[0] == mt:
        return got[1]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    _METAS[str(path)] = (mt, data)
    return data


# --- the answer ---------------------------------------------------------------------


def subagents_dir(session: str) -> Path | None:
    """`<proj>/<session>/subagents` beside the session's transcript, or None
    when the session has no Claude Code transcript."""
    path = transcript.transcript_path(session)
    if not path:
        return None
    return Path(path).with_suffix("") / "subagents"


def _live(session: str) -> bool:
    from . import driver, sessions

    if sessions.live_sessions().get(session):
        return True
    hl = driver.headless_state(session)
    return bool(hl and hl.get("live"))


def agents(session: str, *, live: bool | None = None, now: float | None = None) -> list[dict] | None:
    """Every subagent of `session`, in start order — the §6.12 rows — or None
    when the session has no transcript. `live`: whether the thread's session
    is running, when the caller already knows (else it is looked up)."""
    path = transcript.transcript_path(session)
    if not path:
        return None
    d = Path(path).with_suffix("") / "subagents"
    try:
        files = sorted(d.glob("agent-*.jsonl"))
    except OSError:
        files = []
    if not files:
        return []
    ids = [f.name[len("agent-"):-len(".jsonl")] for f in files]
    pairs = [(i, f) for i, f in zip(ids, files) if AGENT_ID.fullmatch(i)]
    if not pairs:
        return []
    main = _scan(path, own=False)
    own = {i: _scan(str(f), own=True) for i, f in pairs}
    notes: dict[str, tuple[float, str]] = {}
    calls: dict[str, float] = {}
    results: dict[str, tuple[float, str]] = {}
    for s in [main, *own.values()]:
        if s is None:
            continue
        for k, v in s.notes.items():
            if k not in notes or notes[k][0] <= v[0]:
                notes[k] = v
        calls.update(s.calls)
        results.update(s.results)
    if live is None:
        live = _live(session)
    now = time.time() if now is None else now
    stale = stale_s()
    rows = []
    for aid, f in pairs:
        s = own[aid]
        meta = _meta(f.with_name(f"agent-{aid}.meta.json"))
        tid = str(meta.get("toolUseId") or "")
        try:
            mtime = f.stat().st_mtime
        except OSError:
            mtime = 0.0
        # When it was asked for: the call's own record. Not the file's first
        # record — a fork's file opens with a copy of its parent's turns, at
        # their original times.
        first = calls.get(tid) or (s.first_at if s else None) or mtime
        steps = sum(1 for t in (s.step_ats if s else []) if t >= first - 1.0)
        last = (s.last_at if s else None) or mtime
        # The terminal record: the newest notification, else a foreground
        # call's own result.
        term = max((notes[k] for k in (aid, tid) if k and k in notes),
                   key=lambda v: v[0], default=None)
        end = (term[0], _NOTE_STATUS.get(term[1], "")) if term else None
        if end and not end[1]:
            end = None          # "running" (or anything unknown) ends nothing
        if end is None and tid in results and results[tid][1] != "async":
            end = results[tid]
        resumed = end is not None and last > end[0] + GRACE_S
        if end is not None and not resumed:
            status, ended = end[1], end[0]
        elif live and now - mtime < stale:
            status, ended = "running", None
        elif end is not None:
            status, ended = end[1], end[0]
        else:
            status, ended = "stopped", last
        is_fork = bool(meta.get("isFork")) or meta.get("agentType") == "fork"
        parent = str(meta.get("parentAgentId") or "") or None
        rows.append({
            "id": aid,
            "description": str(meta.get("description") or ""),
            "agent_type": str(meta.get("agentType") or ""),
            "is_fork": is_fork,
            "parent_id": parent,
            "depth": int(meta.get("spawnDepth") or (2 if parent else 1)),
            "started_at": round(first, 3),
            "ended_at": round(ended, 3) if ended is not None else None,
            "status": status,
            "current_step": (s.last_title or None) if (s and steps and status == "running")
            else None,
            "steps": steps,
            "last_at": round(last, 3),
        })
    rows.sort(key=lambda r: (r["started_at"], r["id"]))
    return rows


def counts(rows: list[dict] | None) -> dict:
    """`{"running", "total"}` — the thread snapshot's and the `agents`
    event's summary."""
    rows = rows or []
    return {"running": sum(1 for r in rows if r["status"] == "running"), "total": len(rows)}


def agent_path(session: str, agent_id: str) -> str:
    """The agent's own transcript, or "" when there is none."""
    if not AGENT_ID.fullmatch(agent_id or ""):
        return ""
    d = subagents_dir(session)
    if d is None:
        return ""
    p = d / f"agent-{agent_id}.jsonl"
    return str(p) if p.is_file() else ""


def agent_log(session: str, agent_id: str, *, limit: int, before: str = "") -> tuple[bool, dict]:
    """The agent's own turns as §6.2.2 messages, read-only: `{"session",
    "agent": <row>, "messages", "older"}`. Its first message is the prompt
    it was given. Speech never joins (`spoken` is always null): a subagent's
    words are not spoken."""
    path = agent_path(session, agent_id)
    if not path:
        return False, {"error": "no such agent", "status": 404}
    row = next((r for r in agents(session) or [] if r["id"] == agent_id), None)
    got = transcript.messages_at(path, limit=limit, before=before, sidechain=True)
    if got is None or row is None:
        return False, {"error": "no such agent", "status": 404}
    msgs, older = got
    # A fork's file opens with a copy of its parent's conversation: those are
    # the thread's messages, not the agent's.
    own = [m for m in msgs if (m.get("at") or 0.0) >= row["started_at"] - 1.0]
    if len(own) < len(msgs):
        msgs, older = own, False
    transcript.strip_markers(msgs)
    if row["status"] != "running":
        for m in msgs:
            m["turn"]["running"] = False
    return True, {"session": session, "agent": row, "messages": msgs, "older": older}
