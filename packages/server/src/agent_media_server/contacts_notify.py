"""A person's own notification setting (contract §6.25 "Notifications";
David, 20 Oct 2026: "a little more control over specific contact's
notifications").

Per channel, kept in the book's `notify` table (through schema bumps, as
David's other decisions are): `chats` — their Matrix rooms, SMS, Messenger,
WhatsApp and the rest — and `email`. Each is "default", "priority" or
"quiet" (agent_media_core.contact_notify says what each does). A channel
not set is "default"; setting "default" drops the row.

Where it is asked: the Matrix speech intake decides each incoming message
(play, notice, or kept with a Play) and turns that by the sender's person
through `agent_media_core.contact_notify`, whose resolver this module
registers when the server starts its contacts (`register`). Calls raise no
notice anywhere yet, so there is no calls channel. Mail notices (a later
step of docs/proposals/2026-10-20-mail-in-sasonica.md) ask
`contact_notify.mode(<address>, "email")`, already answered here.
"""

from __future__ import annotations

import sqlite3
import time

from agent_media_core.contact_notify import CHANNELS, MODES


def of(conn: sqlite3.Connection, cid: str) -> dict:
    """`{channel: mode}` for every channel, "default" where unset."""
    got = dict.fromkeys(CHANNELS, "default")
    for ch, mode in conn.execute("SELECT channel, mode FROM notify WHERE contact=?", (cid,)):
        if ch in got and mode in MODES:
            got[ch] = mode
    return got


def set_mode(cid: str, channel: str, mode: str) -> dict:
    """Set one channel's mode for contact `cid` in the caller's book; the
    card's `notify` after. KeyError for no such contact, ValueError for a
    channel or mode not known."""
    from . import contacts

    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    with contacts._WRITE:
        conn = contacts._connect()
        try:
            if not conn.execute("SELECT 1 FROM contacts WHERE id=?", (cid,)).fetchone():
                raise KeyError(cid)
            if mode == "default":
                conn.execute("DELETE FROM notify WHERE contact=? AND channel=?", (cid, channel))
            else:
                conn.execute("INSERT OR REPLACE INTO notify (contact, channel, mode, at) "
                             "VALUES (?, ?, ?, ?)", (cid, channel, mode, time.time()))
            got = of(conn, cid)
        finally:
            conn.close()
    _CACHE.clear()
    return got


#: (contact, channel) → mode, read again at most every few seconds: the
#: intake asks once per incoming message.
_CACHE: dict = {}
_TTL = 5.0


def _mode_of_contact(cid: str, channel: str) -> str:
    from . import contacts

    now = time.monotonic()
    hit = _CACHE.get("rows")
    if not hit or now - hit[1] > _TTL:
        rows: dict = {}
        conn = contacts._connect(create=False)
        if conn is not None:
            try:
                for c, ch, m in conn.execute("SELECT contact, channel, mode FROM notify"):
                    rows[(c, ch)] = m
            finally:
                conn.close()
        hit = (rows, now)
        _CACHE["rows"] = hit
    return hit[0].get((cid, channel), "default")


def resolve(key: str, channel: str) -> str | None:
    """The resolver agent_media_core.contact_notify asks: `key` is a thread
    id (chats) or an address (email); the owner's book, whose rooms and
    mail these are. None when no person is found."""
    from . import contacts, matrix

    if not contacts.is_owner():
        return None
    cid = None
    if channel == "chats":
        room = next((r for r in list(matrix._ROOMS.values()) if r.thread == key), None)
        if room is None:
            return None
        got = contacts.for_room(room)
        cid = got["id"] if got else None
    elif channel == "email":
        conn = contacts._connect(create=False)
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT contact FROM emails WHERE email=?",
                               (key.strip().lower(),)).fetchone()
        finally:
            conn.close()
        cid = row[0] if row else None
    return _mode_of_contact(cid, channel) if cid else None


def register() -> None:
    """Make the intake ask this book (the server's contacts start)."""
    from agent_media_core import contact_notify

    contact_notify.set_resolver(resolve)
