"""Chats about a note: the Organiser's ask box.

A heading or a note in the Org tree gets a chat box, like the app's new chat:
the words start a fresh session, opened in the notes tree so the agent can
read and edit the file, with the item named up front. Which chats were about
which item is remembered, so the note's page can list them.

  POST /notes/ask {"path", "at"?, "text", "agent"?} → /ask's answer (+ "path", "at")
  GET  /notes/read also answers "chats": [{session, title, at}], newest first

The item is keyed by its file and title, not its line: lines move as the file
is edited above it, and a title is how notes_edit finds a heading again too.
A refile to another file starts the list afresh; the chats themselves live on.
"""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path

from agent_media_core._paths import state_dir

from . import notes, send

#: How many chats a single item keeps a record of.
KEEP = 20


def _store() -> Path:
    return state_dir() / "note-chats.json"


def _key(path: str, title: str) -> str:
    return f"{path}\t{title.strip()}"


def _load() -> dict[str, list[dict]]:
    try:
        data = json.loads(_store().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember(path: str, title: str, entry: dict) -> None:
    store = _store()
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load()
        rows = [r for r in data.get(_key(path, title), []) if r.get("session") != entry["session"]]
        data[_key(path, title)] = [entry, *rows][:KEEP]
        tmp = store.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(store)


def chats(path: str, title: str) -> list[dict]:
    """The chats started about this item, newest first."""
    return _load().get(_key(path, title), [])


def prompt(got: dict, text: str) -> str:
    """The first message: the item named, then the words. One line — /ask
    flattens it anyway."""
    where = f"~/org/{got['path']}"
    if got.get("at"):
        where += f", line {got['at']}"
        if got.get("state"):
            where += f", {got['state']}"
    return f'About "{got["title"]}" in my Org notes ({where}): {text.strip()}'


def ask(rel: str, at: int, text: str, bearer: str, *, agent: str = "") -> tuple[bool, dict]:
    """Start a session about the note at `rel` (the heading on line `at`)."""
    if not (text or "").strip():
        return False, {"error": "empty message"}
    ok, got = notes.read(rel, at, bearer)
    if not ok:
        return False, got
    ok, detail = send.ask(prompt(got, text), bearer, agent=agent,
                          cwd=str(notes.root()), cwd_trusted=True)
    if ok and detail.get("session"):
        _remember(got["path"], got["title"], {
            "session": detail["session"], "title": " ".join(text.split())[:80],
            "at": int(time.time())})
    return ok, {**detail, "path": got["path"], "at": at}
