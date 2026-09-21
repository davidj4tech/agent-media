"""How much memory each live session is holding, and how much the host has left.

For `/sessions/state` (server-contract.md §6.1): a desk with ten Claude Code
sessions open was measured at ~2.8 GB of red5's 7.7 GB with ~1.4 GB
available, and the phone is where you would notice that and close a few.

A session's memory is its agent process **and every descendant** — Claude
Code's MCP servers, a Codex sandbox, a pi extension host — because that is
what closing the session gives back. Resident set sizes are summed, so pages
shared between processes count once per process: an overestimate of what
closing would free, and the same measure `ps` and `top` show.

One pass over `/proc` answers every session at once: read each process's
parent from `stat`, build the children map, then read `statm` only for the
processes inside some session's tree. `/sessions/state` runs this inside its
3 s cached sweep, never per session and never per request.

`PROC` is the root read, so a test can point it at a fake tree.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The procfs root. Tests replace it with a directory of fake entries.
PROC = Path("/proc")

_MB = 1024 * 1024


def _page_size() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 4096


def _ppid(pid: str) -> int | None:
    """Parent pid from `/proc/<pid>/stat`. The command name is in parentheses
    and may itself hold spaces or ")", so fields are counted from the last ")"."""
    try:
        raw = (PROC / pid / "stat").read_bytes()
    except OSError:
        return None
    rest = raw[raw.rfind(b")") + 1:].split()
    try:
        return int(rest[1])            # state, then ppid
    except (IndexError, ValueError):
        return None


def _rss_bytes(pid: int, page: int) -> int | None:
    """Resident bytes from `/proc/<pid>/statm` (second field, in pages)."""
    try:
        fields = (PROC / str(pid) / "statm").read_text().split()
        return int(fields[1]) * page
    except (OSError, IndexError, ValueError):
        return None


def tree_mem_mb(roots: dict[str, int | None]) -> dict[str, int | None]:
    """`{session: MB}` — resident memory of each root pid's process tree.

    `roots` maps a session to its agent's pid. A session whose pid is
    unknown, or not in `/proc` any more (it ended between the sweep and
    this), answers None; so does one whose own RSS cannot be read. A child
    that exits mid-read is simply not counted.
    """
    children: dict[int, list[int]] = {}
    present: set[int] = set()
    try:
        entries = [e for e in os.listdir(PROC) if e.isdigit()]
    except OSError:
        return {sid: None for sid in roots}
    for name in entries:
        ppid = _ppid(name)
        if ppid is None:
            continue
        pid = int(name)
        present.add(pid)
        children.setdefault(ppid, []).append(pid)
    page = _page_size()
    out: dict[str, int | None] = {}
    for sid, root in roots.items():
        if not root or root not in present:
            out[sid] = None
            continue
        own = _rss_bytes(root, page)
        if own is None:
            out[sid] = None
            continue
        total, stack, seen = own, list(children.get(root, [])), {root}
        while stack:
            pid = stack.pop()
            if pid in seen:            # a pid reused mid-walk cannot loop us
                continue
            seen.add(pid)
            total += _rss_bytes(pid, page) or 0
            stack.extend(children.get(pid, []))
        out[sid] = round(total / _MB)
    return out


def host_mem() -> dict[str, int | None]:
    """`{"mem_total_mb", "mem_available_mb"}` from `/proc/meminfo`; None for a
    value that is missing or unreadable (MemAvailable is absent before 3.14)."""
    found: dict[str, int] = {}
    try:
        for line in (PROC / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                parts = rest.split()
                if parts and parts[0].isdigit():
                    kb = int(parts[0])
                    # Always kB in practice; honour the unit if it ever isn't.
                    unit = parts[1].lower() if len(parts) > 1 else "kb"
                    found[key] = kb * 1024 if unit == "kb" else kb
    except OSError:
        pass

    def mb(key: str) -> int | None:
        return round(found[key] / _MB) if key in found else None

    return {"mem_total_mb": mb("MemTotal"), "mem_available_mb": mb("MemAvailable")}
