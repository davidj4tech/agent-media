"""Reviewing zettels from the app: keep a draft note, or throw it away.

Draft notes sit in the roam notes folder (`roam/notes/`) with the `inbox`
filetag — written by hand from Emacs's `z` capture template, or distilled from
agent sessions by `agent-sessions zk` (those also carry `:ZK_STATUS: draft`).
This does from the phone what paragtd-zettel-promote / -discard do in Emacs:

  POST /org/zettel {"path", "action": "keep" | "discard"}
      → {"path", "action", "title"}

- keep: `inbox` leaves the filetags, `:ZK_STATUS:` (when there is one) becomes
  `permanent`, and a Denote-style name loses its `_inbox` keyword
  (`…__zk_inbox.org` → `…__zk.org`); `path` is the new one.
- discard: the file is deleted. A distilled note is not written again (the
  exporter remembers what it wrote).

`GET /org/read` says `"draft": true` for such a note, and a folder view marks
its drafts the same way, so the app can offer the two keys.
"""

from __future__ import annotations

from agent_media_core import _lock as fcntl  # flock, or msvcrt on Windows
import re
from pathlib import Path

from . import auth
from .org import _rel, _safe_path, root

NOTES_DIR = "roam/notes"
INBOX_TAG = "inbox"
_FILETAGS = re.compile(r"^#\+filetags:[ \t]*(.*)$", re.M | re.I)
_STATUS = re.compile(r"^(:ZK_STATUS:[ \t]*)\S*", re.M)


def _in_notes(p: Path) -> bool:
    try:
        p.resolve().relative_to((root() / NOTES_DIR).resolve())
        return True
    except (OSError, ValueError):
        return False


def _tags(text: str) -> list[str]:
    m = _FILETAGS.search(text)
    return [t for t in (m.group(1).strip().split(":") if m else []) if t.strip()]


def is_draft(p: Path, head: str | None = None) -> bool:
    """A note in roam/notes still tagged `inbox`. `head`: the file's text, if read."""
    if not _in_notes(p):
        return False
    if head is None:
        try:
            with p.open(errors="replace") as f:
                head = f.read(2048)
        except OSError:
            return False
    return INBOX_TAG in _tags(head)


def kept_name(name: str) -> str:
    """The file name without its `inbox` keyword, as paragtd-zettel-promote renames."""
    name = re.sub(rf"__{INBOX_TAG}\.org$", ".org", name)
    return re.sub(rf"_{INBOX_TAG}(_|\.org$)", r"\1", name)


def promote_text(text: str) -> str:
    def retag(m: re.Match) -> str:
        tags = [t for t in _tags(m.group(0)) if t != INBOX_TAG]
        return "#+filetags: :" + ":".join(tags) + ":" if tags else "#+filetags:"

    text = _FILETAGS.sub(retag, text, count=1)
    return _STATUS.sub(lambda m: m.group(1) + "permanent", text, count=1)


def review(rel: str, action: str, bearer: str) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if action not in ("keep", "discard"):
        return False, {"error": f"action is keep or discard, not {action!r}", "status": 400}
    p = _safe_path(rel)
    if not p:
        return False, {"error": "no such note", "status": 404}
    if not _in_notes(p):
        return False, {"error": f"only notes in {NOTES_DIR}/ are reviewed here", "status": 400}
    try:
        with p.open("r+", errors="replace") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            text = f.read()
            if INBOX_TAG not in _tags(text):
                return False, {"error": "it is not a draft (already kept?)", "status": 409}
            title_m = re.search(r"^#\+title:[ \t]*(.*)$", text, re.M | re.I)
            title = title_m.group(1).strip() if title_m else p.stem
            discard = action == "discard"
            if not discard:
                f.seek(0)
                f.truncate()
                f.write(promote_text(text))
                f.flush()
        if discard:
            # Closed first: Windows will not delete a file that is open.
            p.unlink()
            return True, {"path": _rel(p), "action": action, "title": title}
        new = p.with_name(kept_name(p.name))
        if new != p and not new.exists():
            p.rename(new)
        else:
            new = p
        return True, {"path": _rel(new), "action": action, "title": title}
    except OSError as e:
        return False, {"error": f"could not change the note ({e})", "status": 500}
