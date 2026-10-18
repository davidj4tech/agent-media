"""Restart the services this checkout owns, safely enough for an agent to run.

`media restart-services` is a thin wrapper over `run()`. A server route on any
host (the phone's runit, a Windows box) can call `run()` too and answer its
result dict as JSON; `POST /services/restart` does (server app.py), with
`detach_own` so a server restarting itself answers first, and
`run_remote()` is the other end of it (`media restart-services --host NAME`,
as a paired device of that host's server: peer_link.py). Nothing here names
a host or a path of one machine.

What makes it safe to run unattended (David, 18 Oct 2026):

* **`only`**: just the named services. Short names work (`sessiond`,
  `canvas`, `tts-shim`), matched against the units the checkout owns: the
  same enumeration selfcheck grades (`cli._runit_service_states`,
  `cli._systemd_service_states`).
* **busy checks**, skipped with `force`:
  - sessiond: refused while any live session is working or starting, waiting
    on an approval, holding queued messages, or has a task in flight.
    The caller's own session (`MEDIA_SESSIOND_SESSION`, which sessiond sets
    in its children's environment) is left out of this check, except that
    more than one task on it is refused too. The one tolerated task is the
    command running this, or the subagent that ran it; any further task would
    die with the restart. A restart of sessiond *now* from inside one of its
    sessions is refused as well, because it would end the very chat asking
    for it. Use `after`.
  - speech (tts-shim, sink-speech, speech-state, clips, the speech fifo):
    refused while a reply is being spoken.
* **`after`**: the restart runs later, outside the caller. Under systemd it
  is a transient user unit (`systemd-run --user --on-active=N`), so it
  survives the caller being killed. Elsewhere (runit, Windows) it is a
  detached child in its own session. The detached run checks again just
  before it acts. While busy it retries every `MEDIA_RESTART_RETRY_S` (15 s)
  for up to `MEDIA_RESTART_WAIT_MAX_S` (600 s), then gives up and raises an
  alert. At that point the caller's own session counts like any other, so a
  turn still going is waited for rather than cut off.
* **health check**: each restarted service must be up within
  `MEDIA_RESTART_HEALTH_S` (20 s). A canvas must also answer HTTP. A failure
  raises the host alert `restart-services.<host>` (§6.17), which the phone
  sees. A clean restart reports it ok, which clears an old one.

Exit codes: 0 done (or scheduled), 1 a restart or health check failed,
2 an unknown `only` name, 3 busy (refused).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

#: Short names that are not just the unit name less its prefix. `canvas` is
#: red5's `agent-media-visual-canvas`, the phone's loopback `canvas-local`
#: and an Android install's `sasonica-canvas`, so `--only canvas` means the
#: canvas wherever it runs.
ALIASES = {"canvas": ("visual-canvas", "canvas-local", "sasonica-canvas")}
#: Prefixes a short name drops: systemd's `agent-media-x`, runit's `media-x`.
PREFIXES = ("agent-media-", "media-")
#: Services a restart of which cuts off a reply being spoken (short names).
SPEECH = ("tts-shim", "tts-shim-private", "sink-speech", "speech-state", "clips",
          "am-snapfifo@am")
#: The session holder; restarting it ends every chat it holds.
SESSIOND = "sessiond"
#: sessiond session states in which a turn is under way.
BUSY_STATES = ("starting", "working", "approval")

OK, FAILED, UNKNOWN, BUSY = 0, 1, 2, 3

#: The detached run's waits (its delay and its busy retries); a hook for tests.
_sleep = time.sleep


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def health_s() -> float:
    return _env_f("MEDIA_RESTART_HEALTH_S", 20.0)


def retry_s() -> float:
    return _env_f("MEDIA_RESTART_RETRY_S", 15.0)


def wait_max_s() -> float:
    return _env_f("MEDIA_RESTART_WAIT_MAX_S", 600.0)


@dataclass
class Service:
    name: str    # unit stem (systemd) or service dir name (runit)
    state: str   # up | down | failed | parked | timed | …
    kind: str    # systemd | runit

    @property
    def short(self) -> str:
        return short_name(self.name)


def short_name(name: str) -> str:
    for p in PREFIXES:
        if name.startswith(p):
            name = name[len(p):]
            break
    for alias, real in ALIASES.items():
        if name in real:
            return alias
    return name


def services() -> list[Service]:
    """Everything this checkout owns, as selfcheck enumerates it."""
    from . import cli

    root = cli._checkout_root()
    return ([Service(n, s, "runit") for n, s in cli._runit_service_states(root)]
            + [Service(n, s, "systemd") for n, s in cli._systemd_service_states()])


def resolve(only: "Optional[list[str]]", svcs: list[Service]
            ) -> "tuple[list[Service], list[str]]":
    """(the services `only` names, in enumeration order; the names that match
    none). No `only` is all of them."""
    if not only:
        return list(svcs), []
    wanted, unknown = set(), []
    for raw in only:
        n = raw.strip().removesuffix(".service")
        hits = [s for s in svcs if n in (s.name, s.short)
                or s.name in ALIASES.get(n, ())]
        if not hits:
            unknown.append(raw)
        wanted.update(id(s) for s in hits)
    return [s for s in svcs if id(s) in wanted], unknown


def is_speech(s: Service) -> bool:
    return s.short in SPEECH or s.name in SPEECH


def is_sessiond(s: Service) -> bool:
    return s.short == SESSIOND


# --- busy checks -------------------------------------------------------------------


def sessiond_sessions() -> list[dict]:
    """The sessions sessiond holds live, from its `list` op. When it does not
    answer, its records on disk whose process is still alive: a restart
    terminates such orphans too. [] on a host without the server package."""
    try:
        from agent_media_server import sessiond
        from agent_media_server.driver import headless
    except ImportError:
        return []
    there = sessiond.use_tcp() or sessiond.socket_path().exists()
    r = headless.call("list", timeout=5.0) if there else {"ok": False}
    if r.get("ok"):
        return [v for v in r.get("sessions") or [] if v.get("live")]
    from . import procinfo

    out = []
    for rec in sessiond.records():
        pid = rec.get("pid")
        if pid and rec.get("state") in sessiond.LIVE_STATES and procinfo.alive(pid):
            out.append(rec)
    return out


def _label(v: dict) -> str:
    sid = str(v.get("session") or "?")[:8]
    first = " ".join(str(v.get("first_text") or "").split())
    if len(first) > 40:
        first = first[:39] + "…"
    return f"session {sid}" + (f" ({first})" if first else "")


def session_busy(v: dict) -> list[str]:
    why = []
    state = str(v.get("state") or "")
    if state in BUSY_STATES:
        why.append(f"is {state}")
    if v.get("pending"):
        why.append(f"has {len(v['pending'])} approval(s) pending")
    if v.get("queued"):
        why.append(f"holds {len(v['queued'])} queued message(s)")
    if int(v.get("tasks") or 0) > 0:
        why.append(f"has {int(v['tasks'])} task(s) in flight")
    return why


def speech_in_flight() -> bool:
    from . import cli

    return cli._speech_in_flight()


def busy(selected: list[Service], caller: str = "", *, now: bool = False) -> list[str]:
    """Why restarting `selected` now would cut something off ([] = clear).

    `caller` is the sessiond session asking (left out of the check, but see
    the module doc). `now` means the restart would happen in this process."""
    reasons: list[str] = []
    if any(is_sessiond(s) for s in selected):
        for v in sessiond_sessions():
            if caller and v.get("session") == caller:
                tasks = int(v.get("tasks") or 0)
                if tasks > 1:
                    reasons.append(f"sessiond: your own session has {tasks - 1} other "
                                   "task(s) in flight; they would die with it")
                continue
            why = session_busy(v)
            if why:
                reasons.append(f"sessiond: {_label(v)} {', '.join(why)}")
        if now and caller:
            reasons.append("sessiond: restarting it now would end this chat (you run "
                           "inside one of its sessions). Use --after 10")
    speech = [s.short for s in selected if is_speech(s)]
    if speech and speech_in_flight():
        reasons.append(f"speech: a reply is being spoken ({', '.join(speech)} would "
                       "cut it off)")
    return reasons


# --- restarting, checking, alerting ------------------------------------------------


def _svdir() -> "Optional[Path]":
    from . import cli

    return cli._sv_dir()


def _restart_one(s: Service) -> "tuple[bool, str]":
    if s.kind == "runit":
        svdir = _svdir()
        cmd = ["sv", "restart", str(Path(svdir or ".") / s.name)]
        env = {**os.environ, "SVDIR": str(svdir or "")}
    else:
        cmd = ["systemctl", "--user", "restart", f"{s.name}.service"]
        env = dict(os.environ)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    detail = (r.stdout or r.stderr or "").strip().replace("\n", " ")
    return r.returncode == 0, detail or (str(r.returncode) if r.returncode else "")


def _is_up(s: Service) -> bool:
    try:
        if s.kind == "runit":
            svdir = _svdir()
            r = subprocess.run(["sv", "status", str(Path(svdir or ".") / s.name)],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "SVDIR": str(svdir or "")})
            return (r.stdout or "").strip().startswith("run:")
        r = subprocess.run(["systemctl", "--user", "is-active", f"{s.name}.service"],
                           capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _canvas_answers() -> bool:
    """Any HTTP answer counts (401 and 404 included): the server is serving."""
    import urllib.error
    import urllib.request

    from . import cli

    url = cli._canvas_web_url()
    try:
        urllib.request.urlopen(url, timeout=3).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def is_canvas(s: Service) -> bool:
    return "canvas" in s.short


# --- the service this process is -------------------------------------------------

#: How long a restart of the service running this code waits, by default, so
#: the answer gets out first (the route's `after` when it names its own server).
SELF_AFTER_S = 10.0


def _ancestors() -> set[int]:
    """This process and its parents, up to init. Read from /proc where there
    is one (Linux, Termux); elsewhere just this process and its parent."""
    out = {os.getpid()}
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1 or pid in out:
            break
        out.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return out


def _main_pid(s: Service) -> int:
    """The service's main process id, 0 when it cannot be told."""
    try:
        if s.kind == "runit":
            svdir = _svdir()
            if not svdir:
                return 0
            return int((Path(svdir) / s.name / "supervise" / "pid").read_text().strip() or 0)
        r = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value",
                            f"{s.name}.service"], capture_output=True, text=True, timeout=10)
        return int((r.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _own_cgroup() -> str:
    try:
        return Path("/proc/self/cgroup").read_text()
    except OSError:
        return ""


def own_services(selected: list[Service]) -> list[Service]:
    """The services in `selected` that this process runs inside: its main
    process is this one or one of its parents (runit and systemd), or this
    process is in its systemd cgroup. Restarting one of them now would kill
    this process before it could answer, or check its health."""
    mine = _ancestors()
    cgroup = _own_cgroup()
    out = []
    for s in selected:
        if s.kind == "systemd" and f"/{s.name}.service" in cgroup:
            out.append(s)
            continue
        pid = _main_pid(s)
        if pid > 1 and pid in mine:
            out.append(s)
    return out


def health(restarted: list[Service], within: "Optional[float]" = None) -> list[str]:
    """The restarted services not up (and, for a canvas, not answering) within
    `within` seconds. A timer's job is not checked: between runs it is meant
    to be inactive."""
    deadline = time.monotonic() + (health_s() if within is None else within)
    todo = [s for s in restarted if s.state != "timed"]
    while True:
        todo = [s for s in todo if not (_is_up(s) and (not is_canvas(s) or _canvas_answers()))]
        if not todo or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    return [f"{s.name}: {'not answering HTTP' if is_canvas(s) and _is_up(s) else 'not up'} "
            f"{health_s() if within is None else within:.0f} s after restart" for s in todo]


def host() -> str:
    h = socket.gethostname().split(".")[0].lower()
    return re.sub(r"[^a-z0-9-]", "-", h) or "host"


def alert(level: str, title: str = "", detail: str = "", fix: str = "") -> None:
    """Say on the host alert `restart-services.<host>` what is true now:
    `agent-alert` when it is installed (it has a fallback for a store that is
    down, and the canvas may be what just failed), else the store in-process,
    else only the journal (stderr), which always gets it."""
    aid = f"restart-services.{host()}"
    if level != "ok":
        print(f"restart-services: ALERT {level}: {title}\n{detail}", file=sys.stderr)
    tool = shutil.which("agent-alert")
    if tool:
        cmd = [tool, "report", aid, "--level", level, "--host", host()]
        for k, v in (("--title", title), ("--detail", detail), ("--fix", fix)):
            if v:
                cmd += [k, v]
        if level == "ok":
            cmd.append("--quiet")
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return
        except (OSError, subprocess.SubprocessError) as e:
            print(f"restart-services: agent-alert failed: {e}", file=sys.stderr)
    try:
        from agent_media_server import alerts
    except ImportError:
        return
    try:
        alerts.report({"id": aid, "level": level, "title": title, "detail": detail,
                       "fix": fix, "host": host()})
    except Exception as e:  # noqa: BLE001 — an alert must not fail the restart
        print(f"restart-services: alert store: {e}", file=sys.stderr)


def restart(selected: list[Service], emit: Callable[[str], None]) -> int:
    rc = OK
    done: list[Service] = []
    failed: list[str] = []
    for s in selected:
        if s.state == "parked":
            emit(f"{s.name}: parked, left alone")
            continue
        ok, detail = _restart_one(s)
        if ok:
            emit(f"{s.name}: restarted{f' ({detail})' if detail else ''}")
            done.append(s)
        else:
            emit(f"{s.name}: restart FAILED: {detail}")
            failed.append(f"{s.name}: restart failed: {detail}")
            rc = FAILED
    down = health(done) if done else []
    for line in down:
        emit(f"{line} (health check FAILED)")
    if done and not down:
        emit(f"health: {len(done)} up")
    problems = failed + down
    if problems:
        names = " ".join(p.split(":", 1)[0] for p in problems)
        alert("warn", f"restart left {len(problems)} service(s) down on {host()}",
              "\n".join(problems), f"media restart-services --only {names}")
        rc = FAILED
    elif done:
        alert("ok")
    return rc


# --- scheduling --------------------------------------------------------------------

#: Environment a detached run needs from its caller. Only these: the unit's
#: properties are readable by `systemctl show`, so no tokens.
PASS_ENV = ("PATH", "HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
            "SVDIR", "MEDIA_SESSIOND_SOCKET", "MEDIA_SESSIOND_TRANSPORT",
            "MEDIA_SESSIOND_TASK_STALE_S", "MEDIA_CANVAS_URL", "MEDIA_VISUAL_URL",
            "MEDIA_VISUAL_PORT", "MEDIA_VISUAL_MAGICDNS_HOST", "AGENT_ALERT_URL",
            "MEDIA_RESTART_HEALTH_S", "MEDIA_RESTART_RETRY_S",
            "MEDIA_RESTART_WAIT_MAX_S")


def fire_argv(selected: list[Service], force: bool) -> list[str]:
    argv = [sys.executable, "-m", "agent_media_core.restart_services", "--fire",
            "--only", *[s.name for s in selected]]
    return argv + (["--force"] if force else [])


def _unit_name() -> str:
    return f"media-restart-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


def log_path() -> Path:
    from ._paths import state_dir

    return state_dir() / "restart-services.log"


def schedule(selected: list[Service], after: float, force: bool) -> dict:
    """Start the detached run `after` seconds from now. Under systemd: a
    transient user unit, out of every caller's cgroup. Elsewhere, or when
    systemd-run is missing: a detached child in its own session, logging to
    `log_path()`."""
    argv = fire_argv(selected, force)
    n = max(1, int(round(after)))
    systemd = any(s.kind == "systemd" for s in selected) and shutil.which("systemd-run")
    if systemd:
        unit = _unit_name()
        cmd = ["systemd-run", "--user", f"--unit={unit}", f"--on-active={n}",
               "--collect", f"--description=media restart-services in {n}s"]
        cmd += [f"--setenv={k}={os.environ[k]}" for k in PASS_ENV if os.environ.get(k)]
        cmd += ["--", *argv]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "systemd-run failed").strip())
        return {"how": "systemd-run", "unit": unit,
                "log": f"journalctl --user -u {unit}"}
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "MEDIA_SESSIOND_SESSION"}
    env["MEDIA_RESTART_DELAY_S"] = str(n)
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kw["start_new_session"] = True
    with open(log, "a") as fh:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                             env=env, close_fds=True, **kw)
    return {"how": "detached", "unit": f"pid {p.pid}", "log": str(log)}


# --- the whole thing ---------------------------------------------------------------


def run(only: "Optional[list[str]]" = None, *, after: float = 0, force: bool = False,
        dry_run: bool = False, fire: bool = False, caller: "Optional[str]" = None,
        echo: "Optional[Callable[[str], None]]" = None,
        detach_own: bool = False) -> dict:
    """Restart `only` (default all), now or `after` seconds from now.

    Returns {"rc", "lines", "selected", "busy", "unknown", "scheduled",
    "own", "after"}, with `rc` the exit code and `lines` what was said.
    `caller` defaults to `MEDIA_SESSIOND_SESSION`. `fire` is the detached run:
    it waits out a busy host instead of refusing, and treats no session as its
    own.

    `detach_own` is for a caller that is itself a service (the server's
    `POST /services/restart`): when the selection includes the service this
    process runs inside (`own_services`), a restart "now" becomes one
    `SELF_AFTER_S` from now, detached, so this process answers before it is
    killed. `own` names those services, `after` is the delay used."""
    lines: list[str] = []

    def emit(line: str) -> None:
        lines.append(line)
        if echo:
            echo(line)

    res = {"rc": OK, "lines": lines, "selected": [], "busy": [], "unknown": [],
           "scheduled": None, "own": [], "after": after}
    if fire:
        caller = ""
        delay = _env_f("MEDIA_RESTART_DELAY_S", 0.0)
        if delay > 0:
            _sleep(delay)
    elif caller is None:
        caller = os.environ.get("MEDIA_SESSIOND_SESSION", "")
    svcs = services()
    if not svcs:
        emit("no services owned by this checkout")
        return res
    selected, unknown = resolve(only, svcs)
    res["selected"] = [s.name for s in selected]
    if unknown:
        known = sorted({s.short for s in svcs})
        emit(f"unknown service(s): {', '.join(unknown)}. Known: {', '.join(known)}")
        res.update(rc=UNKNOWN, unknown=unknown)
        return res
    if detach_own and not fire:
        own = own_services(selected)
        res["own"] = [s.name for s in own]
        if own and after <= 0:
            after = SELF_AFTER_S
            emit(f"{' '.join(res['own'])} answers this request: restarting in "
                 f"{int(after)} s, detached, so the answer gets out first")
    res["after"] = after
    now = after <= 0 and not fire

    if dry_run:
        for s in selected:
            emit(f"{s.name}: {'parked, left alone' if s.state == 'parked' else f'would restart ({s.kind}, currently {s.state})'}")
        reasons = [] if force else busy(selected, caller or "", now=now)
        res["busy"] = reasons
        for r in reasons:
            emit(f"busy: {r}")
        if force:
            emit("busy checks: skipped (--force)")
        elif not reasons:
            emit("busy checks: clear")
        if after > 0:
            how = ("systemd-run --user --on-active"
                   if any(s.kind == "systemd" for s in selected) and shutil.which("systemd-run")
                   else "a detached child")
            emit(f"would schedule in {int(round(after))} s via {how}")
        res["rc"] = BUSY if reasons else OK
        return res

    if not fire:
        reasons = [] if force else busy(selected, caller or "", now=now)
        if reasons:
            for r in reasons:
                emit(f"refused, busy: {r}")
            emit("nothing restarted (--force restarts anyway)")
            res.update(rc=BUSY, busy=reasons)
            return res
        if after > 0:
            try:
                sch = schedule(selected, after, force)
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                emit(f"could not schedule the restart: {e}")
                res["rc"] = FAILED
                return res
            res["scheduled"] = sch
            emit(f"scheduled: {' '.join(s.name for s in selected)} in "
                 f"{int(round(after))} s ({sch['how']} {sch['unit']}; log: {sch['log']})")
            return res
        res["rc"] = restart(selected, emit)
        return res

    # The detached run: wait out a busy host, then act, or give up loudly.
    if not force:
        waited = 0.0
        while True:
            reasons = busy(selected, "", now=False)
            if not reasons:
                break
            if waited >= wait_max_s():
                for r in reasons:
                    emit(f"gave up, still busy: {r}")
                names = " ".join(s.name for s in selected)
                alert("warn", f"restart of {names} on {host()} gave up: still busy "
                      f"after {waited / 60:.0f} min", "\n".join(reasons),
                      f"media restart-services --only {names} --after 10")
                res.update(rc=BUSY, busy=reasons)
                return res
            emit(f"busy, retrying in {retry_s():.0f} s: {'; '.join(reasons)}")
            _sleep(retry_s())
            waited += retry_s()
    res["rc"] = restart(selected, emit)
    return res


def run_remote(name: str, only: "Optional[list[str]]" = None, *, after: float = 0,
               force: bool = False, dry_run: bool = False,
               echo: "Optional[Callable[[str], None]]" = None) -> dict:
    """`run()` on another host, through its server's `POST /services/restart`
    as a paired device of it (peer_link.py). No ssh.

    Returns the remote run's dict (its `rc` the exit code), or one with
    `rc` FAILED and a line saying why there is no answer: not paired, not
    reachable, refused, or a server too old to have the route."""
    from . import peer_link

    def say(line: str) -> None:
        if echo:
            echo(line)

    body: dict = {"force": force, "dry_run": dry_run}
    if only:
        body["only"] = list(only)
    if after > 0:
        body["after"] = after
    fail = {"rc": FAILED, "lines": [], "selected": [], "busy": [], "unknown": [],
            "scheduled": None, "own": [], "after": after}
    try:
        status, ans, link = peer_link.request(name, "/services/restart", body,
                                              timeout=180.0)
    except peer_link.PeerError as e:
        fail["lines"].append(str(e))
        say(str(e))
        return fail
    where = f"{ans.get('host') or link['alias']} ({link['url']})"
    if "rc" not in ans:
        if status in (401, 403):
            why = (f"{where} refused this host's token ({status}: {ans.get('error') or ''}); "
                   f"pair again: `media peer pair {link['alias']} '<link>'`")
        elif status == 404:
            why = (f"{where} has no /services/restart ({status}): it runs older code. "
                   "Pull it first (`media doctor --fix`)")
        else:
            why = f"{where} answered {status}: {ans.get('error') or ans}"
        fail["lines"].append(why)
        say(why)
        return fail
    say(f"on {where}:")
    for line in ans.get("lines") or []:
        say(f"  {line}")
    return {**fail, **ans, "rc": int(ans.get("rc", FAILED))}


def run_device(name: str, only: "Optional[list[str]]" = None, *, after: float = 0,
               force: bool = False, dry_run: bool = False,
               echo: "Optional[Callable[[str], None]]" = None) -> "Optional[dict]":
    """`run()` on a device that dials out to this host's server — the phone's
    Termux worker (server phone_jobs.py, contract §6.21a) — rather than a
    server this host dials in to. Nothing has to reach the device: the
    restart goes down the stream its worker holds open, as a job of its own
    kind, and the device's run() dict comes back the same way.

    Asks this host's own canvas (`POST /jobs/restart`, the host's token,
    `MEDIA_PHONE_JOBS_URL` / `MEDIA_VISUAL_URL`, as phone_run does). None when
    NAME is not this server's device, or there is no server here to ask (or
    one too old for the route): the caller then tries NAME as a peer. Else
    the device's dict, or one with `rc` FAILED saying why there is none
    (offline, no answer in time, a refused token)."""
    from . import phone_run

    body: dict = {"device": name, "force": force, "dry_run": dry_run}
    if only:
        body["only"] = list(only)
    if after > 0:
        body["after"] = after
    req = urllib.request.Request(
        phone_run._base() + "/jobs/restart", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json",
                                "X-Auth-Token": phone_run._token()})
    try:
        with urllib.request.urlopen(req, timeout=DEVICE_TIMEOUT_S) as r:
            status, ans = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            ans = json.loads(e.read() or b"{}")
        except ValueError:
            ans = {}
    except (OSError, ValueError):
        return None
    if not isinstance(ans, dict) or status in (401, 403, 404):
        # Not a device of ours (`not_a_device`), a server without the route,
        # or one that does not take this host's token (not this host's own
        # server): nothing here to ask, so NAME is tried as a peer.
        return None
    fail = {"rc": FAILED, "lines": [], "selected": [], "busy": [], "unknown": [],
            "scheduled": None, "own": [], "after": after}
    if "rc" not in ans:
        why = (f"this host's server refused the restart ({status}: "
               f"{ans.get('error') or ans})")
        if echo:
            echo(why)
        return {**fail, "lines": [why]}
    where = ans.get("device") or name
    if status == 200 and echo:
        echo(f"on {where} (down its own connection to this server):")
    for line in ans.get("lines") or []:
        if echo:
            echo(f"  {line}" if status == 200 else line)
    return {**fail, **ans, "rc": int(ans.get("rc", FAILED))}


#: How long `run_device` waits on this host's server, which waits on the phone
#: (phone_jobs.RESTART_TIMEOUT_S, 60 s, and its own margin).
DEVICE_TIMEOUT_S = 80.0


def main(argv: "Optional[list[str]]" = None) -> int:
    """The detached run's entry point (`python -m agent_media_core.restart_services
    --fire --only …`); people use `media restart-services`.

    `--json` is the phone's jobs worker asking (phone-jobs.py, a restart job
    from the server it dials out to): run() with `--after`/`--dry-run`/
    `--force` as given, the caller no session (`caller=""`) and `detach_own`
    (the worker may be among the restarted services), and its dict printed
    as one line of JSON on stdout, the lines on stderr as they come."""
    import argparse

    ap = argparse.ArgumentParser(prog="restart_services")
    ap.add_argument("--only", nargs="+", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--fire", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--after", type=float, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.json:
        res = run(a.only, after=max(0.0, a.after), force=a.force, dry_run=a.dry_run,
                  caller="", detach_own=True,
                  echo=lambda line: print(line, file=sys.stderr, flush=True))
        print(json.dumps({**res, "host": host()}), flush=True)
        return int(res["rc"])
    print(f"restart-services: detached run for {' '.join(a.only or ['all'])}", flush=True)
    res = run(a.only, force=a.force, fire=a.fire,
              echo=lambda line: print(line, flush=True))
    return int(res["rc"])


if __name__ == "__main__":
    sys.exit(main())
