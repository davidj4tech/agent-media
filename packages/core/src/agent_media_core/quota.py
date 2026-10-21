"""The account's usage limit: who it stopped, and when to pick them up again.

Every Claude session on a host shares one subscription, so when the usage
limit is reached (Claude Code's "You've hit your session limit · resets 2am
(Australia/Melbourne)", a `result` with `api_error: "usage_limit_reached"`
and HTTP 429) every turn after it fails the same way until the window
resets. On 20 Oct 2026 that stopped several of David's sessions mid-turn and
nothing picked them up. This module is the one record of it, shared by
whoever notices: sessiond (its headless children, sessiond.py), and the
Claude Code `StopFailure` hook of a desk session in a tmux pane
(intake/hook_claude_code.py). sessiond's release loop is the one that acts
on it: at the reset (plus `MEDIA_QUOTA_RESUME_MARGIN_S`, default 60 s) it
resumes the sessions waiting, oldest cut off first, one every
`MEDIA_QUOTA_RESUME_GAP_S` (default 20 s); one that hits the limit again
puts the reset forward and the rest keep waiting.

`<state>/quota.json`, under `quota.lock`:

    {"since": <epoch s>, "resets_at": <epoch s>, "source": "event" | "text" |
     "backoff", "kind": "five_hour" | "seven_day" | …, "text": "<the CLI's
     sentence>", "attempt": <backoffs so far>, "state": "limited" |
     "resuming", "next_at": <epoch s: the next resume, staggered>,
     "waiting": [{"kind": "headless" | "desk", "session", "cwd", "at", …}]}

A headless entry is only the order: what was cut off is the session's own
record (`cut_off`, why "quota"). A desk entry carries it: `pane`,
`last_text`, `tasks` (background agents the limit stopped) and `turn`.

When the reset time is not known (no `resetsAt`, a sentence this cannot
read) it is a backoff: 15 min, doubling to 2 h, and the first resume is the
probe. Standard library only: the hook imports it.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

#: Claude Code's sentence for it ("You've hit your session limit · resets
#: 2am (Australia/Melbourne)"; weekly and Opus limits say so in its place).
LIMIT_RE = re.compile(
    r"(?:hit|reached) your (?:[\w-]+ )*limit|usage limit"
    r"|out of extra usage|rate_limit_error|would exceed your (?:account's |organization's )?rate limit"
    r"|resource_exhausted|quota exceeded|exhausted your (?:capacity|quota|daily quota)"
    r"|ratelimitexceeded|rate_limit_exceeded|insufficient_quota|usage_limit_reached"
    r"|model's limit is used up|free usage exceeded|too many requests.*(?:quota|limit)",
    re.I,
)
#: The CLI's error code for it, on the result and on the transcript line.
LIMIT_ERROR = "usage_limit_reached"


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def margin_s() -> float:
    """How long after the reset the first session is resumed: the window's
    clock and ours need not agree to the second."""
    return _env_f("MEDIA_QUOTA_RESUME_MARGIN_S", 60.0)


def gap_s() -> float:
    """Between one resume and the next: long enough for the first to fail
    again if the limit is still on, before the rest follow it."""
    return _env_f("MEDIA_QUOTA_RESUME_GAP_S", 20.0)


def backoff_s(attempt: int) -> float:
    return min(900.0 * (2 ** max(0, attempt)), 7200.0)


def path() -> Path:
    from ._paths import state_dir

    return state_dir() / "quota.json"


# --- recognising it ---------------------------------------------------------------

def is_limit_text(text: object) -> bool:
    return isinstance(text, str) and bool(LIMIT_RE.search(text))


def is_limit_result(obj: dict) -> bool:
    """A stream-json `result` that ended its turn on the usage limit."""
    if not isinstance(obj, dict) or obj.get("type") != "result":
        return False
    if obj.get("api_error") == LIMIT_ERROR:
        return True
    if not obj.get("is_error"):
        return False
    tr = obj.get("terminal_reason")
    if tr and tr != "api_error" and obj.get("api_error_status") != 429:
        return False
    return is_limit_text(obj.get("result"))


def is_limit_line(obj: dict) -> bool:
    """A transcript line that is the limit's synthetic reply."""
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return False
    if obj.get("apiError") == LIMIT_ERROR:
        return True
    if not obj.get("isApiErrorMessage"):
        return False
    content = (obj.get("message") or {}).get("content")
    return any(isinstance(b, dict) and is_limit_text(b.get("text"))
               for b in content if isinstance(content, list)) if isinstance(content, list) \
        else is_limit_text(content)


def reset_from_info(info: object) -> Optional[float]:
    """`resetsAt` of a `rate_limit_info` (or a transcript's `quotaLimits`)."""
    if not isinstance(info, dict):
        return None
    try:
        at = float(info.get("resetsAt") or 0)
    except (TypeError, ValueError):
        return None
    if at > 1e12:            # milliseconds
        at /= 1000.0
    return at or None


_RESET_RE = re.compile(
    r"(?:resets?|try again|available)\s+(?:on\s+|at\s+)?(?:(?P<mon>[A-Z][a-z]{2})[a-z]*\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?\s*(?:\((?P<tz>[^)]+)\))?", re.I)
_RESET_IN = re.compile(
    r'(?:resets?\s+in|try\s+again\s+in|retry\s+after|reset_seconds["\']?\s*[:=]\s*)'
    r"\s*(?:(?P<h>\d+)\s*(?:h|hr|hrs|hour|hours)\s*)?"
    r"(?:(?P<m>\d+)\s*(?:m|min|mins|minute|minutes)\s*)?"
    r"(?:(?P<s>\d+)\s*(?:s|sec|secs|second|seconds)?)?",
    re.I,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def parse_reset(text: object, now: Optional[float] = None) -> Optional[float]:
    """When the sentence says it resets ("resets 2am (Australia/Melbourne)",
    "resets 3:30pm", "resets Oct 22, 9am (UTC)", "resets in 2h 15m"): the next
    such moment after `now`, in its zone (else local time). None when it says no time."""
    if not isinstance(text, str):
        return None
    now = time.time() if now is None else now
    ri = _RESET_IN.search(text)
    if ri and (ri.group("h") or ri.group("m") or ri.group("s")):
        delta = (int(ri.group("h") or 0) * 3600
                 + int(ri.group("m") or 0) * 60
                 + int(ri.group("s") or 0))
        if delta > 0:
            return float(int(now + delta))
    m = _RESET_RE.search(text)
    if not m or (not m.group("ap") and not m.group("m")):
        return None
    now = time.time() if now is None else now
    tz = None
    if m.group("tz"):
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(m.group("tz").strip())
        except Exception:  # noqa: BLE001 — an unknown zone is local time
            tz = None
    base = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    hour = int(m.group("h")) % 12 if m.group("ap") else int(m.group("h"))
    if (m.group("ap") or "").lower() == "pm":
        hour += 12
    if hour > 23:
        return None
    minute = int(m.group("m") or 0)
    try:
        if m.group("mon"):
            mon = _MONTHS.index(m.group("mon")[:3].lower()) + 1
            at = base.replace(month=mon, day=int(m.group("day")), hour=hour, minute=minute,
                              second=0, microsecond=0)
            if at.timestamp() < now - 86400:
                at = at.replace(year=at.year + 1)
            return at.timestamp()
        at = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    if at.timestamp() <= now:
        at += timedelta(days=1)
    return at.timestamp()


# --- transcripts (desk sessions and background agents) ---------------------------

def _tail_lines(p: Path, max_bytes: int = 262144) -> list[dict]:
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for ln in raw.splitlines():
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _last_turn_line(lines: list[dict]) -> Optional[dict]:
    """The last user or assistant line (a sidechain's own lines count)."""
    return next((o for o in reversed(lines) if o.get("type") in ("user", "assistant")), None)


def transcript_limit(p: "Path | str") -> Optional[dict]:
    """When a transcript (a session's or a subagent's) ends on the limit:
    `{"resets_at", "kind", "text", "at"}`; else None — it has gone on since,
    or never hit it."""
    last = _last_turn_line(_tail_lines(Path(p)))
    if not last or not is_limit_line(last):
        return None
    info = ((last.get("apiErrorParams") or {}).get("rate_limit_info")
            or last.get("quotaLimits") or {})
    content = (last.get("message") or {}).get("content")
    text = next((b.get("text") for b in content if isinstance(b, dict) and b.get("text")), "") \
        if isinstance(content, list) else str(content or "")
    at = None
    try:
        at = datetime.fromisoformat(str(last.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        pass
    return {"resets_at": reset_from_info(info) or parse_reset(text, at),
            "kind": str(info.get("rateLimitType") or "") if isinstance(info, dict) else "",
            "text": text, "at": at}


def last_user_text(p: "Path | str") -> str:
    """The last thing typed into a transcript (not a tool result), for the note."""
    for o in reversed(_tail_lines(Path(p))):
        if o.get("type") != "user" or o.get("isMeta") or o.get("isSidechain"):
            continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, str) and c.strip():
            return " ".join(c.split())[:300]
        if isinstance(c, list):
            words = [b.get("text") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            if words and any(w and w.strip() for w in words):
                return " ".join(" ".join(w or "" for w in words).split())[:300]
    return ""


def limited_agents(subagents: "Path | str | None", since: float = 0.0) -> list[dict]:
    """The background agents in a session's `subagents/` whose transcript
    ends on the limit (written to since `since`): `{"id", "description",
    "type": "local_agent", "resets_at"}` each. One continued since has a
    later line and is not among them."""
    if not subagents:
        return []
    d = Path(subagents)
    out = []
    try:
        files = sorted(d.glob("agent-*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return []
    for p in files:
        try:
            if p.stat().st_mtime < since:
                continue
        except OSError:
            continue
        hit = transcript_limit(p)
        if not hit:
            continue
        aid = p.stem[len("agent-"):]
        desc = ""
        try:
            desc = str(json.loads(p.with_name(f"agent-{aid}.meta.json").read_text())
                       .get("description") or "")
        except (OSError, ValueError, AttributeError):
            pass
        out.append({"id": aid, "description": desc[:200], "type": "local_agent",
                    "resets_at": hit.get("resets_at")})
    return out


# --- the record ---------------------------------------------------------------------

def read() -> Optional[dict]:
    try:
        rec = json.loads(path().read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _update(fn: "Callable[[Optional[dict]], Optional[dict]]") -> Optional[dict]:
    """Read, change and write the record under its lock (sessiond and a
    desk session's hook both write it). `fn` returns the new record, or None
    to remove it."""
    from . import _lock

    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "a+") as lk:
        _lock.flock(lk, _lock.LOCK_EX)
        try:
            new = fn(read())
            if new is None:
                p.unlink(missing_ok=True)
            else:
                tmp = p.with_name(f".{p.name}.{os.getpid()}")
                tmp.write_text(json.dumps(new, indent=1))
                os.replace(tmp, p)
            return new
        finally:
            _lock.flock(lk, _lock.LOCK_UN)


def _merge_tasks(old: list, new: list) -> list:
    seen = {t.get("id") for t in old if isinstance(t, dict) and t.get("id")}
    return list(old) + [t for t in new if isinstance(t, dict)
                        and (not t.get("id") or t["id"] not in seen)]


def note_limit(entry: dict, resets_at: Optional[float] = None, *, kind: str = "",
               text: str = "", source: str = "event", agent: str = "",
               now: Optional[float] = None) -> dict:
    """The limit stopped `entry` (`{"kind", "session", "cwd", "at", …}`).
    The account is limited until `resets_at` (the later of what is known;
    a backoff when nothing is), and `entry` waits for it — once: a session
    already waiting keeps its place and its earliest `at`, and gains any new
    background agents. The record as written."""
    now = time.time() if now is None else now
    eff_agent = str(agent or entry.get("agent") or "").strip().lower()
    entry = {**entry, "at": float(entry.get("at") or now),
             **({"agent": eff_agent} if eff_agent else {})}

    def fn(rec):
        rec = dict(rec or {})
        live = bool(rec) and float(rec.get("resets_at") or 0) + margin_s() > now \
            and rec.get("state") == "limited"
        if not live:
            # A new spell, or a resumed session hit it again: from now, and
            # a backoff that grows while the reset time stays unknown.
            rec["attempt"] = int(rec.get("attempt") or 0) + 1 if rec else 0
            rec["since"] = now
        if resets_at:
            # A reset already past is a backfill (sessiond starting after
            # it): due at once.
            rec["resets_at"] = max(float(resets_at),
                                   float(rec.get("resets_at") or 0) if live else 0.0)
            rec["source"] = source
        elif not live:
            rec["resets_at"] = now + backoff_s(int(rec.get("attempt") or 0))
            rec["source"] = "backoff"
        if kind:
            rec["kind"] = kind
        if text:
            rec["text"] = text
        if eff_agent:
            rec["agent"] = eff_agent
        rec["state"] = "limited"
        rec.pop("next_at", None)
        waiting = [w for w in rec.get("waiting") or [] if isinstance(w, dict)]
        for i, w in enumerate(waiting):
            if w.get("session") == entry.get("session"):
                merged = {**w, **{k: v for k, v in entry.items() if k not in ("at", "tasks")}}
                merged["at"] = min(float(w.get("at") or now), entry["at"])
                merged["tasks"] = _merge_tasks(w.get("tasks") or [], entry.get("tasks") or [])
                merged["turn"] = bool(w.get("turn") or entry.get("turn"))
                waiting[i] = merged
                break
        else:
            waiting.append(entry)
        rec["waiting"] = sorted(waiting, key=lambda w: float(w.get("at") or 0))
        return rec

    clear_warnings(eff_agent or "claude")
    return _update(fn) or {}


def limited(now: Optional[float] = None, rec: Optional[dict] = None,
            *, agent: Optional[str] = None) -> Optional[dict]:
    """The record while the account is still limited (before the reset and
    its margin), else None. When `agent` is given, only matches if that
    harness is limited (untagged limits default to `claude`)."""
    now = time.time() if now is None else now
    rec = read() if rec is None else rec
    if rec and rec.get("state") == "limited" \
            and now < float(rec.get("resets_at") or 0) + margin_s():
        if agent:
            rec_agent = str(rec.get("agent") or "claude")
            waiting_agents = {str(w.get("agent") or rec_agent)
                              for w in rec.get("waiting") or [] if isinstance(w, dict)}
            if agent != rec_agent and agent not in waiting_agents:
                return None
        return rec
    return None


def waiting_entry(session: str, rec: Optional[dict] = None) -> Optional[dict]:
    rec = read() if rec is None else rec
    for w in (rec or {}).get("waiting") or []:
        if isinstance(w, dict) and w.get("session") == session:
            return w
    return None


def view(now: Optional[float] = None, rec: Optional[dict] = None) -> Optional[dict]:
    """The account's limit as the app is told it (§6.1 `quota`):
    `{"resets_at", "since", "state": "waiting" | "resuming", "kind",
    "waiting": <sessions waiting>}`, or None when there is none."""
    now = time.time() if now is None else now
    rec = read() if rec is None else rec
    if not rec or not rec.get("resets_at"):
        return None
    waiting = [w for w in rec.get("waiting") or [] if isinstance(w, dict)]
    is_limited = limited(now, rec) is not None
    if not is_limited and not waiting:
        return None
    return {"resets_at": round(float(rec["resets_at"]), 3),
            "since": rec.get("since"), "kind": rec.get("kind") or "",
            "state": "waiting" if is_limited else "resuming", "waiting": len(waiting)}


def session_view(session: str, now: Optional[float] = None,
                 rec: Optional[dict] = None) -> Optional[dict]:
    """`quota` on one thread: `{"resets_at", "state"}` while it waits to be
    resumed, else None."""
    rec = read() if rec is None else rec
    if not waiting_entry(session, rec):
        return None
    v = view(now, rec)
    return {"resets_at": v["resets_at"], "state": v["state"]} if v else None


def add_waiting(entry: dict, now: Optional[float] = None) -> bool:
    """Put `entry` in line while the account is limited (a message held for
    it), unless it is already there. Whether it is in line now."""
    now = time.time() if now is None else now
    out = []

    def fn(rec):
        if not rec:
            return rec
        waiting = [w for w in rec.get("waiting") or [] if isinstance(w, dict)]
        if not any(w.get("session") == entry.get("session") for w in waiting):
            waiting.append({**entry, "at": float(entry.get("at") or now)})
            rec["waiting"] = sorted(waiting, key=lambda w: float(w.get("at") or 0))
        out.append(True)
        return rec

    _update(fn)
    return bool(out)


def take_next(now: Optional[float] = None) -> Optional[dict]:
    """The next session to resume, taken out of line, when one is due: the
    reset (and its margin) has passed and the last resume was `gap_s()` ago.
    The record goes once nobody is left waiting."""
    now = time.time() if now is None else now
    got: list = []

    def fn(rec):
        if not rec:
            return None
        if now < float(rec.get("resets_at") or 0) + margin_s():
            return rec
        waiting = [w for w in rec.get("waiting") or [] if isinstance(w, dict)]
        if not waiting:
            return None
        if rec.get("next_at") and now < float(rec["next_at"]):
            return rec
        got.append(waiting.pop(0))
        rec["waiting"] = waiting
        rec["state"] = "resuming"
        rec["next_at"] = now + gap_s()
        return rec

    if not path().exists():
        return None
    _update(fn)
    return got[0] if got else None


def put_back(entry: dict, retry_s: float = 10.0, now: Optional[float] = None) -> None:
    """A session that could not be resumed yet (a draft in its pane, no room
    on the host) goes back to the front, tried again after `retry_s`."""
    now = time.time() if now is None else now

    def fn(rec):
        rec = dict(rec or {"since": now, "resets_at": now, "state": "resuming",
                           "attempt": 0})
        waiting = [w for w in rec.get("waiting") or [] if isinstance(w, dict)
                   and w.get("session") != entry.get("session")]
        rec["waiting"] = [entry] + waiting
        rec["next_at"] = now + retry_s
        return rec

    _update(fn)


def drop(session: str) -> Optional[dict]:
    """Take `session` out of line (it was picked up another way); its entry."""
    got: list = []

    def fn(rec):
        if not rec:
            return None
        waiting = []
        for w in rec.get("waiting") or []:
            if isinstance(w, dict) and w.get("session") == session:
                got.append(w)
            elif isinstance(w, dict):
                waiting.append(w)
        rec["waiting"] = waiting
        return rec

    if not path().exists():
        return None
    _update(fn)
    return got[0] if got else None


def lifted(now: Optional[float] = None, *, min_age_s: float = 60.0) -> bool:
    """A request went through while the record says limited (a session's
    `rate_limit_event` came back allowed): the limit is off sooner than it
    said (more usage bought, a model-only limit). The reset is moved to now,
    so the waiting are resumed. Ignored in the first `min_age_s`, when an
    allowed answer can still be one sent before the limit. Whether it moved."""
    now = time.time() if now is None else now
    moved = []

    def fn(rec):
        if rec and rec.get("state") == "limited" \
                and now - float(rec.get("since") or now) >= min_age_s \
                and float(rec.get("resets_at") or 0) > now - margin_s():
            rec["resets_at"] = now - margin_s()
            rec["source"] = "lifted"
            moved.append(True)
        return rec

    if not path().exists():
        return False
    _update(fn)
    return bool(moved)


def clear() -> None:
    _update(lambda rec: None)


# --- approaching-limit warnings ----------------------------------------------------

WARN_PCT_DEFAULT = 80.0


def warn_pct_threshold() -> float:
    return max(1.0, min(99.0, _env_f("MEDIA_QUOTA_WARN_PCT", WARN_PCT_DEFAULT)))


def warnings_path() -> Path:
    from ._paths import state_dir

    return state_dir() / "quota-warnings.json"


def _warn_step(pct: float) -> int:
    if pct >= 95.0:
        return 95
    if pct >= 90.0:
        return 90
    thresh = warn_pct_threshold()
    if pct >= thresh:
        return int(thresh)
    return 0


def _normalize_pct(val: object) -> Optional[float]:
    try:
        f = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return 0.0
    return round(f * 100.0 if f <= 1.0 else f, 1)


def _update_warnings(fn: "Callable[[dict], Optional[dict]]") -> Optional[dict]:
    from . import _lock

    p = warnings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "a+") as lk:
        _lock.flock(lk, _lock.LOCK_EX)
        try:
            try:
                cur = json.loads(p.read_text()) if p.exists() else {}
            except (OSError, ValueError):
                cur = {}
            new = fn(cur if isinstance(cur, dict) else {})
            if not new or not new.get("warnings"):
                p.unlink(missing_ok=True)
                return None
            tmp = p.with_name(f".{p.name}.{os.getpid()}")
            tmp.write_text(json.dumps(new, indent=1))
            os.replace(tmp, p)
            return new
        finally:
            _lock.flock(lk, _lock.LOCK_UN)


def note_warning(
    *,
    agent: str = "claude",
    utilization: Optional[float] = None,
    resets_at: Optional[float] = None,
    kind: str = "",
    text: str = "",
    session: str = "",
    cwd: str = "",
    now: Optional[float] = None,
) -> Optional[dict]:
    """Record a quota-approaching warning when utilization >= threshold or
    the harness explicitly signalled `allowed_warning`. Deduplicated per
    `(agent, kind)` and step band (80%, 90%, 95%) until `resets_at` passes."""
    now = time.time() if now is None else now
    agent = str(agent or "claude").strip().lower() or "claude"
    pct = _normalize_pct(utilization)
    thresh = warn_pct_threshold()
    if pct is not None and pct < thresh and not text:
        return None
    step = _warn_step(pct if pct is not None else thresh)
    if step == 0 and text:
        step = int(thresh)
    if step == 0:
        return None
    created: list[dict] = []

    def fn(data: dict) -> Optional[dict]:
        by_agent = dict(data.get("warnings") or {}) if isinstance(data.get("warnings"), dict) else {}
        prev = by_agent.get(agent) if isinstance(by_agent.get(agent), dict) else {}
        prev_reset = float(prev.get("resets_at") or 0)
        prev_step = int(prev.get("step") or 0)
        if prev and (prev_reset > now or (not resets_at and now - float(prev.get("at") or 0) < 3600)):
            if step <= prev_step and str(prev.get("kind") or "") == str(kind or prev.get("kind") or ""):
                return {"warnings": by_agent}
        target = float(resets_at) if resets_at and resets_at > now else (
            prev_reset if prev_reset > now else 0.0
        )
        row = {
            "agent": agent,
            "at": round(now, 3),
            "step": step,
            "utilization": pct if pct is not None else float(step),
            "resets_at": round(target, 3) if target else None,
            "kind": str(kind or prev.get("kind") or ""),
            "text": str(text or "")[:200],
            "session": str(session or ""),
            "cwd": str(cwd or ""),
            "notified": False,
        }
        by_agent[agent] = row
        created.append(row)
        return {"warnings": by_agent}

    _update_warnings(fn)
    return created[0] if created else None


def check_rate_limit_info(
    info: Optional[dict],
    *,
    agent: str = "claude",
    session: str = "",
    cwd: str = "",
    now: Optional[float] = None,
) -> Optional[dict]:
    """Inspect a rate_limit_info / rate_limits dict from Claude, Codex,
    Antigravity, or OpenCode and record a warning if approaching the limit."""
    if not isinstance(info, dict):
        return None
    status = str(info.get("status") or "").lower()
    if status == "rejected":
        return None
    raw_pct = (
        info.get("utilization")
        if info.get("utilization") is not None
        else info.get("used_percent")
        if info.get("used_percent") is not None
        else info.get("used_percentage")
    )
    pct = _normalize_pct(raw_pct)
    resets_at = reset_from_info(info)
    kind = str(info.get("rateLimitType") or info.get("limit_name") or info.get("window") or "")
    thresh = warn_pct_threshold()
    if status == "allowed_warning" or (pct is not None and pct >= thresh):
        msg = str(info.get("message") or info.get("warning") or "")
        if not msg:
            pct_str = f"{pct:.0f}%" if pct is not None else f"{thresh:.0f}%+"
            msg = f"{agent.capitalize()} usage is at {pct_str}" + (f" ({kind})" if kind else "")
        return note_warning(
            agent=agent,
            utilization=pct if pct is not None else thresh,
            resets_at=resets_at,
            kind=kind,
            text=msg,
            session=session,
            cwd=cwd,
            now=now,
        )
    return None


def warnings(now: Optional[float] = None, *, agent: Optional[str] = None) -> list[dict]:
    """Active quota-approaching warnings across harnesses."""
    now = time.time() if now is None else now
    p = warnings_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    by_agent = (data or {}).get("warnings") if isinstance(data, dict) else {}
    if not isinstance(by_agent, dict):
        return []
    out: list[dict] = []
    for k, row in by_agent.items():
        if not isinstance(row, dict):
            continue
        if agent and k != agent:
            continue
        r_at = float(row.get("resets_at") or 0)
        at = float(row.get("at") or 0)
        if r_at > 0 and r_at <= now:
            continue
        if r_at == 0 and now - at > 6 * 3600:
            continue
        out.append(row)
    return out


def pop_unnotified_warnings(now: Optional[float] = None) -> list[dict]:
    """Return and mark `notified=True` any active warnings not yet notified."""
    now = time.time() if now is None else now
    if not warnings_path().exists():
        return []
    fresh: list[dict] = []

    def fn(data: dict) -> Optional[dict]:
        by_agent = dict(data.get("warnings") or {}) if isinstance(data.get("warnings"), dict) else {}
        for k, row in list(by_agent.items()):
            if not isinstance(row, dict):
                del by_agent[k]
                continue
            r_at = float(row.get("resets_at") or 0)
            at = float(row.get("at") or 0)
            if (r_at > 0 and r_at <= now) or (r_at == 0 and now - at > 6 * 3600):
                del by_agent[k]
                continue
            if not row.get("notified"):
                updated = {**row, "notified": True}
                by_agent[k] = updated
                fresh.append(updated)
        return {"warnings": by_agent} if by_agent else None

    _update_warnings(fn)
    return fresh


def clear_warnings(agent: Optional[str] = None) -> None:
    if not warnings_path().exists():
        return
    if agent is None:
        warnings_path().unlink(missing_ok=True)
        return

    def fn(data: dict) -> Optional[dict]:
        by_agent = dict(data.get("warnings") or {}) if isinstance(data.get("warnings"), dict) else {}
        by_agent.pop(agent, None)
        return {"warnings": by_agent} if by_agent else None

    _update_warnings(fn)
