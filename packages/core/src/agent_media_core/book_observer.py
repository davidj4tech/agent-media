"""book_observer: keep book-channel resume bookmarks correct no matter who
drives the mpv.

The book channel's mpv (sink-book.sock) can be loaded by agent-media itself
(`media book play`, the popup) *or* by an external IPC client — notably the
dedicated "books" Mopidy/Iris instance, whose Mopidy-Mpv backend attaches to
the same socket. agent-media only saves/restores bookmarks on its *own*
transport calls, so Iris-initiated playback would otherwise start from zero and
never persist a position.

This daemon attaches a second, read-mostly IPC client to the book socket and:

  * **saves** the current position to the resume-bookmark store every few
    seconds (and on file change/end), so the book channel always remembers
    where you are — whoever started it; and
  * **restores** on a fresh external load: when a new file is loaded that
    agent-media did *not* initiate (no matching load-intent breadcrumb), it
    seeks to that URI's saved bookmark and updates now_playing/book_last so the
    rest of agent-media (coordinator, `book resume`, `book now`) stays in sync.

agent-media's own loads carry a load-intent marker (SinkBook.play writes it),
so we never fight the start offset it already applied (including --no-resume).
"""

from __future__ import annotations

import json
import logging
import signal
import time

from .sinks import _mpv_ipc as ipc
from .sinks.book import (
    SinkBook,
    normalize_uri,
    load_intent_path,
)
from .state import StateStore

log = logging.getLogger(__name__)

# How long a load-intent breadcrumb is considered to describe the load we're
# now seeing. Generous: covers yt-dlp/network stalls between play() and
# file-loaded, while still expiring stale markers.
_INTENT_FRESH_S = 30.0
# Don't bother resuming to a bookmark only a few seconds in.
_MIN_RESUME_MS = 3000
# Position-save cadence (also the IPC heartbeat / stop-flag check interval).
_SAVE_EVERY_S = 5.0

_stop = False


def _on_signal(_signum, _frame):
    global _stop
    _stop = True


def _read_intent() -> dict | None:
    try:
        raw = load_intent_path().read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _clear_intent() -> None:
    try:
        load_intent_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _current(sock) -> tuple[str | None, float | None]:
    """(path, time-pos seconds) from a one-shot query; (None, None) if idle."""
    try:
        path = ipc.get_property(sock, "path")
        pos = ipc.get_property(sock, "time-pos")
    except (ipc.MpvIpcError, OSError):
        return None, None
    return (path or None), (pos if isinstance(pos, (int, float)) else None)


def _save_pos(state: StateStore, path: str | None, pos: float | None) -> None:
    if not path or pos is None or pos <= 0:
        return
    try:
        state.set_resume_pos(normalize_uri(path), int(pos * 1000))
    except Exception:  # noqa: BLE001
        log.debug("save_pos failed", exc_info=True)


def _seek_to(sock, secs: float) -> None:
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            ipc.set_property(sock, "time-pos", secs)
            return
        except (ipc.MpvIpcError, OSError):
            time.sleep(0.15)


def _handle_load(sock, state: StateStore) -> None:
    """A new file just loaded. Resume it from its bookmark unless agent-media
    initiated the load (and thus already applied the right start offset)."""
    path = None
    # mpv may report `path` a beat after file-loaded; retry briefly.
    for _ in range(10):
        try:
            path = ipc.get_property(sock, "path")
        except (ipc.MpvIpcError, OSError):
            path = None
        if path:
            break
        time.sleep(0.1)
    if not path:
        return
    norm = normalize_uri(path)

    intent = _read_intent()
    if intent and intent.get("uri") == norm \
            and (time.time() - float(intent.get("ts", 0))) < _INTENT_FRESH_S:
        # Our own load — it already seeked (or deliberately didn't). Hands off.
        _clear_intent()
        log.info("load is agent-media-initiated: %s (no auto-resume)", norm)
        return

    _clear_intent()  # stale/foreign marker
    resume_ms = state.get_resume_pos(norm) or 0
    if resume_ms >= _MIN_RESUME_MS:
        log.info("external load %s — resuming at %dms", norm, resume_ms)
        _seek_to(sock, resume_ms / 1000.0)
    else:
        log.info("external load %s — no bookmark, from start", norm)
    # Make the rest of agent-media aware of what's now playing.
    try:
        state.set_now_playing(sink="book", uri=norm, started_at=time.time(),
                              content_type="audiobook")
        state.set_book_last(norm)
        # We are here *because* the file just loaded, so mpv already knows its
        # name — the one moment where the title costs nothing to fetch. Without
        # it an externally-opened book lists as its filename forever.
        try:
            title = str(ipc.get_property(sock, "media-title") or "").strip()
            if title:
                state.set_history_title("book", norm, title)
        except Exception:  # noqa: BLE001
            log.debug("media-title read failed", exc_info=True)
    except Exception:  # noqa: BLE001
        log.debug("now_playing update failed", exc_info=True)


def _run_once(book: SinkBook, state: StateStore) -> None:
    """One connected session; returns when the socket closes."""
    sock = book._sock  # the resolved sink-book.sock path
    last_path, last_pos = _current(sock)
    log.info("attached to book socket %s", sock)
    last_save = time.time()
    for evt in ipc.event_stream(sock, heartbeat=_SAVE_EVERY_S):
        if _stop:
            _save_pos(state, last_path, last_pos)
            return
        if evt is None:
            # heartbeat: snapshot + persist current position
            path, pos = _current(sock)
            if path:
                last_path, last_pos = path, pos
            _save_pos(state, last_path, last_pos)
            last_save = time.time()
            continue
        name = evt.get("event")
        if name in ("end-file", "start-file"):
            # Persist the outgoing book's last known spot before it changes.
            _save_pos(state, last_path, last_pos)
        if name == "file-loaded":
            _handle_load(sock, state)
            last_path, last_pos = _current(sock)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    book, state = SinkBook(), StateStore()
    log.info("book_observer starting")
    while not _stop:
        sock = book._sock
        if not sock.exists():
            time.sleep(2.0)
            continue
        try:
            _run_once(book, state)
        except (ipc.MpvIpcError, OSError) as e:
            log.info("book socket closed (%s); will reattach", e)
        except Exception:  # noqa: BLE001
            log.exception("book_observer loop error; reattaching")
        if not _stop:
            time.sleep(2.0)
    log.info("book_observer stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
