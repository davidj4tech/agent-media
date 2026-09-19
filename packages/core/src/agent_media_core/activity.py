"""What a session is doing between your message and its reply.

Claude Code's terminal shows it as it works: each step in a line of plain
English ("Fetch the APK CI built", "Read canvas.py"), a timer, and "Worked for
3m 38s" at the end. The phone showed three animated dots. This is the same
information, kept where the conversation page can read it.

Three hooks append to one file per session (``hook_main``): a ``turn`` marker
when a prompt is submitted, a ``step`` per tool call, and a ``stop`` marker
when the reply is done. ``work_for`` reads it back for the transcript: the
steps and duration of each finished turn, and the step in progress while one
is still running.

The hook runs on every tool call, so this module imports nothing outside the
standard library and is run by path with the system python (see ``hook_main``),
and the hooks are async: recording never delays a tool.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

#: A file is trimmed to its newest lines once it grows past this. Old turns
#: lose their step lists first; nothing else reads them.
MAX_BYTES = 512 * 1024
KEEP_LINES = 2000
#: A finished turn keeps at most this many steps for the list under it.
MAX_STEPS = 80
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: Tools that are bookkeeping, not work anyone would want listed.
_QUIET = {"TodoWrite", "ToolSearch", "TaskOutput", "BashOutput"}


def activity_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "agent-media" / "activity"


def _path(session: str) -> Optional[Path]:
    return activity_dir() / f"{session}.jsonl" if _UUID.match(session or "") else None


def _short(text: str, n: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


_SETUP = re.compile(r"^(cd|set|export|source|\.|sleep|true|:)\b|^[A-Za-z_][A-Za-z0-9_]*=")


def _command_gist(command: str) -> str:
    """The part of a shell command that says what it does.

    A command with no description reads as its first line, which is usually
    `cd ~/projects/…` or `tok=$(grep …)` — setup, not the work. Skip leading
    setup segments and show the first that does something.
    """
    first = command.strip().splitlines()[0] if command.strip() else ""
    parts = [p.strip() for p in re.split(r"&&|;|\|", first) if p.strip()]
    for part in parts:
        if not _SETUP.match(part):
            return part
    return parts[-1] if parts else ""


def describe(tool: str, args: dict) -> str:
    """One line of plain English for a tool call, or '' to leave it out."""
    if tool in _QUIET:
        return ""
    args = args if isinstance(args, dict) else {}
    # Whatever the caller already wrote for a person wins: Bash and Agent
    # calls carry a description written to be read in exactly this place.
    if args.get("description"):
        return _short(args["description"])
    name = lambda k: os.path.basename(str(args.get(k) or "")) or "a file"  # noqa: E731
    if tool == "Read":
        return f"Read {name('file_path')}"
    if tool in ("Edit", "MultiEdit"):
        return f"Edit {name('file_path')}"
    if tool == "Write":
        return f"Write {name('file_path')}"
    if tool == "NotebookEdit":
        return f"Edit {name('notebook_path')}"
    if tool == "Grep":
        return _short(f"Search for {args.get('pattern', '')}")
    if tool == "Glob":
        return _short(f"Find {args.get('pattern', '')}")
    if tool == "Bash":
        return _short(f"Run {_command_gist(str(args.get('command') or ''))}", 70)
    if tool == "WebFetch":
        m = re.match(r"https?://([^/]+)", str(args.get("url") or ""))
        return f"Read {m.group(1) if m else 'a web page'}"
    if tool == "WebSearch":
        return _short(f"Search the web for {args.get('query', '')}")
    if tool == "Skill":
        return _short(f"Use the {args.get('skill', '')} skill")
    if tool == "AskUserQuestion":
        return "Ask you a question"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return _short(" ".join(p.replace("_", " ") for p in parts[1:3]))
    return tool


def record(event: dict, now: Optional[float] = None) -> None:
    """Append one hook event to its session's file. Never raises."""
    try:
        path = _path(str(event.get("session_id") or ""))
        if path is None:
            return
        kind = {"UserPromptSubmit": "turn", "Stop": "stop",
                "PreToolUse": "step"}.get(str(event.get("hook_event_name") or ""))
        if not kind:
            return
        row = {"at": round(now or time.time(), 3), "kind": kind}
        if kind == "step":
            text = describe(str(event.get("tool_name") or ""), event.get("tool_input") or {})
            if not text:
                return
            row["text"] = text
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        if path.stat().st_size > MAX_BYTES:
            lines = path.read_text().splitlines()[-KEEP_LINES:]
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(path)
    except Exception:  # noqa: BLE001 — a hook must never break the session
        pass


def _rows(session: str) -> list[dict]:
    path = _path(session)
    if path is None or not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict) and "at" in row:
                out.append(row)
        except ValueError:
            continue
    return out


def turns(session: str) -> list[dict]:
    """``[{start, end, steps}]``, oldest first; ``end`` is None while running."""
    out: list[dict] = []
    for row in _rows(session):
        if row["kind"] == "turn":
            out.append({"start": row["at"], "end": None, "steps": []})
        elif not out:
            continue
        elif row["kind"] == "step" and out[-1]["end"] is None:
            out[-1]["steps"].append({"at": row["at"], "text": row.get("text", "")})
        elif row["kind"] == "stop" and out[-1]["end"] is None:
            out[-1]["end"] = row["at"]
    return out


def attach(session: str, lines: list[dict]) -> Optional[dict]:
    """Put each finished turn's work on the reply it produced, in place.

    A reply's ``at`` is when it began to be spoken, which is after its turn
    stopped; the turn it belongs to is the latest one that started before it.
    Each turn is given to the first reply after its start and nobody else, so
    a notification spoken mid-turn does not take the turn's work.

    Returns what the running turn is doing, if one is running — its start,
    the step in progress and how many steps so far — for the page to show in
    place of the dots.
    """
    ts = turns(session)
    if not ts:
        return None
    used: set[int] = set()
    for line in lines:
        if line.get("who") == "you" or line.get("at") is None:
            continue
        at = float(line["at"])
        pick = None
        for i, t in enumerate(ts):
            if t["start"] <= at and t["end"] is not None and t["end"] <= at + 5:
                pick = i
        if pick is None or pick in used:
            continue
        used.add(pick)
        t = ts[pick]
        if t["steps"]:
            line["work"] = {"seconds": round(t["end"] - t["start"], 1),
                            "count": len(t["steps"]),
                            "steps": [s["text"] for s in t["steps"][-MAX_STEPS:]]}
    last = ts[-1]
    if last["end"] is not None:
        return None
    now = last["steps"][-1] if last["steps"] else None
    return {"since": last["start"], "count": len(last["steps"]),
            "current": now["text"] if now else "", "current_at": now["at"] if now else None,
            # The list so far, newest last, as the terminal shows it.
            "steps": [s["text"] for s in last["steps"][-MAX_STEPS:]],
            "server_time": round(time.time(), 3)}


def hook_main() -> None:
    """The hook: one JSON event on stdin, one line appended. Always exits 0."""
    try:
        event = json.load(sys.stdin)
        if os.environ.get("AGENT_MEDIA_ACTIVITY_DEBUG") or (activity_dir() / ".debug").exists():
            (activity_dir() / ".last-event.json").write_text(json.dumps(event)[:4000])
        record(event)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    hook_main()
