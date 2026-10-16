"""How full a Claude Code session's context is, told to the session itself.

Two entry points share one state file per session:

* ``media-statusline`` — the status line. Claude Code hands it its own count
  of the context: the window of the session's model (``context_window_size``)
  and the share of it used (``used_percentage``). It prints a short line and
  saves the numbers under ``<state>/<session_id>.window``.

* ``media-hook-context`` — a UserPromptSubmit hook. It takes the last
  assistant turn's usage from the transcript and, when that crosses a band
  (50, 70, 85%), adds a note to the session's context so it can work leaner
  and, near the limit, write a handoff note before auto-compaction runs (a
  session can't run /compact itself). Each band is reported once per
  session; a drop below 50% — after a compaction — resets it.

The window is the one the status line saved. A headless session runs no
status line, so for it the window is guessed: ``WINDOWS`` by model id, and a
session already past 200k is taken to have 1M, since it could not have got
there otherwise. ``CLAUDE_CONTEXT_WINDOW`` (tokens) overrides both.

Neither prints anything on anything unexpected, and both exit 0: a prompt
must never be blocked, nor a status line broken, by this.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BANDS = (
    (85, "Context is {pct}% full ({used}k of {win}k); auto-compaction is near and "
         "will run by itself, so don't ask the user to /compact. Before it does, "
         "write a short handoff note: the task in hand, decisions made, what's "
         "done and not yet committed or deployed, and the next steps. Put it in "
         "the project's own plan or notes file if it has one (PLAN.md, a "
         "progress log), otherwise in the session scratchpad, and name the file "
         "in your reply in one line."),
    (70, "Context is {pct}% full ({used}k of {win}k). No need to tell the user: "
         "keep reads targeted, avoid large file dumps, and delegate wide "
         "searches to subagents. Keep the project's plan or notes file current "
         "as decisions are made, so a compaction loses nothing."),
    (50, "Context is {pct}% full ({used}k of {win}k). Prefer targeted reads and "
         "subagents for broad exploration; no need to mention it to the user."),
)

#: Model id prefix -> context window, for a session with no status line.
#: Opus 5.5 sessions run past 500k without auto-compacting (Oct 2026).
WINDOWS = (
    ("claude-opus-5-5", 1_000_000),
)


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    d = base / "agent-media" / "context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def window(model: str, used: int, reported: Path) -> int:
    env = os.environ.get("CLAUDE_CONTEXT_WINDOW")
    if env:
        return int(env)
    try:
        return int(json.loads(reported.read_text())["size"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if model.endswith("[1m]") or used > 200_000:
        return 1_000_000
    for prefix, win in WINDOWS:
        if model.startswith(prefix):
            return win
    return 200_000


def last_usage(transcript: str) -> tuple[str, dict | None]:
    with open(transcript, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 2_000_000))
        lines = f.read().splitlines()
    for raw in reversed(lines):
        if b'"usage"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        m = d.get("message") or {}
        u = m.get("usage")
        if d.get("type") == "assistant" and u and not d.get("isSidechain"):
            return m.get("model", ""), u
    return "", None


def _hook() -> None:
    hook = json.load(sys.stdin)
    transcript = hook.get("transcript_path")
    sid = hook.get("session_id") or "x"
    if not transcript or not os.path.exists(transcript):
        return
    model, u = last_usage(transcript)
    if not u:
        return
    used = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
            + u.get("cache_creation_input_tokens", 0))
    d = state_dir()
    win = window(model, used, d / f"{sid}.window")
    pct = round(100 * used / win)

    state = d / sid
    reported = int(state.read_text() or 0) if state.exists() else 0
    if pct < 50:
        if reported:
            state.write_text("0")
        return
    for band, msg in BANDS:
        if pct >= band:
            if band > reported:
                state.write_text(str(band))
                print(msg.format(pct=pct, used=used // 1000, win=win // 1000))
            return


def _statusline() -> None:
    d = json.load(sys.stdin)
    cw = d.get("context_window") or {}
    size = cw.get("context_window_size")
    pct = cw.get("used_percentage")
    sid = d.get("session_id")
    if size and sid:
        (state_dir() / f"{sid}.window").write_text(
            json.dumps({"size": size, "used_percentage": pct}))
    model = (d.get("model") or {}).get("display_name") or ""
    if size:
        win = f"{size // 1_000_000}M" if size >= 1_000_000 else f"{size // 1000}k"
        used = f"{pct:.0f}%" if pct is not None else "–"
        print(f"{model}  ctx {used} of {win}".strip())
    else:
        print(model)


def main() -> None:
    try:
        _hook()
    except Exception:
        pass


def main_statusline() -> None:
    try:
        _statusline()
    except Exception:
        pass
