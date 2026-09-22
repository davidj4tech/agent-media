#!/usr/bin/env python3
"""Move old Meridian job records out of ~/.claude/jobs, daily.

Every request Meridian (the gateway) serves through Claude Code leaves a job
record in `~/.claude/jobs/<id>/` (state.json + timeline.jsonl), and `claude
agents --json` lists every one of them — 922 of 934 entries on 2026-09-22,
nearly all "blocked". They are records, not processes: no memory, but they
bury the real sessions in that list and grow without end.

So, once a day: a Meridian job (cwd `~/.meridian`, background, no live pid)
whose newest file is older than --days is MOVED, not deleted, to a dated
folder under ~/backups/claude-jobs-meridian/, and dated folders older than
--keep-days are deleted. Anything that is not Meridian's is never touched.

The selection is Claude Code's own (`claude agents --json --all`), not a guess
from the files: it is what knows which cwd a job belongs to.

  meridian-jobs-prune.py [--days 7] [--keep-days 30] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
JOBS = HOME / ".claude" / "jobs"
MERIDIAN = str((HOME / ".meridian").resolve())
BACKUPS = HOME / "backups" / "claude-jobs-meridian"


def newest_mtime(d: Path) -> float:
    times = [f.stat().st_mtime for f in d.iterdir() if f.is_file()]
    return max(times) if times else d.stat().st_mtime


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--keep-days", type=float, default=30.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    claude = shutil.which("claude") or str(HOME / ".local/share/fnm/aliases/default/bin/claude")
    try:
        out = subprocess.run([claude, "agents", "--json", "--all"], capture_output=True,
                             text=True, timeout=120, check=True, cwd=str(HOME)).stdout
        rows = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"meridian-jobs-prune: could not list jobs ({e}); nothing moved", file=sys.stderr)
        return 1

    cut = time.time() - a.days * 86400
    dest = BACKUPS / dt.date.today().isoformat()
    moved = kept = 0
    for r in rows:
        if r.get("kind") != "background" or r.get("pid"):
            continue
        if str(Path(r.get("cwd") or "/").resolve()) != MERIDIAN:
            continue
        d = JOBS / str(r.get("id") or "")
        if not r.get("id") or not d.is_dir():
            continue
        if newest_mtime(d) > cut:
            kept += 1
            continue
        moved += 1
        if not a.dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            os.chmod(BACKUPS, 0o700)
            shutil.move(str(d), str(dest / d.name))

    dropped = 0
    if BACKUPS.is_dir():
        old = time.time() - a.keep_days * 86400
        for b in BACKUPS.iterdir():
            if b.is_dir() and b.stat().st_mtime < old:
                dropped += 1
                if not a.dry_run:
                    shutil.rmtree(b)

    mode = "would move" if a.dry_run else "moved"
    print(f"meridian-jobs-prune: {mode} {moved}, kept {kept} newer than {a.days:g} d; "
          f"{'would drop' if a.dry_run else 'dropped'} {dropped} backup batch(es) "
          f"older than {a.keep_days:g} d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
