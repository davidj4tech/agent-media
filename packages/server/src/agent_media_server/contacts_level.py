"""A person's speech level (contract §6.25 "Speech level"; David, 20 Oct 2026:
one scale for threads and people, "maybe we should … call it the same
thing to keep it simple").

A person on their card has "default" or one of the thread speech levels
(agent_media_core.speak_priority: interrupt, auto, pocket, normal, quiet),
kept per book in the `person_level` table (through schema bumps, as David's
other decisions are). It is the level of every chat that is theirs — each
Matrix room whose one person they are — unless the room has a level of its
own, and the level their mail notices will follow
(docs/proposals/2026-10-20-mail-in-sasonica.md).

How a room's level is found: its own (set in the room, /session/priority),
else its person's, else quiet — a room starts quiet (David, 9 Oct 2026).
This module is the fallback speak_priority asks for a conversation with no
level of its own (`register`), so everything that reads a level — the Matrix
intake's decision, the thread rows' `speech` — sees the person's.

It replaced the card's Default/Priority/Quiet setting of the same day
(contacts_notify.py, its `notify` table): priority became auto, quiet stayed
quiet, default became none; contacts._connect moves the rows once.
"""

from __future__ import annotations

import sqlite3
import time

from agent_media_core.speak_priority import LEVELS

#: A Matrix room with no level of its own and no person's: kept with a
#: Play, nothing read out (David, 9 Oct 2026).
ROOM_DEFAULT = "quiet"
#: The old card setting's modes, as levels (none: "default").
FROM_NOTIFY = {"priority": "auto", "quiet": "quiet"}


def of(conn: sqlite3.Connection, cid: str) -> str | None:
    """`cid`'s own level, None for "default"."""
    got = conn.execute("SELECT level FROM person_level WHERE contact=?", (cid,)).fetchone()
    return got[0] if got and got[0] in LEVELS else None


def set_level(cid: str, level: str) -> str | None:
    """Set `cid`'s level in the caller's book ("default" drops it); their
    level after. KeyError for no such contact, ValueError for a level not
    known."""
    from . import contacts

    if level != "default" and level not in LEVELS:
        raise ValueError(f"level must be default or one of {', '.join(LEVELS)}")
    with contacts._WRITE:
        conn = contacts._connect()
        try:
            if not conn.execute("SELECT 1 FROM contacts WHERE id=?", (cid,)).fetchone():
                raise KeyError(cid)
            if level == "default":
                conn.execute("DELETE FROM person_level WHERE contact=?", (cid,))
            else:
                conn.execute("INSERT OR REPLACE INTO person_level (contact, level, at) "
                             "VALUES (?, ?, ?)", (cid, level, time.time()))
            got = of(conn, cid)
        finally:
            conn.close()
    _CACHE.clear()
    return got


def migrate_notify(conn: sqlite3.Connection) -> None:
    """Move the old `notify` rows into `person_level` once and drop the
    table: a person's chats setting, else their email one, as a level."""
    rows: dict = {}
    for c, ch, m in conn.execute("SELECT contact, channel, mode FROM notify"):
        lvl = FROM_NOTIFY.get(m)
        if lvl and (ch == "chats" or c not in rows):
            rows[c] = lvl
    now = time.time()
    for c, lvl in rows.items():
        conn.execute("INSERT OR IGNORE INTO person_level (contact, level, at) VALUES (?, ?, ?)",
                     (c, lvl, now))
    conn.execute("DROP TABLE notify")


#: contact → level, read again at most every few seconds: a level is read
#: for every row of the thread list and every incoming message.
_CACHE: dict = {}
_TTL = 5.0


def _levels() -> dict:
    from . import contacts

    now = time.monotonic()
    hit = _CACHE.get("rows")
    if not hit or now - hit[1] > _TTL:
        rows: dict = {}
        conn = contacts._connect(create=False)
        if conn is not None:
            try:
                rows = {c: lv for c, lv in conn.execute(
                    "SELECT contact, level FROM person_level") if lv in LEVELS}
            except sqlite3.Error:
                rows = {}
            finally:
                conn.close()
        hit = (rows, now)
        _CACHE["rows"] = hit
    return hit[0]


def level_of_contact(cid: str) -> str | None:
    """`cid`'s level in the owner's book, None for "default"."""
    return _levels().get(cid) if cid else None


def fallback(session: str) -> tuple[str, str] | None:
    """What speak_priority asks for a conversation with no level of its own:
    a Matrix room's person's level ("person"), else quiet ("room"); None for
    anything that is not a room the canvas holds (the default then)."""
    from . import contacts, matrix

    room = matrix._ROOMS.get(session)      # held only: never a load per row
    if room is None:
        return None
    if contacts.is_owner():
        got = contacts.for_room(room)
        lvl = level_of_contact(got["id"]) if got else None
        if lvl:
            return lvl, "person"
    return ROOM_DEFAULT, "room"


def level_for_address(address: str) -> str | None:
    """The level of the person with this email address, for the mail notices
    to come; None when no one, or no level of their own."""
    from . import contacts

    conn = contacts._connect(create=False)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT contact FROM emails WHERE email=?",
                           ((address or "").strip().lower(),)).fetchone()
    finally:
        conn.close()
    return level_of_contact(row[0]) if row else None


def register() -> None:
    """Make every level read on this server ask the owner's book for a room's
    person (the server's contacts start)."""
    from agent_media_core import speak_priority

    speak_priority.set_fallback(fallback)
