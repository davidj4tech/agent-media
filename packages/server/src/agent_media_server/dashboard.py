"""`GET /dashboard`: the app's home screen in one answer (server-contract §6.11).

What needs you, what is working, what is being said, where each thread was,
where a new chat can start, and how the machines are — every piece read from
a sweep something else already keeps warm, so one poll every few seconds
costs about what `/sessions/state` does:

* live rows, their state and memory, and the host's memory: the
  `sessions.session_states` sweep (cached 3 s);
* titles, recaps, rested and archived marks: `sessions_index()`, cached here
  for `INDEX_TTL_S` (it lists tmux panes and walks the shelf);
* a stopped session's dialog: `sessions.approval_for` (one capture of that
  pane) or the headless driver's pending request — only for rows the sweep
  says are on a dialog;
* what a working turn is doing: the activity file its hooks append to;
* the speech bar's `/speech/now`, cut down, over the last speech snapshot
  read (`_speech_state`: a snapshot costs a `media` subprocess);
* the machines: `/proc/meminfo`, the reaper's log, `systemctl --user
  is-active` and `tailscale status --json`, cached `MEDIA_DASHBOARD_HOSTS_TTL`
  (15 s) — never ssh, never a peer's own answer in the request path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import auth, sessions, speech

#: `sessions_index()` is a tmux listing plus a walk of the shelf; the
#: dashboard is polled every ~5 s, and a title or recap a few seconds old is
#: no loss.
INDEX_TTL_S = 4.0
#: Rows in `recent`.
RECENT_ROWS = 8
#: The machine part: subprocesses and a log read, none of which changes fast.
HOSTS_TTL_S = 15.0
#: The reaper log is read from its end, this far back at most.
REAP_TAIL_BYTES = 16 * 1024
SHELL_UNIT = "sasonica-shell"
SESSIOND_UNIT = "agent-media-sessiond"

_LOCK = threading.Lock()
_INDEX: tuple[float, list] = (0.0, [])
_HOSTS: tuple[float, object] = (0.0, None)
#: `{session: path | None}` — transcript lookups are a glob each.
_TRANSCRIPTS: dict[str, Path | None] = {}

#: Indirections, so the tests fake the machine without patching the stdlib.
_run = subprocess.run
_which = shutil.which

#: The canvas's speech snapshot runs `media popup-status` (1–3 s on red5), so
#: the dashboard serves the last one it read — stale-while-revalidate: younger
#: than SPEECH_FRESH_S it is used as is; older, it is still used (up to
#: SPEECH_STALE_S) while one background read replaces it; past that, or with
#: none yet, the request waits for a read.
SPEECH_FRESH_S = 1.5
SPEECH_STALE_S = 20.0
_SPEECH: tuple[float, dict | None] = (0.0, None)
_SPEECH_BUSY = threading.Event()

_SPEECH_KEYS = ("live", "speaking", "paused", "session", "title", "sentence", "target",
                "replay")


def _reset_for_tests() -> None:
    global _INDEX, _HOSTS, _SPEECH
    _INDEX = (0.0, [])
    _HOSTS = (0.0, None)
    _SPEECH = (0.0, None)
    _SPEECH_BUSY.clear()
    _TRANSCRIPTS.clear()


def _index() -> list[dict]:
    global _INDEX
    with _LOCK:
        at, rows = _INDEX
        if not at or time.monotonic() - at > INDEX_TTL_S:
            rows = sessions.sessions_index()
            _INDEX = (time.monotonic(), rows)
        return rows


# --- sessions ------------------------------------------------------------------------

def _approval(session: str, live: dict[str, str], headless: bool) -> dict | None:
    if headless:
        from . import driver

        return driver.headless_driver().approval(session)
    pane = live.get(session, "")
    if not pane:
        return None
    return sessions.approval_for(pane, sessions._agent_of_pane(pane), session)


def _working(session: str, title: str) -> dict:
    from agent_media_core import activity

    try:
        w = activity.attach(session, [])
    except (OSError, ValueError):
        w = None
    w = w or {}
    since = w.get("since")
    return {"session": session, "title": title, "current": str(w.get("current") or ""),
            "since": round(float(since), 3) if since else None,
            "count": int(w.get("count") or 0)}


def _transcript_mtime(session: str) -> float | None:
    if session not in _TRANSCRIPTS:
        from agent_media_core import conversation

        _TRANSCRIPTS[session] = conversation.transcript(session)
    path = _TRANSCRIPTS[session]
    if path is None:
        return None
    try:
        return round(path.stat().st_mtime, 3)
    except OSError:
        _TRANSCRIPTS.pop(session, None)
        return None


def _recent(index: list[dict]) -> list[dict]:
    rows = []
    for r in index:
        if r.get("archived"):
            continue
        sid = str(r.get("session") or "")
        at = r.get("at") if not r.get("live") else None
        if at is None:
            at = _transcript_mtime(sid)
        if at is None and r.get("recap"):
            at = r["recap"].get("at")
        rows.append({"session": sid, "title": str(r.get("title") or ""),
                     "recap": r.get("recap"), "at": at, "live": bool(r.get("live")),
                     "rested": r.get("rested"), "project": r.get("project"),
                     "cwd": r.get("cwd")})
    rows.sort(key=lambda x: -(x["at"] or 0))
    return rows[:RECENT_ROWS]


def _read_speech() -> dict:
    global _SPEECH
    try:
        st = speech.current_state()
    finally:
        _SPEECH_BUSY.clear()
    _SPEECH = (time.monotonic(), st)
    return st


def _speech_state() -> dict:
    at, st = _SPEECH
    age = time.monotonic() - at
    if st is None or age > SPEECH_STALE_S:
        _SPEECH_BUSY.set()
        return _read_speech()
    if age > SPEECH_FRESH_S and not _SPEECH_BUSY.is_set():
        _SPEECH_BUSY.set()
        threading.Thread(target=_read_speech, name="dashboard-speech", daemon=True).start()
    return st


# --- machines ------------------------------------------------------------------------

def _is_active(unit: str) -> bool | None:
    if not _which("systemctl"):
        return None
    try:
        out = _run(["systemctl", "--user", "is-active", unit], capture_output=True,
                   text=True, timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    word = out.stdout.strip()
    if word == "active":
        return True
    if word in ("inactive", "failed", "activating", "deactivating", "unknown"):
        return False
    return None


_STAMP = re.compile(r"^(\S+) (dry-run|apply) (\S+)")


def _epoch_iso(s: str) -> float | None:
    if not s or s.startswith("0001-"):
        return None
    try:
        return round(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp(), 3)
    except ValueError:
        # Tailscale writes a tenth of a second ("…00.1Z"); older Pythons want
        # exactly 0, 3 or 6 digits.
        m = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?$", s)
        if not m:
            return None
        try:
            return round(datetime.fromisoformat(
                m.group(1) + (m.group(2) or "+00:00").replace("Z", "+00:00")).timestamp(), 3)
        except ValueError:
            return None


def reaper_last_run() -> dict:
    """`{"mode", "last_run_at", "closed_last_run"}` from the reaper log's end:
    the last run is the lines that share the newest stamp."""
    from . import reap

    out = {"mode": None, "last_run_at": None, "closed_last_run": 0}
    try:
        with open(reap.log_path(), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - REAP_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return out
    lines = [ln for ln in tail.splitlines() if _STAMP.match(ln)]
    if not lines:
        return out
    stamp, mode, _ = _STAMP.match(lines[-1]).groups()
    run = [ln for ln in lines if ln.startswith(stamp + " ")]
    out["mode"] = mode
    out["last_run_at"] = _epoch_iso(stamp)
    out["closed_last_run"] = sum(1 for ln in run if _STAMP.match(ln).group(3) == "closed")
    return out


def tailscale_peers() -> dict[str, dict] | None:
    """`{hostname lowercased: {"online", "last_seen"}}`, or None when tailscale
    cannot be asked."""
    if not _which("tailscale"):
        return None
    try:
        out = _run(["tailscale", "status", "--json"], capture_output=True,
                   text=True, timeout=2.0)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    peers = {}
    for p in (data.get("Peer") or {}).values():
        if not isinstance(p, dict):
            continue
        name = str(p.get("HostName") or "").lower()
        dns = str(p.get("DNSName") or "").split(".")[0].lower()
        row = {"online": bool(p.get("Online")), "last_seen": _epoch_iso(str(p.get("LastSeen") or ""))}
        for key in {name, dns} - {""}:
            peers.setdefault(key, row)
    return peers


def _role() -> str:
    try:
        from agent_media_core import config

        roles = config.host_roles()
    except Exception:  # noqa: BLE001 — a bad config file is not a failed dashboard
        roles = None
    return ",".join(sorted(roles)) if roles else ""


def _peer_names() -> list[str]:
    raw = os.environ.get("MEDIA_DASHBOARD_PEERS")
    raw = "hpo" if raw is None else raw
    return [n.strip() for n in raw.replace(",", " ").split() if n.strip()]


def _machines() -> tuple[list[dict], list[dict]]:
    """`(the local host's fixed facts, peers)` and the agents — the slow part,
    cached HOSTS_TTL_S."""
    from agent_media_core import harnesses

    local = {"name": socket.gethostname().split(".")[0], "role": _role(),
             "reaper": reaper_last_run(),
             "shell": {"service": os.environ.get("MEDIA_DASHBOARD_SHELL_UNIT") or SHELL_UNIT,
                       "active": None},
             "sessiond": {"service": os.environ.get("MEDIA_DASHBOARD_SESSIOND_UNIT")
                          or SESSIOND_UNIT, "active": None}}
    local["shell"]["active"] = _is_active(local["shell"]["service"])
    local["sessiond"]["active"] = _is_active(local["sessiond"]["service"])
    ts = tailscale_peers()
    peers = []
    for name in _peer_names():
        seen = (ts or {}).get(name.lower())
        peers.append({"name": name, "role": "peer", "local": False,
                      "online": seen["online"] if seen else None,
                      "last_seen": seen["last_seen"] if seen else None,
                      "sessions": None, "mem_used_mb": None, "mem_total_mb": None,
                      "mem_available_mb": None, "sessions_mem_mb": None, "tight": None,
                      "reaper": None, "shell": None, "sessiond": None})
    agents = [{"name": n, "present": bool(harnesses.program(n))} for n in harnesses.HARNESSES]
    return [local] + peers, agents


def _hosts_cached() -> tuple[list[dict], list[dict]]:
    global _HOSTS
    ttl = HOSTS_TTL_S
    try:
        ttl = float(os.environ.get("MEDIA_DASHBOARD_HOSTS_TTL") or HOSTS_TTL_S)
    except ValueError:
        pass
    with _LOCK:
        at, cached = _HOSTS
        if cached is None or time.monotonic() - at > ttl:
            cached = _machines()
            _HOSTS = (time.monotonic(), cached)
        return cached


def _hosts(live_rows: list[dict], host: dict) -> tuple[list[dict], list[dict]]:
    from . import reap

    fixed, agents = _hosts_cached()
    local, peers = dict(fixed[0]), [dict(p) for p in fixed[1:]]
    total, avail = host.get("mem_total_mb"), host.get("mem_available_mb")
    local.update({
        "local": True, "online": True, "last_seen": None, "sessions": len(live_rows),
        "mem_used_mb": total - avail if total is not None and avail is not None else None,
        "mem_total_mb": total, "mem_available_mb": avail,
        "sessions_mem_mb": host.get("sessions_mem_mb"),
        "tight": reap.is_tight(host, reap.Config.from_env()) if avail is not None else None})
    order = ("name", "role", "local", "online", "last_seen", "sessions", "mem_used_mb",
             "mem_total_mb", "mem_available_mb", "sessions_mem_mb", "tight", "reaper",
             "shell", "sessiond")
    return [{k: local[k] for k in order}] + peers, agents


# --- the answer ----------------------------------------------------------------------

def build(bearer: str) -> dict:
    """The dashboard body (no gate: `dashboard` gates)."""
    _ok, st = sessions.session_states(bearer)
    rows, host = st["sessions"], st["host"]
    index = _index()
    titles = {str(r.get("session")): str(r.get("title") or "") for r in index}
    where = {str(r.get("session")): r for r in index}
    live = sessions.live_sessions()
    needs, working = [], []
    for r in rows:
        sid = str(r["session"])
        headless = r.get("driver") == "headless"
        if r["state"] == "approval":
            ap = _approval(sid, live, headless)
            if ap:
                row = {"session": sid, "title": titles.get(sid, ""),
                       "kind": "question" if ap.get("kind") == "question" else "approval",
                       "approval": ap}
                if headless:
                    row["driver"] = "headless"
                needs.append(row)
        elif r["state"] == "working":
            working.append(_working(sid, titles.get(sid, "")))
    # The project line under each card's title: the index's, else worked out
    # (a session the index left out, e.g. a live one with no title yet).
    missing = [r for r in needs + working if str(r["session"]) not in where]
    sessions.add_projects(missing)
    for r in needs + working:
        known = where.get(str(r["session"]))
        if known is not None:
            r["project"], r["cwd"] = known.get("project"), known.get("cwd")
    _sok, now = speech.speech_now(bearer, _speech_state())
    hosts, agents = _hosts(rows, host)
    return {"at": round(time.time(), 3), "needs_you": needs, "working": working,
            "speech": {"now": {k: now.get(k) for k in _SPEECH_KEYS},
                       "queued": list(now.get("queued") or [])},
            "recent": _recent(index), "places": sessions.places(),
            "agents": agents, "hosts": hosts}


def dashboard(bearer: str) -> tuple[bool, dict]:
    """`/dashboard`, gated like `/targets`."""
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    return True, build(bearer)
