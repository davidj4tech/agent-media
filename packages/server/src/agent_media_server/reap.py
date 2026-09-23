"""The idle-session reaper: close agent sessions nobody has spoken in for hours.

A desk with ten Claude Code sessions open holds ~3 GB of red5's 7.7, and most
of them are threads nobody has touched since yesterday. David's rule (22 Sep
2026): close a session idle for 12 hours; 6 when memory is tight. Closing is
the same `send.close_pane` the app's "End" uses, so the transcript stays and a
reply or a resume brings the thread straight back. A session closed here is
marked **rested** (`rest.py`), not ended, and gets a recap first if it has none
newer than its last message.

**Idle** is time since the last *message* in either direction — the last
user/assistant record in the session's transcript (Claude, Codex, pi; Hermes's
`messages` table), or the last thing of it spoken, whichever is later. Not
pane quietness: a TUI repaints a clock, and a pane that is merely open is not a
conversation anybody is having.

**Never reaped** — each is a reason in the decision log:

  pinned          kept open with `POST /session/pin`
  caller          the pane this reaper runs in, or any agent it runs under
  speech-live     the source of speech that is speaking or paused (and, when
                  the speech state cannot be read, the session it last named)
  working         the pane shows a turn in progress
  approval        the pane is stopped on a permission dialog or a question
  pane-draft      text half-typed on the pane's input line
  server-draft    a non-empty `/draft` for it, written in the last 6 h
  unrecognised    the pane does not look like its agent (not painted, or not
                  an agent at all) — fail closed
  no-last-message no transcript message found, so idle cannot be known
  recent          idle for less than the threshold

Only sessions `sessions.live_sessions` finds are considered, which is agent
processes (claude, codex, pi, hermes) in a pane — never a plain shell.

**Dry run first.** The timer runs `media session-reap` every 15 minutes, and
the default mode is dry run: every decision is logged (one line each, to
`<state_dir>/session-reap.log` and stdout → the journal) and nothing is
closed, no recap is generated and no mark is written. `--apply` or
`MEDIA_REAP_MODE=apply` makes it act.

Config (env; hours and percentages may be fractional):
  MEDIA_REAP_MODE          dry-run (default) | apply
  MEDIA_REAP_IDLE_H        idle hours before closing (default 12)
  MEDIA_REAP_TIGHT_IDLE_H  idle hours when memory is tight (default 6)
  MEDIA_REAP_TIGHT_PCT     tight when MemAvailable < this % of MemTotal (20)
  MEDIA_REAP_TIGHT_MB      … or when MemAvailable < this many MB (1500)
  MEDIA_REAP_DRAFT_H       a server-side draft this recent protects (6)
  MEDIA_REAP_RECAP         0 to skip writing recaps before resting (default on)
  MEDIA_REAP_RECAP_MODEL   chat model (default MEDIA_FOLLOWUP_MODEL, then the
                           summary's); endpoint and key are MEDIA_SUMMARY_*
  MEDIA_REAP_RECAP_TIMEOUT seconds (default 20) — a failure never blocks a close
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import drafts, panes, pins, procmem, recaps, rest, send, sessions

MODES = ("dry-run", "apply")

RECAP_PROMPT = (
    "You read the end of a conversation between a person and a coding "
    "assistant. In one to three short sentences, say where this thread was "
    "left: what was being worked on, what was done last, and what was still "
    "open or next. Plain prose, no markdown, no preamble, no quotes."
)

#: The decision log is kept to about this size: a line per live session every
#: 15 minutes is ~100 KB a day, and the last few days are what anyone reads.
_LOG_MAX = 1024 * 1024


# --- config ---------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return v if v > 0 else default


@dataclass(frozen=True)
class Config:
    idle_h: float
    tight_idle_h: float
    tight_pct: float
    tight_mb: float
    draft_h: float
    mode: str

    @classmethod
    def from_env(cls) -> "Config":
        mode = (os.environ.get("MEDIA_REAP_MODE") or "dry-run").strip().lower()
        return cls(idle_h=_env_float("MEDIA_REAP_IDLE_H", 12.0),
                   tight_idle_h=_env_float("MEDIA_REAP_TIGHT_IDLE_H", 6.0),
                   tight_pct=_env_float("MEDIA_REAP_TIGHT_PCT", 20.0),
                   tight_mb=_env_float("MEDIA_REAP_TIGHT_MB", 1500.0),
                   draft_h=_env_float("MEDIA_REAP_DRAFT_H", 6.0),
                   mode=mode if mode in MODES else "dry-run")


def is_tight(host: dict, cfg: Config) -> bool:
    """Memory is tight when MemAvailable is under `tight_pct`% of MemTotal, or
    under `tight_mb`. Unreadable is not tight: the long threshold is the safe
    one."""
    avail, total = host.get("mem_available_mb"), host.get("mem_total_mb")
    if avail is None:
        return False
    if avail < cfg.tight_mb:
        return True
    return bool(total) and avail * 100.0 / total < cfg.tight_pct


def threshold_h(host: dict, cfg: Config) -> float:
    return cfg.tight_idle_h if is_tight(host, cfg) else cfg.idle_h


# --- when the last message was -------------------------------------------------------


def _epoch(v) -> float | None:
    """A record's timestamp: ISO text, or epoch seconds or milliseconds."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e11 else float(v)
    return recaps._epoch(str(v))


def _lines_backwards(path: Path, chunk: int = 256 * 1024):
    """The file's non-empty lines, last first, read in chunks from the end."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        pos = fh.tell()
        carry = b""
        while pos > 0:
            n = min(chunk, pos)
            pos -= n
            fh.seek(pos)
            parts = (fh.read(n) + carry).split(b"\n")
            carry = parts[0] if pos > 0 else b""
            for ln in reversed(parts[1:] if pos > 0 else parts):
                if ln.strip():
                    yield ln


def _is_message(harness: str, rec: dict) -> bool:
    """Is this transcript record a message in the conversation — something
    the person said or the agent did — rather than bookkeeping (a title, a
    recap, token counts, a hook's progress line)?"""
    from agent_media_core import harnesses as h

    kind = rec.get("type")
    if harness == h.CLAUDE:
        return kind in ("user", "assistant")
    if harness == h.CODEX:
        if kind == "response_item":
            return True
        return kind == "event_msg" and (rec.get("payload") or {}).get("type") in (
            "user_message", "agent_message")
    if harness == h.PI:
        return kind == "message"
    return False


def _hermes_last(session: str) -> float | None:
    from agent_media_core import harnesses as h

    # Not max(): an aggregate answers one (NULL) row from every store, and
    # `_hermes_rows` takes the first store that answers anything.
    rows = h._hermes_rows(
        "select timestamp from messages where session_id = ? "
        "and role in ('user', 'assistant') order by timestamp desc limit 1", (session,))
    return _epoch(rows[0][0]) if rows and rows[0][0] is not None else None


def last_message_at(session: str) -> float | None:
    """When the last message in `session`'s transcript was written, or None."""
    from agent_media_core import harnesses as h

    if h.is_hermes(session):
        return _hermes_last(session)
    if h.is_opencode(session):
        rows = h.opencode_rows(
            "select max(time_created) from message where session_id = ?", (session,))
        return rows[0][0] / 1000.0 if rows and rows[0][0] is not None else None
    found = h.transcript(session)
    if not found:
        return None
    harness, path = found
    try:
        for raw in _lines_backwards(path):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and _is_message(harness, rec):
                at = _epoch(rec.get("timestamp"))
                if at is not None:
                    return at
    except OSError:
        return None
    return None


def _text_of(harness: str, rec: dict) -> tuple[str, str]:
    """`(who, words)` of a message record — "" words for a tool call, a tool
    result or anything else that is not prose."""
    from agent_media_core import harnesses as h

    if harness == h.CODEX:
        p = rec.get("payload") or {}
        if rec.get("type") != "response_item" or p.get("type") != "message":
            return "", ""
        role, content = p.get("role"), p.get("content")
    else:
        m = rec.get("message") or {}
        role, content = m.get("role") or rec.get("type"), m.get("content")
        if harness == h.CLAUDE and rec.get("isMeta"):
            return "", ""
    if role not in ("user", "assistant"):
        return "", ""
    if isinstance(content, list):
        content = " ".join(str(b.get("text") or "") for b in content
                           if isinstance(b, dict)
                           and b.get("type") in ("text", "input_text", "output_text"))
    words = " ".join(str(content or "").split())
    if harness == h.CODEX and role == "user" and h._is_preamble(words):
        return "", ""
    return ("Person" if role == "user" else "Assistant"), words


def tail_text(session: str, limit: int = 6000) -> str:
    """The end of the conversation as `Person: …` / `Assistant: …` lines,
    oldest first, at most `limit` characters — what a recap is written from."""
    from agent_media_core import harnesses as h

    picked: list[str] = []
    size = 0
    if h.is_hermes(session):
        rows = h._hermes_rows(
            "select role, content from messages where session_id = ? "
            "and role in ('user', 'assistant') order by id desc limit 40", (session,))
        pairs = [("Person" if r == "user" else "Assistant", " ".join(str(c or "").split()))
                 for r, c in rows]
    else:
        found = h.transcript(session)
        if not found:
            return ""
        harness, path = found
        pairs = []

        def gen():
            for raw in _lines_backwards(path):
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield _text_of(harness, rec)
        try:
            for who, words in gen():
                if words:
                    pairs.append((who, words))
                    size += len(words)
                    if size > limit:
                        break
        except OSError:
            return ""
    size = 0
    for who, words in pairs:
        if not words:
            continue
        line = f"{who}: {words[:1500]}"
        if size + len(line) > limit and picked:
            break
        picked.append(line)
        size += len(line)
    return "\n".join(reversed(picked))


# --- the guards ------------------------------------------------------------------------


def speech_now() -> tuple[bool | None, str]:
    """`(live, session)`: whether speech is speaking or paused, and whose.

    Read the way the canvas reads it, without the canvas: the speech
    channel's now_playing names its source session, and the speech display
    state says whether anything is live. `live` is None when that could not
    be read — the caller then keeps the named session, failing closed.
    """
    session = ""
    try:
        from agent_media_core.state.store import StateStore

        np = StateStore().get_now_playing("speech") or {}
        session = str((np.get("extras") or {}).get("source_session") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        from agent_media_core import cli

        idle = cli._speech_display_state()[0]
        return (not idle), session
    except Exception:  # noqa: BLE001
        return None, session


def caller_sessions(live: dict[str, str], pids: dict[str, int]) -> set[str]:
    """The sessions this process is running inside: the one in its own pane
    (TMUX_PANE / HERDR_PANE_ID), and any agent that is one of its ancestors.
    An agent that asks for a reap is never reaped by it."""
    env = {k.encode(): v.encode() for k, v in os.environ.items()}
    me = panes.addr_of_env(env)
    out = {sid for sid, pane in live.items() if me and pane == me}
    ancestors: set[int] = set()
    pid: int | None = os.getpid()
    for _ in range(64):
        if not pid or pid in ancestors:
            break
        ancestors.add(pid)
        pid = procmem._ppid(str(pid))
    out |= {sid for sid, p in pids.items() if p in ancestors}
    return out


def server_draft_fresh(session: str, hours: float, now: float) -> bool:
    """A non-empty `/draft` for this session, written in the last `hours`.
    Written-at is the file's own mtime: the `at` inside is the client's clock."""
    path = drafts._drafts_dir() / f"{session}.json"
    try:
        data = json.loads(path.read_text())
        written = path.stat().st_mtime
    except (OSError, ValueError):
        return False
    return bool(str(data.get("text") or "").strip()) and now - written < hours * 3600


def pane_guards(session: str, pane: str) -> list[str]:
    """What the session's activity says against closing it — asked of
    `sessions.activity_of`, the one place that answers "is it busy"."""
    act = sessions.activity_of(session, pane, with_draft=True)
    out = []
    if act["state"] is None:
        out.append("unrecognised")
    elif act["state"] in ("working", "approval"):
        out.append(act["state"])
    if act.get("draft"):
        out.append("pane-draft")
    return out


# --- deciding -------------------------------------------------------------------------


@dataclass
class Decision:
    session: str
    title: str
    pane: str
    idle_h: float | None
    last_at: float | None
    mem_mb: int | None
    reasons: list[str] = field(default_factory=list)
    action: str = "keep"          # "close" | "keep"
    rest_reason: str = ""         # "idle" | "idle-tight", for a close
    result: str = ""              # would-close | closed | gone | failed: … | kept
    recap: str = ""               # have | would-write | written | failed | off


def _title(sid: str, pane: str, titles: dict[str, str]) -> str:
    t = titles.get(pane) or ""
    if not t:
        try:
            t = sessions._live_title(sid)
        except Exception:  # noqa: BLE001
            t = ""
    return t


def decide(cfg: Config, now: float | None = None) -> tuple[list[Decision], dict]:
    """A decision for every live agent session, and the host's memory. Reads only."""
    now = time.time() if now is None else now
    live = sessions.live_sessions()
    pids = dict(sessions._PIDS)
    host = procmem.host_mem()
    tight = is_tight(host, cfg)
    limit = cfg.tight_idle_h if tight else cfg.idle_h
    mem = procmem.tree_mem_mb({sid: pids.get(sid) for sid in live})
    try:
        titles = sessions._pane_titles()
    except Exception:  # noqa: BLE001
        titles = {}
    pinned = pins.pinned()
    callers = caller_sessions(live, pids)
    speaking, speech_sid = speech_now()
    try:
        from agent_media_core.state.store import StateStore

        spoken = StateStore().last_spoken()
    except Exception:  # noqa: BLE001
        spoken = {}
    out: list[Decision] = []
    for sid, pane in sorted(live.items()):
        last = last_message_at(sid)
        heard = spoken.get(sid)
        if heard is not None and (last is None or heard > last):
            last = heard
        idle = round((now - last) / 3600.0, 2) if last is not None else None
        d = Decision(session=sid, title=_title(sid, pane, titles), pane=pane,
                     idle_h=idle, last_at=last, mem_mb=mem.get(sid))
        if sid in pinned:
            d.reasons.append("pinned")
        if sid in callers:
            d.reasons.append("caller")
        if speech_sid == sid and speaking is not False:
            d.reasons.append("speech-live")
        d.reasons += pane_guards(sid, pane)
        if server_draft_fresh(sid, cfg.draft_h, now):
            d.reasons.append("server-draft")
        if idle is None:
            d.reasons.append("no-last-message")
        elif idle < limit:
            d.reasons.append("recent")
        if not d.reasons:
            d.action = "close"
            d.rest_reason = "idle" if idle >= cfg.idle_h else "idle-tight"
        out.append(d)
    return out, {**host, "tight": tight, "threshold_h": limit}


# --- acting --------------------------------------------------------------------------


def _clean(text: str) -> str:
    text = " ".join((text or "").split()).strip().strip('"').strip()
    for label in ("Recap:", "Summary:"):
        if text.lower().startswith(label.lower()):
            text = text[len(label):].strip()
    return text[:600]


def generate_recap(session: str) -> dict | None:
    """Write a "where this thread was" recap through the gateway and keep it
    (`rest.save_recap`). None on any failure — never raises."""
    try:
        from agent_media_core.intake._summary import _chat, _int_env

        text = tail_text(session)
        if not text:
            return None
        model = (os.environ.get("MEDIA_REAP_RECAP_MODEL")
                 or os.environ.get("MEDIA_FOLLOWUP_MODEL") or None)
        out = _clean(_chat(RECAP_PROMPT, text, _int_env("MEDIA_REAP_RECAP_TIMEOUT", 20),
                           model=model) or "")
        if not out:
            return None
        return rest.save_recap(session, out)
    except Exception:  # noqa: BLE001 — a recap must never stand in front of a close
        return None


def _recap_state(d: Decision) -> str:
    have = recaps.recap_for(d.session)
    return "have" if have and d.last_at is not None and have["at"] >= d.last_at else "missing"


def _recaps_on() -> bool:
    return (os.environ.get("MEDIA_REAP_RECAP", "1") or "1").strip() != "0"


def act(decisions: list[Decision], cfg: Config, apply: bool) -> None:
    """Close what `decide` said to close (apply), or say it would (dry run)."""
    for d in decisions:
        if d.action != "close":
            d.result = "kept"
            continue
        state = _recap_state(d)
        if not apply:
            d.result = "would-close"
            d.recap = "have" if state == "have" else (
                "would-write" if _recaps_on() else "off")
            continue
        if state == "have":
            d.recap = "have"
        elif not _recaps_on():
            d.recap = "off"
        else:
            d.recap = "written" if generate_recap(d.session) else "failed"
        # The recap call can take seconds: look at the pane again before
        # closing it, in case somebody started typing or a turn began.
        late = pane_guards(d.session, d.pane)
        if late:
            d.action, d.result = "keep", "kept"
            d.reasons += late
            continue
        ok, detail = send.close_pane(d.session, d.pane)
        if ok and detail.get("closed"):
            rest.mark_rested(d.session, d.idle_h or 0.0, d.rest_reason)
            d.result = "closed"
        elif ok:
            d.result = "gone"
        else:
            d.result = f"failed: {detail.get('error') or 'could not close'}"
    if apply:
        # A rested session that is live again (resumed at the desk, say) is
        # not resting: drop the stale mark.
        live_now = {d.session for d in decisions if d.result != "closed"}
        for sid in list(rest.rested()):
            if sid in live_now:
                rest.clear_quietly(sid)


# --- reporting ------------------------------------------------------------------------


def log_path() -> Path:
    from agent_media_core._paths import state_dir

    return state_dir() / "session-reap.log"


def line(d: Decision, host: dict, mode: str, at: float) -> str:
    stamp = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
    idle = f"{d.idle_h:.1f}h" if d.idle_h is not None else "?"
    mem = f"{d.mem_mb}MB" if d.mem_mb is not None else "?"
    avail = host.get("mem_available_mb")
    total = host.get("mem_total_mb")
    title = (d.title or "").replace('"', "'")[:60]
    verdict = d.result or ("would-close" if d.action == "close" else "kept")
    out = (f"{stamp} {mode} {verdict} {d.session[:8]} \"{title}\" "
           f"idle={idle}/{host.get('threshold_h'):g}h mem={mem} "
           f"host={avail if avail is not None else '?'}/"
           f"{total if total is not None else '?'}MB{' tight' if host.get('tight') else ''}")
    if d.action == "close" or d.result in ("closed", "gone") or d.result.startswith("failed"):
        out += f" why={d.rest_reason} recap={d.recap}"
    else:
        out += f" reason={','.join(d.reasons)}"
    return out


def _append_log(lines: list[str]) -> None:
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _LOG_MAX:
            keep = path.read_bytes()[-_LOG_MAX // 2:]
            path.write_bytes(keep[keep.find(b"\n") + 1:])
        with open(path, "a") as fh:
            fh.write("".join(ln + "\n" for ln in lines))
    except OSError as e:
        print(f"session-reap: could not write {path} ({e})", file=sys.stderr)


def run(mode: str | None = None, *, as_json: bool = False, write_log: bool = True,
        now: float | None = None, out=None) -> int:
    """One reaper pass. `mode` overrides MEDIA_REAP_MODE. Prints the decisions
    (lines, or one JSON object) and appends the lines to the log."""
    out = out or sys.stdout
    cfg = Config.from_env()
    mode = mode if mode in MODES else cfg.mode
    now = time.time() if now is None else now
    decisions, host = decide(cfg, now)
    act(decisions, cfg, apply=(mode == "apply"))
    lines = [line(d, host, mode, now) for d in decisions]
    if not decisions:
        lines = [f"{datetime.fromtimestamp(now).astimezone().isoformat(timespec='seconds')} "
                 f"{mode} no live agent sessions"]
    if write_log:
        _append_log(lines)
    if as_json:
        print(json.dumps({"mode": mode, "at": round(now, 3), "host": host,
                          "config": asdict(cfg),
                          "decisions": [asdict(d) for d in decisions]}, indent=1), file=out)
    else:
        for ln in lines:
            print(ln, file=out)
    return 0 if not any(d.result.startswith("failed") for d in decisions) else 1

