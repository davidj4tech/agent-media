"""How full an agent session's context is, told to the session itself.

Two entry points share one state file per session:

* ``media-statusline`` — the status line. Claude Code hands it its own count
  of the context: the window of the session's model (``context_window_size``)
  and the share of it used (``used_percentage``). It prints a short line and
  saves the numbers under ``<state>/<session_id>.window``.

* ``media-hook-context`` — Claude Code's UserPromptSubmit hook. Codex's is
  ``media-hook-codex event``, which calls ``note_for`` (a new hooks.json
  entry would stop Codex for a review); Gemini CLI's is its BeforeAgent
  hook, ``media-hook-gemini``, which does the same; Grok's is its
  PostToolUse hook, ``media-hook-grok`` (Grok reads context from no other
  event), with numbers it reads itself (``grok_usage``); and pi's extension
  calls this with its own numbers. It takes the last turn's
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
where it compresses its history (``gemini_compression_threshold``), and
Grok's to where it compacts (``grok_compaction_point``).

It also carries the **restart note** (``_restart_note``): while a drained
restart of sessiond is pending (restart_services.py), a headless session it
waits on is told, once, to end its turn. A turn under way sees no
UserPromptSubmit — and new prompts are held while a restart is pending — so
the note rides on Claude Code's **PostToolUse** too: ``media-hook-context
--restart``, installed by the same setup row behind a shell test that costs
nothing until a restart is pending in a sessiond session (``RESTART_HOOK``).
PostToolUse is the one hook that runs inside a turn and adds context to it
(its ``additionalContext`` follows the tool's result). A subagent's tool
calls fire it too (``agent_id`` in the input): those are not told, since a
subagent told to end its turn would stop its parent's work half done.

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
    home = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())
    try:
        cfg = json.loads((home / ".gemini" / "settings.json").read_text())
        return float(cfg["model"]["compressionThreshold"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.5


#: Grok's window and where it compacts, when its model list does not say
#: (grok-4.7 in 1.0.62's models_cache.json: 256k, 80%).
GROK_WINDOW = 256_000
GROK_COMPACT_PERCENT = 80


def grok_compaction_point(model: str = "", window: int = 0) -> int:
    """The tokens at which Grok compacts a conversation: a share
    (`auto_compact_threshold_percent`) of the model's `context_window`. Its
    warnings are counted against that point, since that is where history is
    lost.

    Both come from Grok's own model list (`models_cache.json`, `models.<id>.
    info`) unless its config.toml sets them — `[model.<id>]`, then
    `[session]` for the share. `window` is one the session already
    reported."""
    import tomllib

    from .. import harnesses

    base = harnesses.grok_dir()
    try:
        models = json.loads((base / "models_cache.json").read_text()).get("models") or {}
    except (OSError, ValueError, AttributeError):
        models = {}
    if not isinstance(models, dict):
        models = {}
    entry = models.get(model) or (next(iter(models.values())) if len(models) == 1 else {})
    info = (entry.get("info") if isinstance(entry, dict) else None) or {}
    try:
        cfg = tomllib.loads((base / "config.toml").read_text())
    except (OSError, ValueError):
        cfg = {}
    mine = ((cfg.get("model") or {}).get(model) or {}) if model else {}
    pct = (mine.get("auto_compact_threshold_percent")
           or (cfg.get("session") or {}).get("auto_compact_threshold_percent")
           or info.get("auto_compact_threshold_percent") or GROK_COMPACT_PERCENT)
    win = window or mine.get("context_window") or info.get("context_window") or GROK_WINDOW
    try:
        return int(int(win) * float(pct) / 100)
    except (TypeError, ValueError):
        return int(GROK_WINDOW * GROK_COMPACT_PERCENT / 100)


def grok_usage(transcript: str) -> tuple[int, int] | None:
    """`(tokens in the context, the point Grok compacts at)` for the Grok
    session whose `updates.jsonl` is `transcript`, or None.

    Not confirmed on a real session (none on disk yet): the count is read
    from the newest ACP `usage_update` in the log (`used`, of `size`), else
    from a `usage` with Anthropic-shaped input counts (its headless output's
    shape), else from the folder's `signals.json` (`context_tokens`, the
    name its status line gives the same number). The model is the one
    `summary.json` says."""
    from .. import harnesses

    folder = Path(transcript).parent
    model = str(harnesses.grok_summary(folder).get("current_model_id") or "")
    used = size = 0
    try:
        lines = _tail(transcript, 400_000)
    except OSError:
        lines = []
    for raw in reversed(lines):
        if b'"used"' not in raw and b'"usage"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        u = harnesses._grok_update(rec) or {}
        if u.get("sessionUpdate") == "usage_update" and isinstance(u.get("used"), int):
            used, size = u["used"], int(u.get("size") or 0)
            break
        got = u.get("usage") if isinstance(u.get("usage"), dict) else None
        if got and got.get("input_tokens") is not None:
            used = sum(int(got.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                        "cache_creation_input_tokens"))
            break
    if not used:
        try:
            sig = json.loads((folder / "signals.json").read_text())
        except (OSError, ValueError):
            sig = {}
        for d in (sig, *(v for v in (sig.values() if isinstance(sig, dict) else ())
                         if isinstance(v, dict))):
            got = d.get("context_tokens", d.get("contextTokens")) if isinstance(d, dict) else None
            if isinstance(got, int) and got > 0:
                used = got
                break
    if not used:
        return None
    return used, grok_compaction_point(model, size)


def _tail(transcript: str, most: int = 2_000_000) -> list[bytes]:
    with open(transcript, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - most))
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
            if p.get("type") == "token_count":
                rl = p.get("rate_limits")
                if isinstance(rl, dict):
                    try:
                        from .. import quota
                        for win_key in ("primary", "secondary"):
                            win = rl.get(win_key)
                            if isinstance(win, dict) and win.get("used_percent") is not None:
                                quota.check_rate_limit_info(
                                    {"used_percent": win.get("used_percent"),
                                     "resetsAt": win.get("resets_at"),
                                     "window": win_key},
                                    agent="codex",
                                )
                    except Exception:
                        pass
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
    UserPromptSubmit, Gemini CLI's BeforeAgent, Grok's PostToolUse) gets the
    JSON all four read as context for the model — Gemini shows plain stdout
    to the person instead, and Codex is not known to read it at all. pi's extension asks
    without an event name, and takes the text as it is."""
    from ._env import load_env_file

    load_env_file("hook")  # the switches below may be set in the env files
    d = state_dir()
    note = " ".join(n for n in (_restart_note(hook, d), _band_note(hook, d),
                                _notes_note(hook, d), _link_note(hook, d)) if n)
    event = hook.get("hook_event_name")
    if note and event:
        note = json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": note}})
    return note


def _band_note(hook: dict, d: Path) -> str:
    """The context band just crossed, once per band, or ""."""
    sid = hook.get("session_id") or "x"
    got = measure(hook, d)
    if not got:
        return ""
    used, win = got
    pct = round(100 * used / win)

    state = d / sid
    reported = int(state.read_text() or 0) if state.exists() else 0
    if pct < 50:
        if reported:
            # A compaction: what the session was told is gone with it.
            state.write_text("0")
            (d / f"{sid}.link").unlink(missing_ok=True)
            (d / f"{sid}.notes").unlink(missing_ok=True)
        return ""
    for band, msg in BANDS:
        if pct >= band:
            if band <= reported:
                return ""
            state.write_text(str(band))
            return msg.format(pct=pct, used=used // 1000, win=win // 1000)
    return ""


#: The session's own thread link (sasonica-app lib/threadLinks.ts; David,
#: 18 Oct 2026): `<MEDIA_PUBLIC_URL>/app/t/<session>` opens it in the
#: Sasonica app on a phone (an App Link, the server's assetlinks.json) and in
#: the web app at a desk; with no public address, `sasonica://t/<session>`,
#: app only.
LINK_NOTE = ("This thread's link is {link} — it opens this conversation in the "
             "Sasonica app. When you name a session or thread to the user, link "
             "it the same way ({base}<session-id>), as a markdown link.")


def thread_link_base() -> str:
    """Where a thread's link starts, up to the session id."""
    from ._env import load_env_file

    load_env_file("hook")
    pub = (os.environ.get("MEDIA_PUBLIC_URL") or "").strip().rstrip("/")
    return f"{pub}/app/t/" if pub.startswith("https://") else "sasonica://t/"


#: The shipped notes (agent_media_core/agent_notes.md): how to write for a
#: reader who mostly listens, on a phone — from what David's own sessions
#: learnt (docs/proposals/2026-10-18-memories-to-docs.md, "D").
NOTES_FILE = Path(__file__).resolve().parent.parent / "agent_notes.md"


def agent_notes(hook: dict) -> list[str]:
    """The notes' rules that apply to this session: `all`, `claude` when it
    is a Claude Code session (its transcript is under ~/.claude), `canvas`
    when the canvas draws figures."""
    from ._env import load_env_file
    from ._visual import visual_enabled

    load_env_file("hook")
    path = Path(os.environ.get("MEDIA_AGENT_NOTES_FILE") or NOTES_FILE).expanduser()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    claude = "/.claude/" in str(hook.get("transcript_path") or "")
    want = {"all"} | ({"claude"} if claude else set()) | ({"canvas"} if visual_enabled() else set())
    out = []
    for line in lines:
        if not line.startswith("- ["):
            continue
        tag, _, rule = line[3:].partition("] ")
        if tag in want and rule.strip():
            out.append(rule.strip())
    return out


def _notes_note(hook: dict, d: Path) -> str:
    """The shipped notes, told once per session (again after a compaction).
    MEDIA_AGENT_NOTES=0 keeps them out."""
    sid = hook.get("session_id")
    if not sid or (os.environ.get("MEDIA_AGENT_NOTES") or "").strip() in ("0", "-", "off"):
        return ""
    marker = d / f"{sid}.notes"
    if marker.exists():
        return ""
    marker.write_text("1")
    rules = agent_notes(hook)
    return ("How to write for this user (agent-media): " + " ".join(rules)) if rules else ""


def _link_note(hook: dict, d: Path) -> str:
    """The thread's link, told once per session (again after a compaction),
    on whichever hook a harness reads context from. MEDIA_THREAD_LINK=0
    keeps it out."""
    sid = hook.get("session_id")
    if not sid:
        return ""
    if (os.environ.get("MEDIA_THREAD_LINK") or "").strip() in ("0", "-", "off"):
        return ""
    marker = d / f"{sid}.link"
    if marker.exists():
        return ""
    marker.write_text("1")
    base = thread_link_base()
    return LINK_NOTE.format(link=base + sid, base=base)


#: What a session a drained restart waits on is told (restart_services.py).
RESTART_NOTE = ("A restart of the session host is waiting. Finish the step you're on "
                "and end your turn; don't start new background agents or background "
                "commands until it's done. You'll be resumed automatically.")


def _restart_note(hook: dict, d: Path) -> str:
    """`RESTART_NOTE`, once per pending restart, to a sessiond session it is
    waiting on (not for a question card only: that waits on David, not on
    the agent), and never to a subagent."""
    sid = (os.environ.get("MEDIA_SESSIOND_SESSION") or "").strip()
    if not sid or hook.get("agent_id"):
        return ""
    try:
        from .. import restart_services as rs

        rec = rs.pending()
    except Exception:  # noqa: BLE001 — a note is never worth failing a hook
        return ""
    if not rec or not any(isinstance(w, dict) and w.get("session") == sid
                          and w.get("kind") != "question"
                          for w in rec.get("waiting_on") or []):
        return ""
    marker = d / f"{sid}.restart"
    try:
        if marker.read_text().strip() == str(rec["id"]):
            return ""
    except OSError:
        pass
    marker.write_text(str(rec["id"]))
    return RESTART_NOTE


def told_to_end(sid: str, restart_id: str) -> bool:
    """Whether sessiond session `sid` has been told (`RESTART_NOTE`) to end
    its turn for the pending restart `restart_id`: sessiond then resumes it
    after, since its work was cut short (sessiond.py `_drain_cut`)."""
    if not sid or not restart_id:
        return False
    try:
        return (state_dir() / f"{sid}.restart").read_text().strip() == restart_id
    except OSError:
        return False


def restart_note_for(hook: dict) -> str:
    """`media-hook-context --restart` (Claude Code's PostToolUse): the
    restart note alone, as the hook JSON."""
    note = _restart_note(hook, state_dir())
    if not note:
        return ""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": hook.get("hook_event_name") or "PostToolUse",
        "additionalContext": note}})


def _hook() -> None:
    hook = json.load(sys.stdin)
    note = restart_note_for(hook) if "--restart" in sys.argv[1:] else note_for(hook)
    if note:
        print(note)


def _statusline() -> None:
    d = json.load(sys.stdin)
    rl = d.get("rate_limits") or d.get("rate_limit_info")
    if isinstance(rl, dict):
        try:
            from .. import quota
            quota.check_rate_limit_info(rl, agent="claude", session=str(d.get("session_id") or ""))
        except Exception:
            pass
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
