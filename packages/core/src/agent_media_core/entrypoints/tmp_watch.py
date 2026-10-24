"""Watch /tmp for filling up, and say who is holding it when it does.

/tmp here is a 3.8 GB tmpfs. On 24 Oct 2026 it sat at 100% with its files
adding up to half of that: the rest was deleted files still held open, each
one 213 MB (the size of the `agy` binary), so `du` could not see them and
tests failed with "No space left on device". They were gone by the time
anyone looked. So, every run:

1. How full /tmp is. Under WARN_PCT it reports `ok`.
2. Over it, who holds what: every open file under /tmp, deleted ones
   flagged, grouped by process (pid, command line, the binary it runs), and
   /tmp's biggest top-level entries. That goes to `tmp-watch.log` in the
   state dir (appended, so the next look has it) and into the alert's detail.

`warn` from WARN_PCT, `needs` from NEEDS_PCT. It reports every run through
agent-alert (the alert store keeps the edges) and keeps no state of its own.

    media-tmp-watch [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

ALERT_ID = "tmp-full"
ROOT = "/tmp"
WARN_PCT = 90
NEEDS_PCT = 97
EVERY_S = 300
TOP = 6


def used_pct(root: str = ROOT) -> float:
    s = os.statvfs(root)
    total = s.f_blocks * s.f_frsize
    return 100.0 * (total - s.f_bavail * s.f_frsize) / total if total else 0.0


def level_for(pct: float) -> str:
    return "needs" if pct >= NEEDS_PCT else "warn" if pct >= WARN_PCT else "ok"


def holders(root: str = ROOT, proc: str = "/proc") -> list[dict]:
    """Open files under `root`, one row per (process, file), biggest first.
    A process of another user is skipped: its fds cannot be read."""
    rows = []
    for pid in (p for p in os.listdir(proc) if p.isdigit()):
        fd_dir = f"{proc}/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            deleted = target.endswith(" (deleted)")
            path = target.removesuffix(" (deleted)")
            if not (path == root or path.startswith(root + "/")):
                continue
            try:
                st = os.stat(f"{fd_dir}/{fd}")
            except OSError:
                continue
            size = st.st_size
            if not stat.S_ISREG(st.st_mode) or not size:
                continue   # a directory, a socket or an empty file holds nothing
            rows.append({"pid": int(pid), "path": path, "deleted": deleted,
                         "size": size})
    rows.sort(key=lambda r: r["size"], reverse=True)
    return rows


def _proc_text(pid: int) -> str:
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        cmd = "?"
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = "?"
    return f"pid {pid} runs {exe}: {cmd.strip()[:120]}"


def top_entries(root: str = ROOT, n: int = TOP) -> list[str]:
    try:
        out = subprocess.run(["du", "-sx", "--block-size=1M", *[e.path for e in os.scandir(root)]],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        size, _, path = line.partition("\t")
        if size.isdigit():
            rows.append((int(size), path))
    rows.sort(reverse=True)
    return [f"{mb} MB {path}" for mb, path in rows[:n]]


def describe(pct: float, rows: list[dict], entries: list[str]) -> str:
    lines = [f"{ROOT} is {pct:.0f}% full."]
    held = sum(r["size"] for r in rows if r["deleted"])
    if rows:
        lines.append(f"Deleted but still open: {held // 2**20} MB.")
    seen: set = set()
    for r in rows[:TOP * 2]:
        tag = "deleted" if r["deleted"] else "open"
        lines.append(f"{r['size'] // 2**20} MB {tag} {r['path']}")
        if r["pid"] not in seen:
            seen.add(r["pid"])
            lines.append("  " + _proc_text(r["pid"]))
    if entries:
        lines.append("Biggest entries:")
        lines += ["  " + e for e in entries]
    return "\n".join(lines)


def log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "agent-media" / "tmp-watch.log"


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="media-tmp-watch")
    ap.add_argument("--dry-run", action="store_true", help="print, report nothing")
    args = ap.parse_args(argv)

    pct = used_pct()
    level = level_for(pct)
    if level == "ok":
        title, detail = f"{ROOT} has room ({pct:.0f}% used)", ""
    else:
        detail = describe(pct, holders(), top_entries())
        title = f"{ROOT} is {pct:.0f}% full"
        if not args.dry_run:
            p = log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{detail}\n")
    if args.dry_run:
        print(level, title)
        print(detail)
        return 0
    cmd = [os.path.expanduser("~/.local/bin/agent-alert"), "report", ALERT_ID,
           "--level", level, "--title", title, "--every", str(EVERY_S)]
    if level != "ok":
        cmd += ["--detail", detail,
                "--fix", f"See who holds the space in {log_path()}; end the "
                         "process (not the files) that holds deleted ones."]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
