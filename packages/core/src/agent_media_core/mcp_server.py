"""MCP control surface for agent-media.

Two entrypoints over the same tool definitions:

  * `media-mcp`      — stdio transport, for Claude Code (user-scope
                       registration via `claude mcp add`).
  * `media-mcp-http` — streamable-HTTP transport, for remote callers
                       (sp4r, HA, anything off-box). Bind via
                       MEDIA_MCP_HOST / MEDIA_MCP_PORT (defaults
                       127.0.0.1:8765 — set MEDIA_MCP_HOST to the
                       Tailscale IP to expose on the tailnet).

Tools cover the surface RESTRUCTURE.md called for: speech.{pause,
resume,stop,now_playing,history,replay_last} and music.{play,pause,
resume,stop,volume,now_playing,seek} plus a convenience `say` that
submits a one-shot Event through the same intake pipeline the hooks
use.

Replaces the legacy Node `packages/media-mcp/server.js` end-to-end.
"""

import logging
import os
import threading
import time

from mcp.server.fastmcp import FastMCP


def _host() -> str:
    return os.environ.get("MEDIA_MCP_HOST", "127.0.0.1")


def _port() -> int:
    try:
        return int(os.environ.get("MEDIA_MCP_PORT", "8765"))
    except ValueError:
        return 8765

from .intake._env import load_env_file
from .route import (
    BED_DUCK,
    BED_PAUSE,
    FOCUS_BOOK,
    FOCUS_MUSIC,
    apply_focus,
    bed_strategy,
    coerce_content_type,
    detect_content_type,
    resolve,
)
from . import library
from .sinks import SinkBook, SinkMusic, SinkSpeech
from .sinks.book import normalize_uri
from .state import StateStore
from .types import Event, Priority, Source, Target


log = logging.getLogger(__name__)

mcp = FastMCP("agent-media", host=_host(), port=_port())


# --- shared singletons ----------------------------------------------------

def _state() -> StateStore:
    if not hasattr(_state, "_v"):
        _state._v = StateStore()  # type: ignore[attr-defined]
    return _state._v  # type: ignore[attr-defined]


def _speech() -> SinkSpeech:
    if not hasattr(_speech, "_v"):
        _speech._v = SinkSpeech()  # type: ignore[attr-defined]
    return _speech._v  # type: ignore[attr-defined]


def _music() -> SinkMusic:
    if not hasattr(_music, "_v"):
        _music._v = SinkMusic()  # type: ignore[attr-defined]
    return _music._v  # type: ignore[attr-defined]


def _book() -> SinkBook:
    if not hasattr(_book, "_v"):
        _book._v = SinkBook()  # type: ignore[attr-defined]
    return _book._v  # type: ignore[attr-defined]


def _save_book_bookmark(book: SinkBook, state: StateStore,
                        target: Target) -> None:
    """Persist the currently-open book's position as its resume bookmark.

    Called before pause/stop/switch so `book resume` (or reopening the same
    URI) lands where the listener left off. Best-effort and spawn-free.
    """
    try:
        np = state.get_now_playing("book")
        if not np:
            return
        pos = book.position(target)
        if pos is not None and pos > 0:
            state.set_resume_pos(np["uri"], pos)
    except Exception:  # noqa: BLE001
        pass


def _target(name: str) -> Target:
    return Target(name=name or "local")


def _book_target(name: str = "") -> Target:
    """Resolve the book channel's output target. An empty name means "use
    the configured default" (MEDIA_BOOK_DEFAULT_TARGET, default `local`) —
    so books can default to the rooms without every caller passing it, while
    an explicit `local`/`rooms` still wins.
    """
    if not name:
        name = os.environ.get("MEDIA_BOOK_DEFAULT_TARGET", "local")
    return Target(name=name or "local")


# --- say: one-shot synthesize+play ----------------------------------------

@mcp.tool()
def say(text: str,
        voice: str = "",
        engine: str = "",
        target: str = "local",
        priority: str = "normal") -> dict:
    """Synthesize `text` and play it through sink-speech.

    Args:
        text: What to speak.
        voice: Override the render voice (engine-specific). Empty = use default.
        engine: Override engine (edge / openai / qwen / realtime).
            Empty = use MEDIA_RENDER_ENGINE default.
        target: Sink target. Default "local".
        priority: "low" / "normal" / "high" / "urgent".
    """
    from .intake.submit import submit_event

    try:
        prio = Priority(priority)
    except ValueError:
        prio = Priority.NORMAL

    history_id = submit_event(Event(
        text=text, source=Source.MCP,
        priority=prio,
        voice=voice or None,
        engine=engine or None,
        target=_target(target),
        metadata={"kind": "say"},
    ), state=_state())
    return {"history_id": history_id}


# --- speech sink controls --------------------------------------------------

@mcp.tool()
def speech_pause(target: str = "local") -> dict:
    """Pause the speech sink (mid-clip). Use `speech_resume` to continue."""
    _speech().pause(_target(target))
    return {"ok": True}


@mcp.tool()
def speech_resume(target: str = "local") -> dict:
    """Resume the speech sink."""
    _speech().resume(_target(target))
    return {"ok": True}


@mcp.tool()
def speech_stop(target: str = "local") -> dict:
    """Stop the speech sink. Drops the current clip."""
    _speech().stop(_target(target))
    return {"ok": True}


@mcp.tool()
def speech_now_playing(target: str = "local") -> dict:
    """What the speech sink is currently playing — path + position."""
    t = _target(target)
    s = _speech()
    return {"idle": s.idle(t), "position_ms": s.position(t)}


@mcp.tool()
def speech_history(limit: int = 10) -> list[dict]:
    """The last N speech clips. Most recent first."""
    # Exclude "Claude is waiting" notif clips: they're alerts, not responses.
    # Over-fetch so filtering still leaves `limit` real responses.
    rows = _state().recent_history(sink="speech", limit=max(limit * 4, limit + 50))
    rows = [r for r in rows
            if not (isinstance(r.get("extras"), dict)
                    and r["extras"].get("kind") == "notif")]
    return rows[:limit]


@mcp.tool()
def speech_replay_last(target: str = "local") -> dict:
    """Replay the most recent speech clip."""
    # Skip "Claude is waiting" notif clips: replay the last real response.
    rows = _state().recent_history(sink="speech", limit=50)
    rows = [r for r in rows
            if not (isinstance(r.get("extras"), dict)
                    and r["extras"].get("kind") == "notif")]
    if not rows:
        return {"ok": False, "reason": "no history"}
    uri = rows[0].get("uri")
    if not uri:
        return {"ok": False, "reason": "history row missing uri"}
    _speech().play(uri, _target(target))
    return {"ok": True, "uri": uri}


def _last_speaking_pane() -> str:
    """The pane currently (or most recently) speaking, from the state store.

    The MCP daemon has no tmux pane of its own, so a pane-less `mute_pane`
    call targets whoever last spoke — the same "the pane I'm hearing" intent
    the popup uses.
    """
    st = _state()
    np = st.get_now_playing("speech") or {}
    ex = np.get("extras") if isinstance(np.get("extras"), dict) else {}
    pane = (ex or {}).get("source_pane") or ""
    if pane:
        return pane
    rows = st.recent_history(sink="speech", limit=1)
    if rows and isinstance(rows[0].get("extras"), dict):
        return rows[0]["extras"].get("source_pane") or ""
    return ""


@mcp.tool()
def mute_pane(pane: str = "", session: str = "", state: str = "toggle") -> dict:
    """Durably mute/unmute a tmux pane's (or a whole tmux session's) speech.

    A muted pane still renders and is recorded to history (so it can be
    replayed), but is never played live and never ducks music. With neither
    `pane` nor `session` given, targets the pane that is currently (or was
    last) speaking. `state` is `on`, `off`, or `toggle`.
    """
    st = _state()
    if session:
        scope, key = "session", session
    else:
        key = pane or _last_speaking_pane()
        if not key:
            return {"ok": False, "reason": "no pane (pass pane= or session=)"}
        scope = "pane"
    if state == "on":
        muted = True
    elif state == "off":
        muted = False
    else:  # toggle this scope's own override (no tmux here for an effective flip)
        muted = not bool(st.get_mute(scope, key))
    st.set_mute(scope, key, muted)
    # Muting also stops the covered pane's in-flight clip so it takes effect
    # immediately (the response is already in history, still replayable).
    stopped = False
    if muted:
        np = st.get_now_playing("speech") or {}
        ex = np.get("extras") if isinstance(np.get("extras"), dict) else {}
        ex = ex or {}
        covered = (ex.get("source_pane") == key if scope == "pane"
                   else ex.get("source_tmux_session") == key)
        if covered:
            _speech().stop(_target("local"))
            stopped = True
    return {"ok": True, "scope": scope, "key": key, "muted": muted,
            "stopped_current": stopped}


@mcp.tool()
def mute_status() -> dict:
    """All durable per-pane / per-session speech mutes."""
    return _state().list_mutes()


# --- music sink controls --------------------------------------------------

@mcp.tool()
def music_play(uri: str, replace: bool = True, target: str = "local",
               content_type: str = "") -> dict:
    """Play a URI on the music sink (Mopidy) — music or longform alike.

    Args:
        uri: Mopidy URI — e.g. `yt:https://...`, `https://stream.url`,
            `local:track:...`.
        replace: Clear the queue first (default True).
        content_type: How speech should interrupt this. `music`/`dj-set`/
            `ambient` duck the volume; `audiobook`/`podcast` pause and
            resume (with a short rewind) so you don't miss narration.
            Defaults to auto-detection from the URI — which classifies a
            bare YouTube/HTTP URL as music, so set `audiobook` explicitly
            for spoken-word content from YouTube.
    """
    _music().play(uri, _target(target), replace=replace)
    ct = coerce_content_type(content_type) or detect_content_type(uri)
    _state().set_music_intent(uri, ct.value)
    return {"ok": True, "uri": uri, "content_type": ct.value}


@mcp.tool()
def music_pause(target: str = "local") -> dict:
    """Pause music."""
    _music().pause(_target(target))
    return {"ok": True}


@mcp.tool()
def music_resume(target: str = "local") -> dict:
    """Resume music."""
    _music().resume(_target(target))
    return {"ok": True}


@mcp.tool()
def music_stop(target: str = "local") -> dict:
    """Stop music and clear the playlist."""
    _music().stop(_target(target))
    _state().clear_music_intent()
    return {"ok": True}


@mcp.tool()
def music_volume(level: int, target: str = "local") -> dict:
    """Set music volume (0-100). For temporary ducking during speech,
    let the route coordinator handle it — this is for the listener's
    own preference.
    """
    _music().duck(_target(target), max(0, min(100, level)))
    return {"ok": True, "level": level}


@mcp.tool()
def music_now_playing(target: str = "local") -> dict:
    """Current track URI + playback position in ms."""
    t = _target(target)
    m = _music()
    return {"uri": m.now_playing_uri(t), "position_ms": m.position(t)}


@mcp.tool()
def music_seek(position_ms: int, target: str = "local") -> dict:
    """Seek the current music track to absolute position (ms)."""
    _music().seek_cur(_target(target), max(0, position_ms))
    return {"ok": True, "position_ms": position_ms}


# --- book sink controls (longform channel) --------------------------------
#
# The book channel is a *separate* player from music: its own queue, its own
# position, and durable resume-by-URI bookmarks. Speech pauses it (and
# rewinds a touch) rather than ducking. It runs as its own mpv broker on the
# local box and lazy-starts on first `book_play`.

@mcp.tool()
def book_play(uri: str, resume: bool = True, start_ms: int = -1,
              target: str = "") -> dict:
    """Play longform audio (audiobook / podcast) on the book channel.

    Use this instead of `music_play` for spoken-word you want to come back
    to: the book channel remembers where you were, and speech pauses it
    instead of talking over it. Accepts the same URIs as `music_play`
    (`yt:https://...`, http(s) streams, file paths) — a leading `yt:` is
    stripped for the underlying mpv player.

    Args:
        uri: What to play.
        resume: If True (default) and no explicit start_ms, resume from this
            URI's saved bookmark.
        start_ms: Explicit start offset (ms). -1 (default) means use the
            bookmark when `resume`, else start from the beginning.
    """
    b, st, t = _book(), _state(), _book_target(target)
    norm = normalize_uri(uri)
    # Download-first: a YouTube URL is unplayable directly on mel (datacenter IP
    # blocked). Resolve it to a cached local file, or start a phone-side fetch
    # (audiobook-fetch) that auto-plays on the book channel when it finishes.
    if library.is_youtube(norm):
        vid = library.video_id(norm)
        cached = library.cached_path(vid) if vid else None
        if cached is not None:
            norm = str(cached)
        else:
            started = library.start_fetch(norm, play=True)
            return {"ok": False, "fetching": started, "uri": norm,
                    "reason": ("downloading on phone; will auto-play when ready"
                               if started
                               else "not cached and audiobook-fetch unavailable")}
    # Save the outgoing book's place before switching away from it.
    _save_book_bookmark(b, st, t)
    if start_ms is not None and start_ms >= 0:
        start = start_ms
    elif resume:
        start = st.get_resume_pos(norm) or 0
    else:
        start = 0
    b.play(norm, t, start_ms=start)
    st.set_now_playing(sink="book", uri=norm, started_at=time.time(),
                       content_type="audiobook", target=t.name)
    st.set_book_last(norm)
    # An ad-hoc book breaks the playlist context, so `book next` won't try to
    # advance a list the listener has stepped away from.
    st.clear_playlist_active()
    return {"ok": True, "uri": norm, "resumed_from_ms": start}


@mcp.tool()
def book_resume(target: str = "") -> dict:
    """Resume the book channel. If nothing is loaded, reopen the last book
    played, at its saved bookmark."""
    b, st, t = _book(), _state(), _book_target(target)
    if b.idle(t):
        last = st.get_book_last()
        if not last:
            return {"ok": False, "reason": "no book to resume"}
        start = st.get_resume_pos(last) or 0
        b.play(last, t, start_ms=start)
        st.set_now_playing(sink="book", uri=last, started_at=time.time(),
                           content_type="audiobook", target=t.name)
        return {"ok": True, "uri": last, "resumed_from_ms": start}
    b.resume(t)
    return {"ok": True}


@mcp.tool()
def book_pause(target: str = "local") -> dict:
    """Pause the book channel and save its place."""
    b, t = _book(), _target(target)
    _save_book_bookmark(b, _state(), t)
    b.pause(t)
    return {"ok": True}


@mcp.tool()
def book_stop(target: str = "local") -> dict:
    """Stop the book channel, saving its place first so you can resume later."""
    b, st, t = _book(), _state(), _target(target)
    _save_book_bookmark(b, st, t)
    b.stop(t)
    st.clear_now_playing("book")
    st.clear_playlist_active()
    return {"ok": True}


@mcp.tool()
def book_skip(seconds: float = 30, target: str = "local") -> dict:
    """Skip the book by ±seconds (negative = back). Default +30s."""
    _book().skip(seconds, _target(target))
    return {"ok": True, "seconds": seconds}


@mcp.tool()
def book_seek(position_secs: float, target: str = "local") -> dict:
    """Seek the book to an absolute position (seconds from the start).

    Unlike `book_skip` (which moves ±relative), this jumps to a specific
    time — e.g. `position_secs=5615` for 1:33:35. Clamped to the file length.
    """
    pos = _book().seek_to(position_secs, _target(target))
    return {"ok": True, "position_ms": pos}


@mcp.tool()
def book_speed(rate: float, target: str = "local") -> dict:
    """Set book playback speed (0.25–4.0; 1.0 = normal)."""
    applied = _book().set_speed(rate, _target(target))
    return {"ok": True, "speed": applied}


@mcp.tool()
def book_now_playing(target: str = "local") -> dict:
    """What the book channel is playing — URI, position, duration, speed."""
    b, t = _book(), _target(target)
    if b.idle(t):
        return {"idle": True}
    return {
        "idle": False,
        "uri": b.now_playing_uri(t),
        "position_ms": b.position(t),
        "duration_ms": b.duration(t),
        "paused": b.paused(t),
        "speed": b.speed(t),
    }


# --- book playlists -------------------------------------------------------
#
# A book playlist is an ordered list of part URIs (chapters / episodes) with
# a remembered cursor. Within-part offset resume reuses the per-URI book
# bookmarks; the playlist only tracks which part. `book_playlist_play` opens
# the part at the cursor; `book_next`/`book_prev` step the cursor. The active
# playlist is remembered so `book_next` knows what to advance.

def _play_playlist_part(name: str, index: int, target: Target,
                        resume_part: bool = True) -> dict:
    """Open the playlist `name` at `index` on the book channel.

    Saves the outgoing book's bookmark first, points the playlist cursor at
    `index`, plays that part (resuming within it from its own bookmark when
    `resume_part`), and marks the playlist active. Shared by play/next/prev.
    """
    b, st = _book(), _state()
    item = st.get_playlist_item(name, index)
    if item is None:
        return {"ok": False, "reason": "index out of range", "index": index}
    _save_book_bookmark(b, st, target)
    uri = normalize_uri(item["uri"])
    start = (st.get_resume_pos(uri) or 0) if resume_part else 0
    b.play(uri, target, start_ms=start)
    st.set_playlist_index(name, index)
    st.set_playlist_active(name)
    st.set_now_playing(sink="book", uri=uri, started_at=time.time(),
                       content_type="audiobook", target=target.name)
    st.set_book_last(uri)
    _ensure_autoadvance_watcher()
    return {"ok": True, "playlist": name, "index": index, "uri": uri,
            "title": item["title"], "resumed_from_ms": start}


# --- book event watcher: playlist auto-advance + EOF self-heal ------------
#
# The book broker is a single long-lived mpv. One daemon thread (started the
# first time a playlist plays, and at service boot) watches its async event
# stream and reacts to two kinds of `end-file`:
#
#   reason=eof   → a part ended naturally: advance the active playlist. A
#                  user stop/skip/replace ends with reason `stop`, so manual
#                  control never auto-advances.
#   reason=error → playback broke (the resolved YouTube media URL carries a
#                  ~6h `expire=`; pausing across it, or a network drop, ends
#                  the file and leaves mpv idle with the entry still queued).
#                  Reload the last book at the live position so an expired-URL
#                  stall self-heals without a keypress. A consecutive-failure
#                  cap keeps a genuinely dead stream from hot-looping.
#
# Both live in the long-running MCP server process, which is where playlist
# playback is driven — so no separate watcher process or service is needed.

_autoadvance_thread: "threading.Thread | None" = None
_autoadvance_lock = threading.Lock()

# Self-heal tuning: stop rehealing after this many consecutive error end-files
# with no intervening settled playback (the stream is dead, not just expired);
# a clean stretch of playback resets the streak.
_HEAL_MAX_CONSECUTIVE = 3
_HEAL_RECOVERED_AFTER_S = 5.0


def _advance_after_eof() -> None:
    """Advance the active playlist one part. No-op if none is active.

    Called from the watcher thread when a part ends naturally. Walks off the
    end by clearing the active pointer (the playlist is finished) rather than
    looping.
    """
    st = _state()
    name = st.get_playlist_active()
    if not name:
        return
    pl = st.get_playlist(name)
    if pl is None:
        st.clear_playlist_active()
        return
    nxt = pl["cur_index"] + 1
    np = st.get_now_playing("book")
    t = _book_target((np or {}).get("target") or "")
    if nxt >= len(pl["items"]):
        st.clear_playlist_active()
        st.clear_now_playing("book")
        log.info("book playlist %r finished", name)
        return
    _play_playlist_part(name, nxt, t)


def _reheal_after_error(last_pos_ms: "int | None") -> bool:
    """Reload the last book at the best-known position after an error
    end-file. Prefers the live-tracked position over the saved bookmark so a
    self-heal never restarts from zero; reloads onto the same target the book
    was last playing to. Returns True if a load was issued."""
    from .sinks.book import normalize_uri

    st, b = _state(), _book()
    uri = st.get_book_last()
    if not uri:
        return False
    norm = normalize_uri(uri)
    pos = last_pos_ms if last_pos_ms and last_pos_ms > 0 else st.get_resume_pos(norm)
    np = st.get_now_playing("book")
    t = _book_target((np or {}).get("target") or "")
    log.warning("book self-heal: reloading %s at %sms on %s", norm, pos, t.name)
    b.play(norm, t, start_ms=(pos or 0))
    return True


def _autoadvance_loop() -> None:
    from .sinks import _mpv_ipc as ipc

    sock = _book()._sock
    last_pos_ms: "int | None" = None
    failures = 0
    last_load_at = time.monotonic()
    while True:
        try:
            for msg in ipc.event_stream(sock):
                if msg is None:
                    # Heartbeat: remember the live position so an error
                    # end-file can reload where we were, and clear the failure
                    # streak once playback has settled back in.
                    try:
                        pos = ipc.get_property(sock, "time-pos")
                        if pos is not None:
                            last_pos_ms = int(pos * 1000)
                            if (time.monotonic() - last_load_at) > _HEAL_RECOVERED_AFTER_S:
                                failures = 0
                    except (OSError, ipc.MpvIpcError):
                        pass
                    continue
                ev = msg.get("event")
                if ev == "start-file":
                    last_load_at = time.monotonic()
                    continue
                if ev != "end-file":
                    continue
                reason = msg.get("reason")
                if reason == "eof":
                    try:
                        _advance_after_eof()
                    except Exception:  # noqa: BLE001 — never kill the watcher
                        log.exception("book auto-advance failed")
                    failures = 0
                    continue
                if reason != "error":
                    continue  # stop/quit/redirect — never reheal
                # Error end-file: self-heal unless the stream looks truly dead.
                failures += 1
                if failures > _HEAL_MAX_CONSECUTIVE:
                    log.warning("book self-heal: giving up after %d consecutive "
                                "errors (stream looks dead)", failures - 1)
                    continue
                time.sleep(min(2.0 * failures, 8.0))  # back off a flapping net
                try:
                    if _reheal_after_error(last_pos_ms):
                        last_load_at = time.monotonic()
                except Exception:  # noqa: BLE001 — never kill the watcher
                    log.exception("book self-heal: reload failed")
        except (OSError, ipc.MpvIpcError):
            pass
        # Broker gone or never came up; back off then retry.
        time.sleep(2.0)


def _ensure_autoadvance_watcher() -> None:
    """Start the auto-advance watcher once; idempotent and thread-safe."""
    global _autoadvance_thread
    with _autoadvance_lock:
        if _autoadvance_thread is not None and _autoadvance_thread.is_alive():
            return
        _autoadvance_thread = threading.Thread(
            target=_autoadvance_loop, name="book-autoadvance", daemon=True)
        _autoadvance_thread.start()


@mcp.tool()
def book_playlist_new(name: str) -> dict:
    """Create an empty book playlist. Add parts with `book_playlist_add`."""
    created = _state().create_playlist(name, channel="book")
    return {"ok": True, "playlist": name, "created": created}


@mcp.tool()
def book_playlist_add(name: str, uris: list[str]) -> dict:
    """Append one or more part URIs (in order) to a book playlist.

    Accepts the same URIs as `book_play` (`yt:https://...`, http(s) streams,
    file paths). Creates the playlist if it doesn't exist yet.
    """
    st = _state()
    st.create_playlist(name, channel="book")  # no-op if it exists
    count = st.add_playlist_items(name, list(uris))
    return {"ok": True, "playlist": name, "count": count, "added": len(uris)}


@mcp.tool()
def book_playlist_play(name: str, resume: bool = True, target: str = "") -> dict:
    """Play a saved book playlist, resuming at its remembered part + offset.

    For spoken requests like "play my Dune audiobook" / "put on the <name>
    playlist" / "continue my book" where <name> is a saved playlist.

    Args:
        name: The playlist to play.
        resume: If True (default), start at the playlist's saved cursor and
            within that part at its saved bookmark. If False, start over from
            the first part.
    """
    st, t = _state(), _book_target(target)
    pl = st.get_playlist(name)
    if pl is None:
        return {"ok": False, "reason": f"no playlist {name!r}"}
    if not pl["items"]:
        return {"ok": False, "reason": f"playlist {name!r} is empty"}
    index = pl["cur_index"] if resume else 0
    if index >= len(pl["items"]):
        index = 0
    return _play_playlist_part(name, index, t, resume_part=resume)


@mcp.tool()
def book_next(target: str = "") -> dict:
    """Next part of the active book playlist — "next chapter", "skip ahead",
    "play the next part", "next episode"."""
    st, t = _state(), _book_target(target)
    name = st.get_playlist_active()
    if not name:
        return {"ok": False, "reason": "no active playlist"}
    pl = st.get_playlist(name)
    if pl is None:
        return {"ok": False, "reason": f"playlist {name!r} gone"}
    nxt = pl["cur_index"] + 1
    if nxt >= len(pl["items"]):
        return {"ok": False, "reason": "end of playlist", "playlist": name}
    return _play_playlist_part(name, nxt, t)


@mcp.tool()
def book_prev(target: str = "") -> dict:
    """Previous part of the active book playlist — "previous chapter",
    "go back a part", "last episode"."""
    st, t = _state(), _book_target(target)
    name = st.get_playlist_active()
    if not name:
        return {"ok": False, "reason": "no active playlist"}
    pl = st.get_playlist(name)
    if pl is None:
        return {"ok": False, "reason": f"playlist {name!r} gone"}
    prv = pl["cur_index"] - 1
    if prv < 0:
        return {"ok": False, "reason": "at start of playlist", "playlist": name}
    return _play_playlist_part(name, prv, t)


@mcp.tool()
def book_playlist_ls(name: str = "") -> dict:
    """List book playlists, or the parts of one if `name` is given."""
    st = _state()
    if not name:
        return {"playlists": st.list_playlists(channel="book")}
    pl = st.get_playlist(name)
    if pl is None:
        return {"ok": False, "reason": f"no playlist {name!r}"}
    return pl


@mcp.tool()
def book_playlist_rm(name: str) -> dict:
    """Delete a book playlist (its parts' bookmarks are kept)."""
    removed = _state().delete_playlist(name)
    return {"ok": removed, "playlist": name,
            **({} if removed else {"reason": "no such playlist"})}


# --- channel concurrency: focus + bed -------------------------------------
#
# The book and music channels can play at once (book in front, music as a
# quiet bed). `focus` chooses which is in front; `book_bed` chooses whether
# the music bed ducks (instrumental) or pauses (lyrics) under the book.

@mcp.tool()
def focus(channel: str, target: str = "local") -> dict:
    """Bring a channel to the front; push the other into its bed.

    Args:
        channel: "book" → music drops to a quiet bed (or pauses, per the
            current `book_bed` mode) and the book plays at full. "music" →
            the book pauses (its place is saved) and music returns to full.
    """
    ch = channel.strip().lower()
    if ch not in (FOCUS_BOOK, FOCUS_MUSIC):
        return {"ok": False, "reason": f"unknown channel {channel!r}"}
    b, m, st, t = _book(), _music(), _state(), _target(target)
    # Save the book's place before focus pauses it.
    if ch == FOCUS_MUSIC:
        _save_book_bookmark(b, st, t)
    result = apply_focus(ch, music=m, book=b, state=st,
                         music_target=t, book_target=t)
    return {"ok": True, **result}


@mcp.tool()
def book_bed(mode: str, target: str = "local") -> dict:
    """Set how the music bed behaves under a foregrounded book.

    Args:
        mode: "duck" — keep music playing quietly under the narration
            (good for instrumental); "pause" — pause music entirely while
            the book is in front (good for lyrics). Applies immediately if
            a book is currently in front.
    """
    md = mode.strip().lower()
    if md not in (BED_DUCK, BED_PAUSE):
        return {"ok": False, "reason": f"mode must be 'duck' or 'pause', got {mode!r}"}
    st = _state()
    st.set_book_bed(md)
    # If the book is already in front, re-apply so the change takes effect now.
    reapplied = False
    if st.get_focus() == FOCUS_BOOK:
        t = _target(target)
        apply_focus(FOCUS_BOOK, music=_music(), book=_book(), state=st,
                    music_target=t, book_target=t)
        reapplied = True
    return {"ok": True, "bed": md, "applied_now": reapplied}


@mcp.tool()
def channels_status() -> dict:
    """Both channels at a glance — what's playing, plus focus and bed mode."""
    b, m = _book(), _music()
    t = _target("local")
    pol = resolve(_state())
    book_state: dict = {"idle": b.idle(t)}
    if not book_state["idle"]:
        book_state.update(uri=b.now_playing_uri(t), position_ms=b.position(t),
                          paused=b.paused(t), speed=b.speed(t))
    return {
        "focus": pol.focus,
        "bed": pol.bed,
        "music": {"uri": m.now_playing_uri(t), "position_ms": m.position(t)},
        "book": book_state,
    }


# --- entrypoint -----------------------------------------------------------

def _configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    """stdio entrypoint — for Claude Code and other local MCP clients."""
    load_env_file("media-mcp")
    _configure_logging()
    # Watch for playlist part-ends host-wide, so a playlist started from the
    # CLI (a short-lived process that can't host the watcher) still advances.
    _ensure_autoadvance_watcher()
    mcp.run()


def main_http() -> None:
    """streamable-HTTP entrypoint — for remote callers over Tailscale."""
    load_env_file("media-mcp-http")
    _configure_logging()
    _ensure_autoadvance_watcher()
    log.info("media-mcp http listening on %s:%d", _host(), _port())
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
