"""Notes profiles: how a method lays the Org tree out.

Core (`org.py`, `org_edit.py`, `org_setup.py`) reads and edits plain
Org. What a method adds on top — which files are the views and what they are
called, where a capture lands, the places a heading can be moved to, what a
fresh tree starts with — comes from a profile. `Profile` below is the one for
plain Org, and what every hook falls back to.

A method ships its profile as a package, registered under an entry point
(the render engines' arrangement, `agent_media_core.extensions`):

    [project.entry-points."agent_media.org_profiles"]
    paragtd = "agent_media_org_paragtd:profile"

Which one is used: MEDIA_ORG_PROFILE, else `[org] profile` in config.toml,
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

ENTRY_GROUP = "agent_media.org_profiles"

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
    #: The TODO keywords of a file that declares none (`#+TODO:`) when
    #: config.toml names none either: (open, done), Org's own default.
    keywords: tuple[tuple[str, ...], tuple[str, ...]] = (("TODO",), ("DONE",))

    def detect(self, root: Path) -> bool:
        """Is `root` laid out this profile's way? Only asked when no profile
        is named."""
        return False

    def files(self, root: Path) -> tuple[tuple[str, str, str], ...]:
        """(view name, label, path under the root) for each file view, in
        order: the configured agenda files, else every top-level `.org`."""
        found = configured_files(root)
        if found is None:
            try:
                found = sorted(p for p in root.glob("*.org") if p.is_file())
            except OSError:
                return ()
        return tuple((_view_name(root, p), _title(p), p.relative_to(root).as_posix())
                     for p in found)

    def agenda_files(self, root: Path) -> tuple[str, ...]:
        """The files the agenda is read from."""
        return tuple(f for _, _, f in self.files(root))

    def todo_keywords(self, root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The keywords of a file that declares none: `keywords`, unless the
        profile reads them from the tree."""
        return self.keywords

    def agenda_keep(self, root: Path, fname: str, days_ago: int) -> bool:
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

    def capture_kinds(self, root: Path) -> list[dict]:
        """Capture templates beyond the plain to-do and note:
        `{key, label, target, file, headline?, tree_type?, prepend?,
        template}`, as org-capture-templates has them (org_capture.py
        fills them). Plain Org has none."""
        return []

    def setup_rows(self, root: Path) -> list[dict]:
        """Extra rows for the setup checklist (org_setup.py's shape)."""
        return []

    def setup_run(self, root: Path, component: str, action: str) -> dict | None:
        """Do `action` on one of `setup_rows`' components: what the answer
        adds, or None when it is not one of them. Raise RuntimeError to
        refuse or report a failure."""
        return None

    def enforces_dependencies(self) -> bool:
        """Whether a heading may not close while a child, or under an
        `:ORDERED:` parent an earlier sibling, is open — Org's
        `org-enforce-todo-dependencies`. Plain Org: `[org]
        enforce_todo_dependencies` in config.toml, off by default as in Org."""
        return _org_config().get("enforce_todo_dependencies") is True

    def after_state(self, lines: list[str], i: int, old: str, new: str, kw, now) -> dict | None:
        """Further edits to the same file once the heading on line index `i`
        has gone from `old` to `new` (a repeater that moved on instead is not
        a change). Made in place, under the file's lock; what it returns is
        added to /org/state's answer."""
        return None

    def editable(self, root: Path, rel: str) -> bool:
        """Whether a file (its path under the root) may be changed from the
        app: the capture file and the file views."""
        return rel == self.capture_file or any(f == rel for _, _, f in self.files(root))


def _view_name(root: Path, p: Path) -> str:
    return p.relative_to(root).with_suffix("").as_posix().replace("/", "-")


def _org_config() -> dict:
    """config.toml's `[org]` table — over `[org]`, its name until 26 Sep
    2026, whose keys still count where `[org]` does not set them."""
    try:
        from agent_media_core import config
        loaded = config.load()
    except Exception:  # noqa: BLE001 — a bad config file means "nothing set"
        return {}
    out: dict = {}
    for name in ("notes", "org"):
        got = loaded.get(name)
        if isinstance(got, dict):
            out.update(got)
    return out


def env(name: str) -> str:
    """`MEDIA_ORG_<name>`, else `MEDIA_NOTES_<name>` (the name until 26 Sep
    2026, still honoured)."""
    return (os.environ.get(f"MEDIA_ORG_{name}")
            or os.environ.get(f"MEDIA_NOTES_{name}") or "").strip()


def configured_files(root: Path) -> list[Path] | None:
    """The agenda files config names — `[org] agenda_files` in
    config.toml, else MEDIA_AGENDA_FILES (colon-separated, what `media
    agenda` reads) — as `.org` files under the root, a directory meaning
    the `.org` files in it, as in Org. None when neither is set. A file
    outside the notes root is left out: the app can only reach the tree."""
    raw = _org_config().get("agenda_files")
    if isinstance(raw, str):
        raw = [raw]
    if not raw:
        env = os.environ.get("MEDIA_AGENDA_FILES", "")
        raw = [x for x in env.split(":") if x.strip()]
    if not raw:
        return None
    base = root.resolve()
    out: list[Path] = []
    for entry in raw:
        p = Path(str(entry)).expanduser()
        p = p if p.is_absolute() else root / p
        try:
            found = sorted(p.glob("*.org")) if p.is_dir() else [p]
        except OSError:
            continue
        for f in found:
            try:
                f.resolve().relative_to(base)
            except (OSError, ValueError):
                log.info("notes: %s is outside %s; left out", f, root)
                continue
            if f.suffix == ".org" and f.is_file() and f not in out:
                out.append(root / f.resolve().relative_to(base))
    return out


def configured_keywords() -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """`[org] todo_keywords` in config.toml, Org's way: `["TODO", "NEXT",
    "|", "DONE"]` (no bar: the last one is done). None when unset."""
    raw = _org_config().get("todo_keywords")
    if not isinstance(raw, list) or not raw:
        return None
    return split_keywords([str(x) for x in raw])


def split_keywords(words: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """One `#+TODO:` sequence as (open, done). Fast-access keys and logging
    marks (`WAITING(w@/!)`) are dropped; with no `|` the last word is the
    done state."""
    words = [w.split("(", 1)[0] for w in words if w.split("(", 1)[0]]
    if "|" in words:
        i = words.index("|")
        return tuple(words[:i]), tuple(w for w in words[i + 1:] if w != "|")
    if len(words) < 2:
        return tuple(words), ()
    return tuple(words[:-1]), (words[-1],)


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
    got = env("PROFILE")
    if got:
        return got
    return str(_org_config().get("profile") or "").strip()


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
