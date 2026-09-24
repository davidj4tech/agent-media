"""The paragtd layout for agent-media's Notes.

paragtd (https://github.com/davidj4tech/paragtd) is a PARA/GTD method for
Org: the GTD files at the top of `~/org` (inbox, next actions, waiting for,
tickler, …), org-roam notes under `roam/`, and astro alerts generated into
`astro.org` (its own agenda view in Emacs, not the agenda), the new and full
moon routines among them into `lunar.org` (in the agenda). This profile tells the Notes server that layout: which files are
the views and what they are called, where each refile target puts a heading,
what a fresh tree starts with, and how a sequenced project moves on when a
step is closed (sequence.py).

paragtd describes its setup in `.paragtd.json` at the top of the tree
(paragtd-export.el, written by Emacs at startup and synced with the tree):
the core files, the TODO keywords, the astro settings, as that Emacs has
them. It is read when it is there. The lists below are paragtd's defaults,
for a tree without one (docs/proposals/2026-09-24-notes-core-and-paragtd.md).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from agent_media_server.notes_profile import Profile, _title, split_keywords

from . import sequence

#: paragtd-core-files: (view name, label, file).
GTD_FILES = (
    ("inbox", "Inbox", "inbox.org"),
    ("next", "Next actions", "next-actions.org"),
    ("waiting", "Waiting for", "waiting-for.org"),
    ("tickler", "Tickler", "tickler.org"),
    ("someday", "Someday", "someday.org"),
    ("projects", "Projects", "projects.org"),
    ("areas", "Areas", "areas.org"),
    ("routines", "Routines", "routines.org"),
)

#: org-roam folders, as shelves of notes. Sessions are the agents' notes —
#: hundreds of them — so they are a view of their own and left out of search
#: unless asked for.
ROAM_FOLDERS = (
    ("roam-projects", "Project notes", "roam/projects"),
    ("roam-people", "People", "roam/people"),
    ("roam-refs", "References", "roam/refs"),
    ("roam-notes", "Notes", "roam/notes"),
    ("roam-journal", "Journal", "roam/journal"),
    ("roam-sessions", "Agent sessions", "roam/sessions"),
)

#: `to` → (file, the headline it goes under or None for top level, state or
#: None, needs a date), as paragtd's capture templates file them.
REFILE_TARGETS = {
    "next": ("next-actions.org", "Inbox", "NEXT", False),
    "waiting": ("waiting-for.org", "Waiting", "WAITING", False),
    "tickler": ("tickler.org", "Tickler", None, True),
    "someday": ("someday.org", None, None, False),
    "projects": ("projects.org", None, None, False),
    "inbox": ("inbox.org", None, None, False),
}

#: paragtd-core-files, and the head each starts with.
SKELETON = {
    "inbox.org": "#+title: Inbox\n#+startup: overview\n#+filetags: :inbox:\n\n",
    "next-actions.org": "#+title: Next actions\n\n* Inbox\n",
    "waiting-for.org": "#+title: Waiting for\n\n* Waiting\n",
    "tickler.org": "#+title: Tickler\n\n* Tickler\n",
    "someday.org": "#+title: Someday\n\n",
    "areas.org": "#+title: Areas\n\n",
    "projects.org": "#+title: Projects\n\n",
    "journal.org": "#+title: Journal\n\n",
    "visioning.org": "#+title: Visioning\n\n",
    "routines.org": "#+title: Routines\n\n",
}

#: paragtd-astro-stale-days: a past astro alert older than this is not agenda.
ASTRO_STALE_DAYS = 2

#: paragtd-core-files: the agenda, when there is no manifest.
CORE_FILES = ("inbox.org", "next-actions.org", "waiting-for.org", "someday.org",
              "tickler.org", "areas.org", "journal.org", "projects.org",
              "visioning.org", "routines.org", "lunar.org")

#: Core files that are not a list to work through, so not a view: the
#: generated astro alerts and lunar routines, the journal's datetree, the
#: visioning notes.
NOT_VIEWS = frozenset({"astro.org", "lunar.org", "journal.org", "visioning.org"})

#: paragtd-todo-keywords: TODO NEXT WAITING | DONE CANCELLED.
KEYWORDS = (("TODO", "NEXT", "WAITING"), ("DONE", "CANCELLED"))

MANIFEST = ".paragtd.json"

_PROPS = ":PROPERTIES:\n:CREATED: %U\n:END:\n"
_STEP = ':PROPERTIES:\n:TRIGGER: next-sibling todo!(NEXT) scheduled!("++0d")\n:END:\n'

#: paragtd-capture-templates, for a tree without a manifest.
CAPTURE = [
    {"key": "t", "label": "Todo", "target": "file", "file": "inbox.org",
     "template": "* TODO %?\n" + _PROPS},
    {"key": "n", "label": "Next action", "target": "file+headline", "file": "next-actions.org",
     "headline": "Inbox", "template": "* NEXT %?\n" + _PROPS},
    {"key": "w", "label": "Waiting for", "target": "file+headline", "file": "waiting-for.org",
     "headline": "Waiting", "template": "* WAITING %?\n" + _PROPS},
    {"key": "k", "label": "Tickler / defer until", "target": "file+headline", "file": "tickler.org",
     "headline": "Tickler", "template": "* TODO %?\nSCHEDULED: %^T\n" + _PROPS},
    {"key": "p", "label": "Project (sequenced steps)", "target": "file", "file": "projects.org",
     "template": "* TODO %?\n:PROPERTIES:\n:CREATED: %U\n:ORDERED: t\n:END:\n"
                 "** NEXT First step\n" + _STEP + "** TODO Second step\n" + _STEP
                 + "** TODO Third step\n"},
    {"key": "j", "label": "Journal entry", "target": "file+olp+datetree", "file": "journal.org",
     "tree_type": "week", "template": "* %U %?\n"},
]

log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_CACHE: dict = {"key": None, "data": None}


def manifest(root: Path) -> dict:
    """`.paragtd.json`, or {} when there is none (or it will not parse).
    Read again only when it changes."""
    p = root / MANIFEST
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _LOCK:
        if _CACHE["key"] == key:
            return _CACHE["data"]
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        log.warning("paragtd: %s will not parse (%s); using the defaults", p, e)
        data = {}
    if not isinstance(data, dict) or data.get("version") != 1:
        data = {}
    with _LOCK:
        _CACHE.update(key=key, data=data)
    return data


class Paragtd(Profile):
    name = "paragtd"
    capture_file = "inbox.org"
    #: The agents' notes, the generated astro alerts, and backups.
    search_excludes = ("roam/sessions/**", "astro.org*", "*.bak*")
    skeleton = SKELETON
    roam_dirs = ("roam/projects", "roam/people", "roam/refs", "roam/notes",
                 "roam/journal")

    keywords = KEYWORDS

    def todo_keywords(self, root: Path):
        # What the Emacs that wrote the manifest calls its keywords (one
        # sequence or several), else paragtd-todo-keywords.
        seqs = manifest(root).get("todo_keywords")
        if isinstance(seqs, list) and seqs:
            opens: list[str] = []
            dones: list[str] = []
            for seq in seqs:
                if isinstance(seq, list):
                    o, d = split_keywords([str(w) for w in seq])
                    opens += [w for w in o if w not in opens]
                    dones += [w for w in d if w not in dones]
            if opens or dones:
                return tuple(opens), tuple(dones)
        return KEYWORDS

    def _core(self, root: Path) -> tuple[str, ...]:
        got = manifest(root).get("files")
        if isinstance(got, list) and got and all(isinstance(f, str) for f in got):
            return tuple(got)
        return CORE_FILES

    def detect(self, root: Path) -> bool:
        return (root / "next-actions.org").is_file() or (root / MANIFEST).is_file()

    def files(self, root: Path):
        # The Organiser's order for paragtd's own files (the tickler beside
        # waiting-for, as the capture keys have them), then any a site adds.
        core = self._core(root)
        out = [(name, label, f) for name, label, f in GTD_FILES if f in core]
        out += [(Path(f).stem, _title(root / f), f) for f in core
                if f not in NOT_VIEWS and "/" not in f and not any(f == g for _, _, g in GTD_FILES)]
        return tuple(out)

    def agenda_files(self, root: Path):
        return self._core(root)

    def agenda_keep(self, root: Path, fname: str, days_ago: int) -> bool:
        # Astro alerts are point-in-time and never marked DONE, so past ones
        # age out, as in paragtd-astro-skip-stale.
        stale = (manifest(root).get("astro") or {}).get("stale_days")
        return fname != "astro.org" or days_ago <= (stale if isinstance(stale, int) else ASTRO_STALE_DAYS)

    def roam_folders(self, root: Path):
        return ROAM_FOLDERS

    def refile_targets(self, root: Path):
        return REFILE_TARGETS

    def capture_kinds(self, root: Path) -> list[dict]:
        # The Emacs's own templates, site ones too, when it wrote them down.
        got = manifest(root).get("capture")
        if isinstance(got, list):
            return [c for c in got if isinstance(c, dict) and c.get("type", "entry") == "entry"
                    and isinstance(c.get("template"), str) and isinstance(c.get("file"), str)]
        return CAPTURE

    def setup_rows(self, root: Path) -> list[dict]:
        from . import astro

        return [astro.setup_row(root)]

    def setup_run(self, root: Path, component: str, action: str) -> dict | None:
        if component != "astro":
            return None
        from . import astro

        return astro.setup_run(root, action)

    def enforces_dependencies(self) -> bool:
        # paragtd-setup-sequence turns on org-enforce-todo-dependencies.
        return True

    def after_state(self, lines, i, old, new, kw, now):
        return sequence.after_state(lines, i, old, new, kw, now)


profile = Paragtd()
