"""Keeping astro.org a year ahead, without Emacs.

paragtd generates its astro alerts a year at a time (`bin/paragtd-astro-
generate --year Y --append`), each year a set of `* <Y> …` headings in
`astro.org`. Nothing runs it on its own, so the alerts simply stop at the end
of the last year someone remembered. `ensure` adds this year and next when
either is missing; a monthly user timer runs it (`python -m
agent_media_notes_paragtd.astro`), and the Organiser's setup screen turns the
timer on (`setup_row`, `enable`).

The generator is paragtd's (GPL) and runs as its own process with its own
virtualenv; nothing of it is imported.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

UNIT = "paragtd-astro"

_YEAR = re.compile(r"^\* (\d{4}) ", re.M)


def _settings(root: Path) -> dict:
    from . import manifest

    got = manifest(root).get("astro") or {}
    paragtd = Path(os.environ.get("MEDIA_PARAGTD_DIR") or "~/projects/paragtd").expanduser()
    return {"generator": got.get("generator") or str(paragtd / "bin" / "paragtd-astro-generate"),
            "timezone": got.get("timezone") or "Australia/Melbourne"}


def years(root: Path) -> list[int]:
    """The years astro.org has alerts for."""
    try:
        text = (root / "astro.org").read_text(errors="replace")
    except OSError:
        return []
    return sorted({int(y) for y in _YEAR.findall(text)})


def ensure(root: Path, today: dt.date | None = None, run=subprocess.run) -> list[int]:
    """Generate this year and next into astro.org where missing, oldest
    first so the file stays in order. The years added."""
    today = today or dt.date.today()
    have = set(years(root))
    s = _settings(root)
    added = []
    for year in (today.year, today.year + 1):
        if year in have:
            continue
        out = root / "astro.org"
        cmd = [s["generator"], "--year", str(year), "--output", str(out),
               "--timezone", s["timezone"]] + (["--append"] if out.exists() else [])
        p = run(cmd, capture_output=True, text=True, timeout=600)
        if p.returncode:
            raise RuntimeError(f"the astro generator failed for {year}: "
                               f"{(p.stderr or p.stdout).strip()[-300:]}")
        added.append(year)
    return added


# --- the timer ---------------------------------------------------------------------

def _unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _systemctl(*args: str) -> tuple[int, str]:
    if not shutil.which("systemctl"):
        return 127, "no systemctl on this host"
    try:
        p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def setup_row(root: Path) -> dict:
    have = years(root)
    row = {"name": "astro", "label": "Astro alerts", "optional": True, "why": None,
           "actions": [],
           "detail": (f"astro.org has {have[0]}–{have[-1]}" if len(have) > 1
                      else f"astro.org has {have[0]}" if have else "no astro.org yet")
                     + "; kept a year ahead by a monthly timer"}
    if not Path(_settings(root)["generator"]).is_file():
        row.update(state="missing", why="paragtd's astro generator is not installed (see paragtd, above)")
        return row
    code, out = _systemctl("is-enabled", f"{UNIT}.timer")
    if code == 0 and out.startswith("enabled"):
        row["state"] = "ok"
        row["actions"].append("run")
    else:
        row.update(state="off", why="nothing adds next year's alerts when this year's run out")
        row["actions"] += ["enable", "run"]
    return row


def setup_run(root: Path, action: str) -> dict:
    if action == "run":
        return {"added": ensure(root)}
    if action != "enable":
        raise RuntimeError(f"astro cannot {action!r}")
    d = _unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{UNIT}.service").write_text(
        "[Unit]\nDescription=paragtd: keep astro.org a year ahead (agent-media notes-paragtd)\n\n"
        "[Service]\nType=oneshot\n"
        f"Environment=MEDIA_NOTES_DIR={root}\n"
        f"ExecStart={sys.executable} -m agent_media_notes_paragtd.astro\n")
    (d / f"{UNIT}.timer").write_text(
        "[Unit]\nDescription=paragtd: keep astro.org a year ahead, monthly\n\n"
        "[Timer]\nOnCalendar=monthly\nPersistent=true\nRandomizedDelaySec=1h\n\n"
        "[Install]\nWantedBy=timers.target\n")
    for args in (("daemon-reload",), ("enable", "--now", f"{UNIT}.timer")):
        code, out = _systemctl(*args)
        if code:
            raise RuntimeError(out or "systemctl failed")
    return {"added": ensure(root)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    root = Path(os.environ.get("MEDIA_NOTES_DIR") or "~/org").expanduser()
    try:
        added = ensure(root)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        log.error("paragtd astro: %s", e)
        return 1
    log.info("paragtd astro: %s", f"added {added}" if added else f"nothing to add ({years(root)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
