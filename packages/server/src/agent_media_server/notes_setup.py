"""Setting up notes on a host, from the app's Notes tab.

What `notes.py` stands on, as a checklist the phone can read and fix:

  org      the tree itself — cloned from MEDIA_NOTES_REPO, or started fresh
           with the notes profile's files (paragtd's, when it is installed)
  sync     org-autosync's timer, which commits and pushes the tree (the
           script and units come with the dotfiles `bin` package)
  memory   the memory store search asks and captures are written to — a
           check only; it is run on the hub, not installed from a phone
  paragtd  optional: the Emacs package and the astro-alert generator

  GET  /notes/setup → {"components": [{name, label, state, detail, why,
                       actions, optional}]}
  POST /notes/setup {"component", "action"} → {"component", "action", "done"}
                    or, for a long one, {"pane", "cmd"}

A long action (a clone, an install) runs in a background tmux window the way
the harness installs do (`harnesses._window`), and is registered with them,
so the app watches it with the same `/harnesses/screen`, `/keys`, `/close`.
Quick ones (creating the files, enabling a timer) are done in place.

Gated like the harness installs (`auth.may_control_speech`): this runs
commands on the host.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from . import auth, harnesses, notes

PARAGTD_REPO_DEFAULT = "https://github.com/davidj4tech/paragtd.git"

def _notes_repo() -> str:
    return os.environ.get("MEDIA_NOTES_REPO", "").strip()


def _paragtd_dir() -> Path:
    return Path(os.environ.get("MEDIA_PARAGTD_DIR") or "~/projects/paragtd").expanduser()


def _systemctl(*args: str) -> tuple[int, str]:
    if not shutil.which("systemctl"):
        return 127, "no systemctl on this host"
    try:
        p = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(notes.root()), *args], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


# --- the checks -----------------------------------------------------------------

def _org() -> dict:
    root = notes.root()
    prof = notes.profile()
    row = {"name": "org", "label": "Notes folder", "optional": False,
           "detail": str(root), "why": None, "actions": []}
    missing = [f for f in prof.skeleton if not (root / f).is_file()]
    if root.is_dir() and (root / prof.capture_file).is_file():
        row["state"] = "ok"
        if missing:
            whose = f"{prof.name}'s" if prof.name else "the starting"
            row["why"] = f"{len(missing)} of {whose} files are not here yet"
            row["actions"].append("create")
        return row
    row["state"] = "missing"
    if not root.exists() or (root.is_dir() and not any(root.iterdir())):
        if _notes_repo():
            row["actions"].append("clone")
        row["actions"].append("create")
        row["why"] = ("clone your notes, or start a fresh set" if _notes_repo()
                      else "no notes here; start a fresh set (or set MEDIA_NOTES_REPO to clone yours)")
    else:
        row["actions"].append("create")
        row["why"] = f"the folder is here but has no {prof.capture_file}"
    return row


def _sync() -> dict:
    row = {"name": "sync", "label": "Sync (org-autosync)", "optional": False,
           "detail": "commits and pushes the notes every 5 minutes",
           "why": None, "actions": []}
    root = notes.root()
    if not (root / ".git").is_dir():
        row.update(state="off", why="the notes folder is not a git repository, so there is nothing to sync")
        return row
    if not _git("remote"):
        row.update(state="off", why="the notes repository has no remote to push to")
        return row
    code, out = _systemctl("is-enabled", "org-autosync.timer")
    if code == 127:
        row.update(state="off", why=out)
    elif out.startswith("enabled"):
        row["state"] = "ok"
    elif "not-found" in out or "No such file" in out or code == 1 and not out:
        row.update(state="missing",
                   why="org-autosync is not installed; it comes with the dotfiles bin package")
    else:
        row.update(state="off", why="the timer is installed but not running")
        row["actions"].append("enable")
    return row


def _memory() -> dict:
    row = {"name": "memory", "label": "Memory store", "optional": True,
           "detail": "search reads it, captures are remembered in it",
           "why": None, "actions": []}
    got = notes._memory_call("GET", "/health", timeout=3.0)
    if got and got.get("status") == "ok":
        row["state"] = "ok"
    else:
        row.update(state="down", why="the memory store did not answer; notes work without it")
    return row


def _paragtd() -> dict:
    d = _paragtd_dir()
    row = {"name": "paragtd", "label": "paragtd", "optional": True,
           "detail": "the Emacs package and astro alerts", "why": None, "actions": []}
    if (d / "lisp" / "paragtd.el").is_file():
        row["state"] = "ok"
        row["actions"].append("update")
    else:
        row.update(state="missing", why="only needed for Emacs, or for astro alerts")
        row["actions"].append("install")
    return row


def status(bearer: str) -> tuple[bool, dict]:
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    rows = [_org(), _sync(), _memory(), _paragtd()]
    return True, {"components": rows,
                  "search": "ripgrep" if shutil.which("rg") else "built-in"}


# --- the doing -------------------------------------------------------------------

def _create() -> dict:
    """The profile's files and roam folders, where they are missing. Never
    overwrites a file that is there."""
    root = notes.root()
    prof = notes.profile()
    made = []
    root.mkdir(parents=True, exist_ok=True)
    for name, head in {prof.capture_file: "", **prof.skeleton}.items():
        p = root / name
        if not p.exists():
            p.write_text(head)
            made.append(name)
    for d in prof.roam_dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists() and shutil.which("git"):
        subprocess.run(["git", "init", "-q", str(root)], capture_output=True, timeout=15)
        made.append(".git")
    return {"created": made}


def _windowed(component: str, action: str, argv: list[str]) -> tuple[bool, dict]:
    pane, err = harnesses._window(argv, f"notes-{component}")
    if err:
        return False, {"error": err, "status": 503}
    cmd = shlex.join(argv)
    harnesses._remember(pane, f"notes-{component}", action, cmd)
    return True, {"component": component, "action": action, "pane": pane, "cmd": cmd}


def run(component: str, action: str, bearer: str) -> tuple[bool, dict]:
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    rows = {r["name"]: r for r in (_org(), _sync(), _paragtd())}
    row = rows.get(component)
    if not row:
        return False, {"error": f"nothing to set up called {component!r}", "status": 400}
    if action not in row["actions"]:
        return False, {"error": f"{component} cannot {action!r} here"
                       + (f" ({row['why']})" if row.get("why") else ""), "status": 409}
    if (component, action) == ("org", "create"):
        try:
            return True, {"component": component, "action": action, "done": True,
                          **_create()}
        except OSError as e:
            return False, {"error": f"could not create the notes ({e})", "status": 500}
    if (component, action) == ("org", "clone"):
        return _windowed(component, action,
                         ["git", "clone", _notes_repo(), str(notes.root())])
    if (component, action) == ("sync", "enable"):
        code, out = _systemctl("enable", "--now", "org-autosync.timer")
        if code:
            return False, {"error": out or "systemctl failed", "status": 500}
        return True, {"component": component, "action": action, "done": True}
    d = str(_paragtd_dir())
    if action == "install":
        repo = os.environ.get("MEDIA_PARAGTD_REPO") or PARAGTD_REPO_DEFAULT
        script = (f"git clone {shlex.quote(repo)} {shlex.quote(d)} && "
                  f"cd {shlex.quote(d)} && bin/bootstrap")
    else:
        script = f"cd {shlex.quote(d)} && git pull --ff-only && bin/bootstrap"
    return _windowed(component, action, ["sh", "-c", script])
