"""How full an agent session's context is, told to the session itself.

Two entry points share one state file per session:

* ``media-statusline`` — the status line. Claude Code hands it its own count
  of the context: the window of the session's model (``context_window_size``)
  and the share of it used (``used_percentage``). It prints a short line and
  saves the numbers under ``<state>/<session_id>.window``.

* ``media-hook-context`` — Claude Code's UserPromptSubmit hook and Gemini
  CLI's BeforeAgent hook. Codex's is ``media-hook-codex event``, which calls
  ``note_for`` (a new hooks.json entry would stop Codex for a review), and
  pi's extension calls this with its own numbers. It takes the last turn's
  usage from the transcript and, when that crosses a band
  (50, 70, 85%), adds a note to the session's context so it can work leaner
  and, near the limit, write a handoff note before auto-compaction runs (a
  session can't run /compact itself). Each band is reported once per
  session; a drop below 50% — after a compaction — resets it.

The window is the one the status line saved. A headless session runs no
status line, so for it the window is guessed: ``WINDOWS`` by model id, and a
session already past 200k is taken to have 1M, since it could not have got
there otherwise. ``CLAUDE_CONTEXT_WINDOW`` (tokens) overrides both. Codex
writes its window into its transcript; Gemini's is counted to the point
where it compresses its history (``gemini_compression_threshold``).

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


#: Gemini's windows (core/tokenLimits.js, gemini-cli 0.64): 1M for every
#: Gemini model and anything unknown; Gemma 4 has 256k.
GEMINI_WINDOWS = (("gemma-4", 256_000),)
GEMINI_WINDOW = 1_048_576


def gemini_compression_threshold() -> float:
    """The share of the window at which Gemini CLI compresses its history
    (``model.compressionThreshold``, default 0.5). Its warnings are counted
    against that point, since that is where history is lost."""
    try:
        cfg = json.loads((Path.home() / ".gemini" / "settings.json").read_text())
        return float(cfg["model"]["compressionThreshold"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.5


def _tail(transcript: str) -> list[bytes]:
    with open(transcript, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 2_000_000))
        return f.read().splitlines()


def last_usage(transcript: str) -> tuple[str, int, int | None] | None:
    """(model, tokens in the context, window if the transcript says) from
    the newest turn that carries usage — in whichever of the three
    transcript shapes this is:

    * Claude Code: an ``assistant`` record's ``message.usage``.
    * Codex: an ``event_msg`` ``token_count`` — ``info.last_token_usage``
      (``total_tokens``: the prompt and the reply that is now part of it)
      and its own ``model_context_window``.
    * Gemini CLI: a ``gemini`` record's ``tokens.input``.
    """
    for raw in reversed(_tail(transcript)):
        if not (b'"usage"' in raw or b'"token_count"' in raw or b'"tokens"' in raw):
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        kind = d.get("type")
        if kind == "assistant":
            m = d.get("message") or {}
            u = m.get("usage")
            if u and not d.get("isSidechain"):
                used = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                        + u.get("cache_creation_input_tokens", 0))
                return m.get("model", ""), used, None
        elif kind == "event_msg":
            p = d.get("payload") or {}
            info = p.get("info") or {}
            last = info.get("last_token_usage")
            if p.get("type") == "token_count" and last:
                return "", int(last.get("total_tokens", 0)), info.get("model_context_window")
        elif kind == "gemini":
            t = d.get("tokens") or {}
            if t.get("input"):
                model = d.get("model") or ""
                win = next((w for pre, w in GEMINI_WINDOWS if model.startswith(pre)),
                           GEMINI_WINDOW)
                return model, int(t["input"]), int(win * gemini_compression_threshold())
    return None


def measure(hook: dict, d: Path) -> tuple[int, int] | None:
    """(tokens in the context, window), from whatever the harness gave.

    A harness that knows both says so (pi's extension: ``context_tokens``
    and ``context_window``); otherwise they come from the transcript."""
    if hook.get("context_tokens") and hook.get("context_window"):
        return int(hook["context_tokens"]), int(hook["context_window"])
    transcript = hook.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return None
    got = last_usage(transcript)
    if not got:
        return None
    model, used, win = got
    if win:
        return used, int(win)
    sid = hook.get("session_id") or "x"
    return used, window(model, used, d / f"{sid}.window")


def note_for(hook: dict) -> str:
    """What to tell the session now, as the harness wants it printed, or "".

    A harness hook (``hook_event_name``: Claude Code's and Codex's
    UserPromptSubmit, Gemini CLI's BeforeAgent) gets the JSON all three read
    as context for the model — Gemini shows plain stdout to the person
    instead, and Codex is not known to read it at all. pi's extension asks
    without an event name, and takes the text as it is."""
    sid = hook.get("session_id") or "x"
    d = state_dir()
    got = measure(hook, d)
    if not got:
        return ""
    used, win = got
    pct = round(100 * used / win)

    state = d / sid
    reported = int(state.read_text() or 0) if state.exists() else 0
    if pct < 50:
        if reported:
            state.write_text("0")
        return ""
    for band, msg in BANDS:
        if pct >= band:
            if band <= reported:
                return ""
            state.write_text(str(band))
            note = msg.format(pct=pct, used=used // 1000, win=win // 1000)
            event = hook.get("hook_event_name")
            if event:
                note = json.dumps({"hookSpecificOutput": {
                    "hookEventName": event, "additionalContext": note}})
            return note
    return ""


def _hook() -> None:
    note = note_for(json.load(sys.stdin))
    if note:
        print(note)


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
