"""The paragtd layout for agent-media's Notes.

paragtd (https://github.com/davidj4tech/paragtd) is a PARA/GTD method for
Org: the GTD files at the top of `~/org` (inbox, next actions, waiting for,
tickler, …), org-roam notes under `roam/`, and astro alerts generated into
`astro.org`. This profile tells the Notes server that layout: which files are
the views and what they are called, where each refile target puts a heading,
and what a fresh tree starts with.

The lists mirror paragtd's `paragtd-paths.el` and `paragtd-capture.el` by
hand. A manifest paragtd writes will replace them
(docs/proposals/2026-09-24-notes-core-and-paragtd.md, step 4).
"""

from __future__ import annotations

from pathlib import Path

from agent_media_server.notes_profile import Profile

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


class Paragtd(Profile):
    name = "paragtd"
    capture_file = "inbox.org"
    #: The agents' notes, the generated astro alerts, and backups.
    search_excludes = ("roam/sessions/**", "astro.org*", "*.bak*")
    skeleton = SKELETON
    roam_dirs = ("roam/projects", "roam/people", "roam/refs", "roam/notes",
                 "roam/journal")
    #: paragtd-todo-keywords, plus SOMEDAY (the Organiser's someday list) and
    #: the American CANCELED.
    keywords = (("TODO", "NEXT", "WAITING", "SOMEDAY"), ("DONE", "CANCELLED", "CANCELED"))

    def detect(self, root: Path) -> bool:
        return (root / "next-actions.org").is_file()

    def files(self, root: Path):
        return GTD_FILES

    def agenda_files(self, root: Path):
        return tuple(f for _, _, f in GTD_FILES) + ("astro.org",)

    def agenda_keep(self, fname: str, days_ago: int) -> bool:
        # Astro alerts are point-in-time and never marked DONE, so past ones
        # age out, as in paragtd-astro-skip-stale.
        return fname != "astro.org" or days_ago <= ASTRO_STALE_DAYS

    def roam_folders(self, root: Path):
        return ROAM_FOLDERS

    def refile_targets(self, root: Path):
        return REFILE_TARGETS


profile = Paragtd()
