"""SQLite-backed state store: now-playing, events, history, errors.

Lives at `${XDG_STATE_HOME:-~/.local/state}/agent-media/state.db` by
default. The schema is tiny on purpose — route/ writes a couple of rows
per event so observability ("what's playing right now", "what was the
last thing Claude said") is one query away.

Phase 3 starter scope: enough for the ducker / interruption coordinator.
Queue persistence + replay come later.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .._paths import state_dir


SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS now_playing (
    sink         TEXT PRIMARY KEY,
    uri          TEXT NOT NULL,
    started_at   REAL NOT NULL,
    content_type TEXT,
    target       TEXT,
    -- Position captured at pause-time, used by interruption resume.
    pause_pos_ms INTEGER,
    extras       TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sink         TEXT NOT NULL,
    uri          TEXT NOT NULL,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    target       TEXT,
    source       TEXT,
    content_type TEXT,
    text         TEXT,
    extras       TEXT
);
CREATE INDEX IF NOT EXISTS history_started_idx ON history (started_at);

CREATE TABLE IF NOT EXISTS errors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL NOT NULL,
    component TEXT NOT NULL,
    message   TEXT NOT NULL,
    extras    TEXT
);

-- Durable "where was I" bookmarks for the book channel (sink-book),
-- keyed by the normalized URI. Distinct from now_playing.pause_pos_ms,
-- which is transient speech-interruption state; these survive channel
-- switches and restarts so `book resume` lands at the right spot.
CREATE TABLE IF NOT EXISTS resume_pos (
    uri        TEXT PRIMARY KEY,
    pos_ms     INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
"""


def default_db_path() -> Path:
    return state_dir() / "state.db"


class StateStore:
    """Thread-safe SQLite wrapper. One connection per thread.

    The store is intentionally small — callers should keep transactions
    short and not hold connections open.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._cursor() as cur:
            cur.executescript(SCHEMA)
            cur.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                        ("schema_version", str(SCHEMA_VERSION)))

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), isolation_level=None,
                                check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = self._conn()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    # ---- now_playing ------------------------------------------------------

    def set_now_playing(self, sink: str, uri: str, started_at: float,
                        target: str = "local",
                        content_type: Optional[str] = None,
                        extras: Optional[dict] = None) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO now_playing "
                "(sink, uri, started_at, content_type, target, pause_pos_ms, extras) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (sink, uri, started_at, content_type, target,
                 json.dumps(extras) if extras else None),
            )

    def get_now_playing(self, sink: str) -> Optional[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT sink, uri, started_at, content_type, target, pause_pos_ms, extras "
                "FROM now_playing WHERE sink = ?",
                (sink,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        sink_, uri, started_at, content_type, target, pause_pos_ms, extras = row
        return {
            "sink": sink_,
            "uri": uri,
            "started_at": started_at,
            "content_type": content_type,
            "target": target,
            "pause_pos_ms": pause_pos_ms,
            "extras": json.loads(extras) if extras else None,
        }

    def clear_now_playing(self, sink: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM now_playing WHERE sink = ?", (sink,))

    def set_pause_position(self, sink: str, pos_ms: Optional[int]) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE now_playing SET pause_pos_ms = ? WHERE sink = ?",
                        (pos_ms, sink))

    # ---- music content-type intent ----------------------------------------
    #
    # The caller's content-type intent for whatever is queued on the music
    # sink ("this YouTube URL is an audiobook, pause it instead of ducking").
    # Lives in `meta` rather than `now_playing` on purpose: the interruption
    # coordinator wipes the music `now_playing` row after *every* speech clip
    # (see route/coordinator.after_speech), so a hint stored there would only
    # survive the first interruption. This one persists until the next
    # music_play overwrites it or music_stop clears it.

    _MUSIC_INTENT_KEY = "music_content_intent"

    def set_music_intent(self, uri: str, content_type: Optional[str]) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (self._MUSIC_INTENT_KEY,
                 json.dumps({"uri": uri, "content_type": content_type})),
            )

    def get_music_intent(self) -> Optional[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?",
                        (self._MUSIC_INTENT_KEY,))
            row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def clear_music_intent(self) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM meta WHERE key = ?",
                        (self._MUSIC_INTENT_KEY,))

    # ---- book resume bookmarks -------------------------------------------
    #
    # Durable per-URI "where was I in this book" positions for the book
    # channel, plus a pointer to the last book opened so a bare `book
    # resume` knows what to reopen. See the resume_pos table comment.

    _BOOK_LAST_KEY = "book_last_uri"

    def set_resume_pos(self, uri: str, pos_ms: int) -> None:
        import time
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO resume_pos (uri, pos_ms, updated_at) "
                "VALUES (?, ?, ?)",
                (uri, max(0, int(pos_ms)), time.time()),
            )

    def get_resume_pos(self, uri: str) -> Optional[int]:
        with self._cursor() as cur:
            cur.execute("SELECT pos_ms FROM resume_pos WHERE uri = ?", (uri,))
            row = cur.fetchone()
        return int(row[0]) if row else None

    def set_book_last(self, uri: str) -> None:
        with self._cursor() as cur:
            cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        (self._BOOK_LAST_KEY, uri))

    def get_book_last(self) -> Optional[str]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?",
                        (self._BOOK_LAST_KEY,))
            row = cur.fetchone()
        return row[0] if row else None

    # ---- channel concurrency: focus + bed --------------------------------
    #
    # `focus` is which channel is in front (book | music); the other goes to
    # a quiet bed or out of the way. `book_bed` is how the *music* channel
    # behaves under a foregrounded book — duck (instrumental) or pause
    # (lyrics) — switchable at runtime. Both persist in meta so the speech
    # coordinator and the MCP verbs agree on the current arrangement.

    _FOCUS_KEY = "focus_channel"
    _BOOK_BED_KEY = "book_bed_strategy"

    def set_focus(self, channel: Optional[str]) -> None:
        with self._cursor() as cur:
            if channel is None:
                cur.execute("DELETE FROM meta WHERE key = ?", (self._FOCUS_KEY,))
            else:
                cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            (self._FOCUS_KEY, channel))

    def get_focus(self) -> Optional[str]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?", (self._FOCUS_KEY,))
            row = cur.fetchone()
        return row[0] if row else None

    def set_book_bed(self, strategy: str) -> None:
        with self._cursor() as cur:
            cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        (self._BOOK_BED_KEY, strategy))

    def get_book_bed(self) -> Optional[str]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?", (self._BOOK_BED_KEY,))
            row = cur.fetchone()
        return row[0] if row else None

    # ---- history ----------------------------------------------------------

    def add_history(self, *, sink: str, uri: str, started_at: float,
                    ended_at: Optional[float] = None, target: str = "local",
                    source: Optional[str] = None,
                    content_type: Optional[str] = None,
                    text: Optional[str] = None,
                    extras: Optional[dict] = None) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO history "
                "(sink, uri, started_at, ended_at, target, source, content_type, text, extras) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sink, uri, started_at, ended_at, target, source, content_type,
                 text, json.dumps(extras) if extras else None),
            )
            return cur.lastrowid or 0

    def recent_history(self, *, sink: Optional[str] = None,
                       limit: int = 20) -> list[dict]:
        q = ("SELECT id, sink, uri, started_at, ended_at, target, source, "
             "content_type, text, extras FROM history")
        args: tuple = ()
        if sink is not None:
            q += " WHERE sink = ?"
            args = (sink,)
        q += " ORDER BY started_at DESC LIMIT ?"
        args = args + (limit,)
        with self._cursor() as cur:
            cur.execute(q, args)
            rows = cur.fetchall()
        cols = ["id", "sink", "uri", "started_at", "ended_at", "target",
                "source", "content_type", "text", "extras"]
        result = []
        for r in rows:
            row = dict(zip(cols, r))
            if row.get("extras"):
                try:
                    row["extras"] = json.loads(row["extras"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(row)
        return result

    # ---- errors -----------------------------------------------------------

    def log_error(self, component: str, message: str,
                  *, extras: Optional[dict] = None,
                  at: Optional[float] = None) -> None:
        import time
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO errors (at, component, message, extras) "
                "VALUES (?, ?, ?, ?)",
                (at or time.time(), component, message,
                 json.dumps(extras) if extras else None),
            )
