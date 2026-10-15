"""How much agent-mail is waiting for each place (`/targets` `places[].mail`).

A place's mailbox follows agent-config's `agent-handoff` `mailbox_of`: in a
git repo, the repo's name, a worktree counting as its main checkout; outside
git, the folder's own name. Only **pending** mail counts — not yet claimed,
not acknowledged. The shared `sam` mailbox is never a place's.

The mailbox is agent-mail's SQLite file on this host (`AGENT_MAIL_DB`, else
`$XDG_STATE_HOME/agent-mail/mail.sqlite3`), read with one query, read-only,
and cached for `TTL_S`: `/targets` is polled. No file, no table, a locked
database: no counts, and the places go out without `mail`.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

#: Seconds one count of the whole mailbox is reused for.
TTL_S = 5.0
#: The old everyone-mailbox, before per-project ones: no place's.
SHARED = "sam"

_lock = threading.Lock()
_names: dict[str, str] = {}
_counts: tuple[float, dict[str, int] | None] = (0.0, None)


def db_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return Path(os.environ.get("AGENT_MAIL_DB") or state / "agent-mail/mail.sqlite3")


def mailbox_of(folder: str) -> str:
    """The mailbox a session in `folder` reads; cached per path."""
    name = _names.get(folder)
    if name is not None:
        return name
    try:
        common = subprocess.run(
            ["git", "-C", folder, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        common = ""
    if common:
        c = Path(common)
        name = (c.parent if c.name == ".git" else c).name.removesuffix(".git")
    else:
        name = Path(folder).resolve().name
    _names[folder] = name
    return name


def _read() -> dict[str, int] | None:
    path = db_path()
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute(
                "SELECT recipient, count(*) FROM messages"
                " WHERE acknowledged_at IS NULL AND claimed_session IS NULL"
                " GROUP BY recipient").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return {r: n for r, n in rows}


def pending() -> dict[str, int] | None:
    """`{mailbox: pending count}`, or None when the mailbox can't be read."""
    global _counts
    with _lock:
        at, counts = _counts
        if time.monotonic() - at < TTL_S:
            return counts
        counts = _read()
        _counts = (time.monotonic(), counts)
        return counts


def with_mail(places: list[dict]) -> list[dict]:
    """`places` with `mail` on each row — when the mailbox could be read."""
    counts = pending()
    if counts is None:
        return places
    for p in places:
        box = mailbox_of(p["path"])
        p["mail"] = 0 if box == SHARED else counts.get(box, 0)
    return places
