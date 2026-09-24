"""Notes profiles: how a method lays the Org tree out.

Core (`notes.py`, `notes_edit.py`, `notes_setup.py`) reads and edits plain
Org. What a method adds on top — which files are the views and what they are
called, where a capture lands, the places a heading can be moved to, what a
fresh tree starts with — comes from a profile. `Profile` below is the one for
plain Org, and what every hook falls back to.

A method ships its profile as a package, registered under an entry point
(the render engines' arrangement, `agent_media_core.extensions`):

    [project.entry-points."agent_media.notes_profiles"]
    paragtd = "agent_media_notes_paragtd:profile"

Which one is used: MEDIA_NOTES_PROFILE, else `[notes] profile` in config.toml,
naming one (`none` = plain Org); else the first installed profile whose
`detect(root)` says the tree is laid out its way; else plain Org.

docs/proposals/2026-09-24-notes-core-and-paragtd.md.
"""

from __future__ import annotations

import logging
import os
import re
from importlib.metadata import entry_points
from pathlib import Path

log = logging.getLogger(__name__)

ENTRY_GROUP = "agent_media.notes_profiles"

_TITLE = re.compile(r"^#\+title:\s*(.+)$", re.I | re.M)


def _title(p: Path) -> str:
    try:
        with p.open(errors="replace") as f:
            m = _TITLE.search(f.read(2048))
    except OSError:
        return p.stem
    return m.group(1).strip() if m else p.stem


class Profile:
    """Plain Org: every top-level `.org` file is a view, a capture goes to
    `inbox.org`, a heading moves to the top level of another file, and the
    roam shelves are whatever folders are under `roam/`."""

    name: str | None = None

    #: Where a capture is appended.
    capture_file = "inbox.org"
    #: ripgrep globs kept out of search unless everything is asked for.
    search_excludes: tuple[str, ...] = ("*.bak*",)
    #: What a fresh tree starts with: file → its head.
    skeleton: dict[str, str] = {"inbox.org": "#+title: Inbox\n\n"}
    #: Folders a fresh tree starts with.
    roam_dirs: tuple[str, ...] = ()

    def detect(self, root: Path) -> bool:
        """Is `root` laid out this profile's way? Only asked when no profile
        is named."""
        return False

    def files(self, root: Path) -> tuple[tuple[str, str, str], ...]:
        """(view name, label, file name) for each file view, in order."""
        try:
            found = sorted(p for p in root.glob("*.org") if p.is_file())
        except OSError:
            return ()
        return tuple((p.stem, _title(p), p.name) for p in found)

    def agenda_files(self, root: Path) -> tuple[str, ...]:
        """The files the agenda is read from."""
        return tuple(f for _, _, f in self.files(root))

    def agenda_keep(self, fname: str, days_ago: int) -> bool:
        """Whether an open, dated item from `fname`, `days_ago` days in the
        past (negative = ahead), belongs on the agenda."""
        return True

    def roam_folders(self, root: Path) -> tuple[tuple[str, str, str], ...]:
        """(view name, label, folder) for each shelf of notes."""
        base = root / "roam"
        try:
            dirs = sorted(d for d in base.iterdir()
                          if d.is_dir() and not d.name.startswith("."))
        except OSError:
            return ()
        return tuple((f"roam-{d.name}", d.name.replace("-", " ").capitalize(),
                      f"roam/{d.name}") for d in dirs)

    def refile_targets(self, root: Path) -> dict[str, tuple[str, str | None, str | None, bool]]:
        """`to` → (file, the headline it goes under or None for top level,
        the state it takes or None to keep its own, whether it needs a date —
        the heading is SCHEDULED on it)."""
        return {name: (fname, None, None, False) for name, _, fname in self.files(root)}

    def editable(self, root: Path, fname: str) -> bool:
        """Whether a top-level file may be changed from the app."""
        return fname == self.capture_file or any(
            f == fname for _, _, f in self.files(root))


PLAIN = Profile()

_loaded: dict[str, Profile] | None = None


def _installed() -> dict[str, Profile]:
    """`{name: profile}` for every installed one. A profile that fails to
    load is logged and skipped: notes still work, as plain Org."""
    global _loaded
    if _loaded is None:
        found: dict[str, Profile] = {}
        for ep in entry_points(group=ENTRY_GROUP):
            if ep.name in found or ep.name == "none":
                continue
            try:
                prof = ep.load()
            except Exception as e:  # noqa: BLE001 — one bad package must not break notes
                log.warning("notes profile %r failed to load: %s", ep.name, e)
                continue
            found[ep.name] = prof() if isinstance(prof, type) else prof
        _loaded = found
    return _loaded


def _named() -> str:
    got = os.environ.get("MEDIA_NOTES_PROFILE", "").strip()
    if got:
        return got
    try:
        from agent_media_core import config
        return str((config.load().get("notes") or {}).get("profile") or "").strip()
    except Exception:  # noqa: BLE001 — a bad config file means "not named"
        return ""


def active(root: Path) -> Profile:
    name = _named()
    if name == "none":
        return PLAIN
    installed = _installed()
    if name:
        if name in installed:
            return installed[name]
        log.warning("notes profile %r is not installed; using plain Org", name)
        return PLAIN
    for prof in installed.values():
        try:
            if prof.detect(root):
                return prof
        except OSError:
            continue
    return PLAIN


def _reset_for_tests() -> None:
    global _loaded
    _loaded = None
