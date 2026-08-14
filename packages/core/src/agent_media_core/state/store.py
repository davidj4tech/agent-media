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
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .._paths import state_dir


SCHEMA_VERSION = 4

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

-- Explicit media bookmarks, keyed by channel + stable media id when available
-- (YouTube video id), else URI/path. These are user-created capture points,
-- with optional notes and org export hooks layered above the store.
CREATE TABLE IF NOT EXISTS bookmarks (
    channel     TEXT NOT NULL,
    media_id    TEXT NOT NULL,
    uri         TEXT NOT NULL,
    title       TEXT,
    pos_ms      INTEGER NOT NULL,
    end_pos_ms  INTEGER,
    duration_ms INTEGER,
    note        TEXT,
    transcript  TEXT,
    updated_at  REAL NOT NULL,
    extras      TEXT,
    PRIMARY KEY (channel, media_id)
);
CREATE INDEX IF NOT EXISTS bookmarks_updated_idx ON bookmarks (updated_at);
CREATE INDEX IF NOT EXISTS bookmarks_channel_updated_idx ON bookmarks (channel, updated_at);

-- Book channel playlists: an ordered list of part URIs plus a remembered
-- cursor (which part). Per-part within-offset resume reuses resume_pos
-- above (keyed by URI), so a playlist only needs to remember which part;
-- `cur_index` + the part's resume_pos together give (which part, where in
-- it). Advancing to the next part is just `cur_index += 1`.
CREATE TABLE IF NOT EXISTS playlists (
    name       TEXT PRIMARY KEY,
    channel    TEXT NOT NULL,             -- 'book'
    cur_index  INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
    name  TEXT NOT NULL,
    pos   INTEGER NOT NULL,               -- order in the list (0-based)
    uri   TEXT NOT NULL,
    title TEXT,
    PRIMARY KEY (name, pos)
);

-- Durable per-pane / per-session speech-mute policy. A muted pane still
-- renders its clips (so the popup can browse/replay) but is never played
-- through the broker and never ducks music. Resolution is pane → session
-- → unmuted (see StateStore.resolve_mute). `muted=0` is an explicit unmute
-- that overrides a broader (session) mute; absence of a row means "unset".
CREATE TABLE IF NOT EXISTS mute_policy (
    scope      TEXT NOT NULL,             -- 'pane' | 'session'
    key        TEXT NOT NULL,             -- tmux pane id '%17' | tmux session name
    muted      INTEGER NOT NULL,          -- 1 muted, 0 explicit-unmuted
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, key)
);
"""


def default_db_path() -> Path:
    return state_dir() / "state.db"


def _pid_alive(pid: int) -> bool:
    """True if a (host-local) process with `pid` currently exists.

    Used by the now_playing orphan guard. now_playing is host-local state and
    its writer runs on the same host, so a local liveness probe is sufficient.
    Conservative on the unknown: any error other than "no such process" is
    treated as alive so we never hide a live row on a transient failure.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. PermissionError → process exists, owned elsewhere
    return True


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
            # Existing user DBs may have the pre-transcript bookmarks table from
            # an in-flight build; add the column without disturbing saved rows.
            cur.execute("PRAGMA table_info(bookmarks)")
            cols = {row[1] for row in cur.fetchall()}
            if "end_pos_ms" not in cols:
                cur.execute("ALTER TABLE bookmarks ADD COLUMN end_pos_ms INTEGER")
            if "transcript" not in cols:
                cur.execute("ALTER TABLE bookmarks ADD COLUMN transcript TEXT")
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

    def close(self) -> None:
        """Close this thread's cached connection, if any.

        Mainly matters right before forking a detached child (see
        ``_play_detached`` in the Claude Code hook). A child that inherits an
        open SQLite **WAL** connection shares the parent's wal-index/lock
        state; when the parent then exits — or any unrelated short-lived
        reader process closes as the apparent *last* connection — SQLite
        checkpoints and **unlinks** the ``-wal``/``-shm`` files out from under
        the still-running child. The child's later autocommitted writes
        (e.g. ``now_playing`` during speech playback) land in that orphaned,
        already-deleted WAL and are invisible to every fresh reader, while the
        main db file stays frozen. Releasing the connection before the fork
        keeps the child's own WAL the sole, un-inherited one.
        """
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001 — best-effort release
                pass
            self._local.conn = None

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
            # Snapshot the previous now_playing for this sink so we can log a
            # history row when the URI actually changes. Speech already logs
            # history explicitly via add_history(); skip it here to avoid
            # double-counting. Music/book have no explicit logging, so this is
            # where their plays land.
            prev_uri: Optional[str] = None
            if sink != "speech":
                cur.execute("SELECT uri FROM now_playing WHERE sink = ?", (sink,))
                row = cur.fetchone()
                if row is not None:
                    prev_uri = row[0]
            cur.execute(
                "INSERT OR REPLACE INTO now_playing "
                "(sink, uri, started_at, content_type, target, pause_pos_ms, extras) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (sink, uri, started_at, content_type, target,
                 json.dumps(extras) if extras else None),
            )
            if sink != "speech" and uri and uri != prev_uri:
                cur.execute(
                    "INSERT INTO history "
                    "(sink, uri, started_at, ended_at, target, source, "
                    " content_type, text, extras) "
                    "VALUES (?, ?, ?, NULL, ?, NULL, ?, NULL, ?)",
                    (sink, uri, started_at, target, content_type,
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
        result = {
            "sink": sink_,
            "uri": uri,
            "started_at": started_at,
            "content_type": content_type,
            "target": target,
            "pause_pos_ms": pause_pos_ms,
            "extras": json.loads(extras) if extras else None,
        }
        # Orphan guard: the detached speech player clears its own row in a
        # finally block, so a surviving row whose recorded writer process is
        # gone means it crashed / was SIGKILLed mid-playback. Such a row would
        # otherwise haunt the status bar and popup until the next reply
        # overwrites it. Hide it, and self-heal with a started_at-guarded delete
        # so we never race-delete a fresh replacement written between our read
        # and this clear. Only speech records writer_pid; music/book and legacy
        # rows have none and are left untouched.
        ex = result["extras"] or {}
        pid = ex.get("writer_pid")
        if isinstance(pid, int) and not _pid_alive(pid):
            with self._cursor() as cur:
                cur.execute(
                    "DELETE FROM now_playing WHERE sink = ? AND started_at = ?",
                    (sink_, started_at),
                )
            return None
        return result

    def clear_now_playing(self, sink: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM now_playing WHERE sink = ?", (sink,))

    def set_pause_position(self, sink: str, pos_ms: Optional[int]) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE now_playing SET pause_pos_ms = ? WHERE sink = ?",
                        (pos_ms, sink))

    # ---- mute policy ------------------------------------------------------
    #
    # Durable per-pane / per-session speech suppression. Distinct from the
    # transient mpv `mute` property on the broker: this decides, at intake
    # time, whether a pane's speech is played at all. A muted pane still
    # renders + records history (for popup replay) but is never played and
    # never ducks music.

    def set_mute(self, scope: str, key: str,
                 muted: Optional[bool]) -> None:
        """Set or clear a mute override. `muted=None` deletes the row,
        returning that (scope, key) to "unset" so a broader scope applies.
        """
        if not key:
            return
        with self._cursor() as cur:
            if muted is None:
                cur.execute("DELETE FROM mute_policy WHERE scope = ? AND key = ?",
                            (scope, key))
            else:
                cur.execute(
                    "INSERT OR REPLACE INTO mute_policy "
                    "(scope, key, muted, updated_at) VALUES (?, ?, ?, ?)",
                    (scope, key, 1 if muted else 0, time.time()))

    def get_mute(self, scope: str, key: str) -> Optional[bool]:
        """The override for one (scope, key), or None when unset."""
        if not key:
            return None
        with self._cursor() as cur:
            cur.execute("SELECT muted FROM mute_policy WHERE scope = ? AND key = ?",
                        (scope, key))
            row = cur.fetchone()
        return None if row is None else bool(row[0])

    def resolve_mute(self, pane: str, tmux_session: str) -> bool:
        """Effective mute for a speech event: pane override wins, then the
        owning tmux session, then the default (unmuted). An explicit pane
        unmute (`muted=0`) therefore overrides a session-wide mute.
        """
        if pane:
            v = self.get_mute("pane", pane)
            if v is not None:
                return v
        if tmux_session:
            v = self.get_mute("session", tmux_session)
            if v is not None:
                return v
        return False

    def list_mutes(self) -> dict:
        """All overrides, as {"panes": {key: bool}, "sessions": {key: bool}}."""
        with self._cursor() as cur:
            cur.execute("SELECT scope, key, muted FROM mute_policy "
                        "ORDER BY scope, key")
            rows = cur.fetchall()
        out: dict = {"panes": {}, "sessions": {}}
        for scope, key, muted in rows:
            bucket = "panes" if scope == "pane" else "sessions"
            out[bucket][key] = bool(muted)
        return out

    def prune_panes(self, live_pane_ids) -> int:
        """Drop pane overrides for tmux panes that no longer exist.

        `live_pane_ids` must be a *reliable* snapshot of current pane ids
        (e.g. from `tmux list-panes -a`). An empty set is treated as "could
        not determine" and is a no-op, never a mass-delete — so a failed or
        server-less tmux query can't wipe the policy. Returns rows removed.
        """
        live = [p for p in live_pane_ids if p]
        if not live:
            return 0
        placeholders = ",".join("?" for _ in live)
        with self._cursor() as cur:
            cur.execute(
                f"DELETE FROM mute_policy WHERE scope = 'pane' "
                f"AND key NOT IN ({placeholders})",
                live)
            return cur.rowcount or 0

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

    # ---- bookmarks --------------------------------------------------------

    def set_bookmark(self, channel: str, media_id: str, uri: str, pos_ms: int,
                     title: Optional[str] = None,
                     end_pos_ms: Optional[int] = None,
                     duration_ms: Optional[int] = None,
                     note: Optional[str] = None,
                     transcript: Optional[str] = None,
                     extras: Optional[dict] = None) -> None:
        if not channel or not media_id or not uri:
            return
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO bookmarks "
                "(channel, media_id, uri, title, pos_ms, end_pos_ms, duration_ms, note, transcript, updated_at, extras) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (channel, media_id, uri, title, max(0, int(pos_ms)),
                 None if end_pos_ms is None else max(0, int(end_pos_ms)),
                 duration_ms, note, transcript, time.time(),
                 json.dumps(extras) if extras else None),
            )

    def get_bookmark(self, channel: str, media_id: str) -> Optional[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT channel, media_id, uri, title, pos_ms, end_pos_ms, duration_ms, note, transcript, updated_at, extras "
                "FROM bookmarks WHERE channel = ? AND media_id = ?",
                (channel, media_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        channel_, media_id_, uri, title, pos_ms, end_pos_ms, duration_ms, note, transcript, updated_at, extras = row
        return {
            "channel": channel_, "media_id": media_id_, "uri": uri,
            "title": title, "pos_ms": pos_ms, "end_pos_ms": end_pos_ms,
            "duration_ms": duration_ms,
            "note": note, "transcript": transcript, "updated_at": updated_at,
            "extras": json.loads(extras) if extras else None,
        }

    _BOOKMARK_PENDING_PREFIX = "bookmark_pending:"

    def set_bookmark_pending(self, channel: str, data: Optional[dict],
                             slot: str = "") -> None:
        key = self._BOOKMARK_PENDING_PREFIX + channel + ":" + (slot or "default")
        with self._cursor() as cur:
            if data is None:
                cur.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            (key, json.dumps(data)))

    def get_bookmark_pending(self, channel: str, slot: str = "") -> Optional[dict]:
        key = self._BOOKMARK_PENDING_PREFIX + channel + ":" + (slot or "default")
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def list_bookmarks(self, limit: int = 20,
                       channel: Optional[str] = None) -> list[dict]:
        with self._cursor() as cur:
            if channel:
                cur.execute(
                    "SELECT channel, media_id, uri, title, pos_ms, end_pos_ms, duration_ms, note, transcript, updated_at, extras "
                    "FROM bookmarks WHERE channel = ? ORDER BY updated_at DESC LIMIT ?",
                    (channel, max(1, int(limit))),
                )
            else:
                cur.execute(
                    "SELECT channel, media_id, uri, title, pos_ms, end_pos_ms, duration_ms, note, transcript, updated_at, extras "
                    "FROM bookmarks ORDER BY updated_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                )
            rows = cur.fetchall()
        out = []
        for channel_, media_id, uri, title, pos_ms, end_pos_ms, duration_ms, note, transcript, updated_at, extras in rows:
            out.append({
                "channel": channel_, "media_id": media_id, "uri": uri,
                "title": title, "pos_ms": pos_ms, "end_pos_ms": end_pos_ms,
            "duration_ms": duration_ms,
                "note": note, "transcript": transcript, "updated_at": updated_at,
                "extras": json.loads(extras) if extras else None,
            })
        return out

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

    # ---- book playlists --------------------------------------------------
    #
    # An ordered list of part URIs with a remembered cursor. Within-part
    # offset resume reuses resume_pos (keyed by URI); `cur_index` only tracks
    # which part. `_PLAYLIST_ACTIVE_KEY` points at the playlist currently
    # being played so `book next`/`prev` know which list to advance — cleared
    # when an ad-hoc (non-playlist) book is opened or the book stops.

    _PLAYLIST_ACTIVE_KEY = "book_playlist_active"

    def create_playlist(self, name: str, channel: str = "book") -> bool:
        """Create an empty playlist. Returns False if one already exists."""
        import time
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM playlists WHERE name = ?", (name,))
            if cur.fetchone():
                return False
            cur.execute(
                "INSERT INTO playlists (name, channel, cur_index, updated_at) "
                "VALUES (?, ?, 0, ?)",
                (name, channel, time.time()),
            )
        return True

    def delete_playlist(self, name: str) -> bool:
        """Remove a playlist and its items. Returns False if it didn't exist."""
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM playlists WHERE name = ?", (name,))
            if not cur.fetchone():
                return False
            cur.execute("DELETE FROM playlist_items WHERE name = ?", (name,))
            cur.execute("DELETE FROM playlists WHERE name = ?", (name,))
            if self.get_playlist_active() == name:
                self.clear_playlist_active()
        return True

    def add_playlist_items(self, name: str,
                           items: list) -> int:
        """Append items (str URI, or (uri, title) pairs) to a playlist.

        Returns the number of items now in the list. Raises KeyError if the
        playlist doesn't exist.
        """
        import time
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM playlists WHERE name = ?", (name,))
            if not cur.fetchone():
                raise KeyError(name)
            cur.execute("SELECT COALESCE(MAX(pos), -1) FROM playlist_items "
                        "WHERE name = ?", (name,))
            pos = (cur.fetchone()[0]) + 1
            for item in items:
                if isinstance(item, (tuple, list)):
                    uri, title = item[0], (item[1] if len(item) > 1 else None)
                else:
                    uri, title = item, None
                cur.execute(
                    "INSERT INTO playlist_items (name, pos, uri, title) "
                    "VALUES (?, ?, ?, ?)",
                    (name, pos, uri, title),
                )
                pos += 1
            cur.execute("UPDATE playlists SET updated_at = ? WHERE name = ?",
                        (time.time(), name))
            cur.execute("SELECT COUNT(*) FROM playlist_items WHERE name = ?",
                        (name,))
            return int(cur.fetchone()[0])

    def get_playlist(self, name: str) -> Optional[dict]:
        """A playlist with its ordered items, or None if it doesn't exist."""
        with self._cursor() as cur:
            cur.execute("SELECT name, channel, cur_index, updated_at "
                        "FROM playlists WHERE name = ?", (name,))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute("SELECT pos, uri, title FROM playlist_items "
                        "WHERE name = ? ORDER BY pos", (name,))
            items = [{"pos": p, "uri": u, "title": t}
                     for (p, u, t) in cur.fetchall()]
        return {
            "name": row[0],
            "channel": row[1],
            "cur_index": row[2],
            "updated_at": row[3],
            "items": items,
        }

    def list_playlists(self, channel: Optional[str] = None) -> list[dict]:
        """All playlists (optionally for one channel) with item counts."""
        q = ("SELECT p.name, p.channel, p.cur_index, p.updated_at, "
             "COUNT(i.pos) "
             "FROM playlists p LEFT JOIN playlist_items i ON i.name = p.name")
        args: tuple = ()
        if channel is not None:
            q += " WHERE p.channel = ?"
            args = (channel,)
        q += " GROUP BY p.name ORDER BY p.name"
        with self._cursor() as cur:
            cur.execute(q, args)
            rows = cur.fetchall()
        return [{"name": n, "channel": c, "cur_index": ci,
                 "updated_at": ua, "count": cnt}
                for (n, c, ci, ua, cnt) in rows]

    def get_playlist_item(self, name: str, index: int) -> Optional[dict]:
        """The item at `index` in a playlist, or None if out of range."""
        with self._cursor() as cur:
            cur.execute("SELECT pos, uri, title FROM playlist_items "
                        "WHERE name = ? AND pos = ?", (name, index))
            row = cur.fetchone()
        if row is None:
            return None
        return {"pos": row[0], "uri": row[1], "title": row[2]}

    def set_playlist_index(self, name: str, index: int) -> None:
        """Move a playlist's cursor to `index` (clamped to >= 0)."""
        import time
        with self._cursor() as cur:
            cur.execute("UPDATE playlists SET cur_index = ?, updated_at = ? "
                        "WHERE name = ?", (max(0, int(index)), time.time(), name))

    def set_playlist_active(self, name: Optional[str]) -> None:
        with self._cursor() as cur:
            if name is None:
                cur.execute("DELETE FROM meta WHERE key = ?",
                            (self._PLAYLIST_ACTIVE_KEY,))
            else:
                cur.execute("INSERT OR REPLACE INTO meta (key, value) "
                            "VALUES (?, ?)", (self._PLAYLIST_ACTIVE_KEY, name))

    def get_playlist_active(self) -> Optional[str]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?",
                        (self._PLAYLIST_ACTIVE_KEY,))
            row = cur.fetchone()
        return row[0] if row else None

    def clear_playlist_active(self) -> None:
        self.set_playlist_active(None)

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

    _SPEECH_SPEED_KEY = "speech_speed"

    def set_speech_speed(self, rate: Optional[float]) -> None:
        """Remember the speech broker's playback rate.

        The rate lives on the long-lived broker mpv, so it persists across
        clips — but a *remote* broker (the phone) can only be read over the
        bridge, and while idle there's no now_playing mirror to read it from.
        Stashing it here lets the popup show "you're still at 1.5×" between
        replies without paying a round-trip. 1.0 clears the row (nothing to
        show).
        """
        with self._cursor() as cur:
            if rate is None or abs(float(rate) - 1.0) <= 0.005:
                cur.execute("DELETE FROM meta WHERE key = ?",
                            (self._SPEECH_SPEED_KEY,))
            else:
                cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            (self._SPEECH_SPEED_KEY, repr(float(rate))))

    def get_speech_speed(self) -> Optional[float]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?",
                        (self._SPEECH_SPEED_KEY,))
            row = cur.fetchone()
        if not row:
            return None
        try:
            return float(row[0])
        except (ValueError, TypeError):
            return None

    _ROOMS_DUCK_KEY = "rooms_duck"

    def set_rooms_duck(self, marker: Optional[dict]) -> None:
        """Stash (or clear, with None) the in-force Snapcast rooms-duck marker:
        ``{"level": int, "vols": {client_id: pre_duck_percent}}``. Persisted (not
        in-memory) so a process killed mid-duck self-heals: the next
        before_speech reuses these original volumes as the restore baseline
        instead of re-capturing the already-ducked ones."""
        with self._cursor() as cur:
            if marker is None:
                cur.execute("DELETE FROM meta WHERE key = ?",
                            (self._ROOMS_DUCK_KEY,))
            else:
                cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            (self._ROOMS_DUCK_KEY, json.dumps(marker)))

    def get_rooms_duck(self) -> Optional[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?",
                        (self._ROOMS_DUCK_KEY,))
            row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None

    _MUSIC_DUCK_KEY = "music_duck"

    def set_music_duck(self, marker: Optional[dict]) -> None:
        """Stash (or clear, with None) the in-force music duck:
        ``{"level": int, "baseline": int, "target": str}``.

        The twin of :meth:`set_rooms_duck`, and it exists for the same reason
        the rooms one does — plus a sharper one. The duck used to be recorded
        only inside the now-playing row, which is shared: another flow can clear
        or overwrite it between the duck and the restore, and when that happened
        after_speech found nothing to restore and left the music at the duck
        level indefinitely (observed on the phone twice on 2026-08-14, once for
        two hours). A dedicated key makes the debt outlive whatever else
        happens to that row, and persisting it means a process killed mid-duck
        still self-heals on the next clip.
        """
        with self._cursor() as cur:
            if marker is None:
                cur.execute("DELETE FROM meta WHERE key = ?",
                            (self._MUSIC_DUCK_KEY,))
            else:
                cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            (self._MUSIC_DUCK_KEY, json.dumps(marker)))

    def get_music_duck(self) -> Optional[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?",
                        (self._MUSIC_DUCK_KEY,))
            row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None

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

    def recent_errors(self, *, component: Optional[str] = None,
                      limit: int = 20, since: Optional[float] = None) -> list[dict]:
        """Most recent errors first.

        Everything wrote here and nothing read it, which is how a bridge
        injecting into a tmux session that no longer existed stayed broken for
        weeks. `media errors` and the MCP `errors` tool read this.
        """
        q = ("SELECT id, at, component, message, extras FROM errors")
        clauses, args = [], []
        if component:
            clauses.append("component = ?")
            args.append(component)
        if since is not None:
            clauses.append("at >= ?")
            args.append(since)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        cols = ("id", "at", "component", "message", "extras")
        with self._cursor() as cur:
            cur.execute(q, args)
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            if d.get("extras"):
                try:
                    d["extras"] = json.loads(d["extras"])
                except ValueError:
                    pass
            out.append(d)
        return out

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
