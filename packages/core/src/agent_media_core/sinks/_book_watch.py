"""sink-book EOF self-heal watcher.

A long YouTube audiobook plays from a resolved googlevideo URL that carries
a time-limited `expire=` (~6h out). If the book is paused across that expiry,
or the network drops, mpv reaches `end-file` with `reason=error` and goes
idle — leaving the playlist entry queued at `playlist-pos -1` but nothing
playing. Without intervention the channel just sits there (and the popup used
to mislabel it "loading…"); recovering meant a manual `book resume`.

This watcher subscribes to the book broker's async event stream and, on an
*error* end-file, re-issues the load at the last-known position — turning an
expired-URL stall into a self-healing blip. It deliberately ignores `eof`
(book finished), `stop`/`quit` (user stopped), and `redirect` (mpv's normal
ytdl indirection); only `error` triggers a reheal. A consecutive-failure cap
keeps a permanently-dead URL from hot-looping.

One watcher per broker: an flock on `sink-book.watch.lock` makes duplicate
spawns (every `book play` calls `_ensure_broker`) harmless no-ops. The
watcher exits when the socket closes (broker gone), releasing the lock.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from typing import Optional

from .._paths import state_dir
from ..state import StateStore
from ..types import Target
from . import _mpv_ipc as ipc
from .book import SinkBook, normalize_uri

log = logging.getLogger(__name__)

# Give up rehealing after this many consecutive error end-files with no
# intervening successful playback — i.e. the URL/stream is genuinely dead,
# not just expired. A clean stretch of playback resets the counter.
_MAX_CONSECUTIVE = 3
# Minimum seconds of progress that counts as "playback recovered" and clears
# the failure streak.
_RECOVERED_AFTER_S = 5.0


def _lock(path) -> Optional[int]:
    """Acquire an exclusive non-blocking flock. Returns the fd, or None if
    another watcher already holds it."""
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _reheal(book: SinkBook, state: StateStore, last_pos_ms: Optional[int]) -> bool:
    """Re-load the last book at the best-known position. Returns True if a
    load was issued."""
    uri = state.get_book_last()
    if not uri:
        return False
    norm = normalize_uri(uri)
    # Prefer the live position we were tracking; fall back to the saved
    # bookmark so we never restart from zero on a reheal.
    pos = last_pos_ms if last_pos_ms and last_pos_ms > 0 else state.get_resume_pos(norm)
    # Re-load onto the same target the book was last playing to (local vs
    # rooms), so a self-heal doesn't silently move rooms audio to mel.
    np = state.get_now_playing("book")
    target = Target(name=(np or {}).get("target") or "local")
    log.warning("sink-book self-heal: reloading %s at %sms on %s",
                norm, pos, target.name)
    book.play(norm, target, start_ms=(pos or 0))
    return True


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("MEDIA_BOOK_WATCH_LOG", "WARNING").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sock = state_dir() / "sink-book.sock"
    lockfd = _lock(state_dir() / "sink-book.watch.lock")
    if lockfd is None:
        return 0  # another watcher already on duty

    book, state = SinkBook(), StateStore()
    last_pos_ms: Optional[int] = None
    failures = 0
    last_load_at = time.monotonic()

    try:
        for ev in ipc.event_stream(sock):
            if ev is None:
                # Heartbeat: remember where we are so a sudden error end-file
                # can reload at the live position, not the last saved bookmark.
                try:
                    pos = ipc.get_property(sock, "time-pos")
                    if pos is not None:
                        last_pos_ms = int(pos * 1000)
                        if (time.monotonic() - last_load_at) > _RECOVERED_AFTER_S:
                            failures = 0   # we're playing fine again
                except (ipc.MpvIpcError, OSError):
                    pass
                continue

            name = ev.get("event")
            if name == "start-file":
                last_load_at = time.monotonic()
                continue
            if name != "end-file":
                continue

            reason = ev.get("reason")
            if reason != "error":
                # eof (finished) / stop / quit / redirect — never reheal.
                if reason in ("eof", "stop", "quit"):
                    failures = 0
                continue

            failures += 1
            if failures > _MAX_CONSECUTIVE:
                log.warning("sink-book self-heal: giving up after %d consecutive "
                            "errors (stream looks dead)", failures - 1)
                break
            # Back off a touch so a flapping network doesn't spin.
            time.sleep(min(2.0 * failures, 8.0))
            try:
                if not _reheal(book, state, last_pos_ms):
                    break  # nothing to reheal to
                last_load_at = time.monotonic()
            except Exception as e:  # noqa: BLE001
                log.warning("sink-book self-heal: reload failed: %s", e)
    finally:
        try:
            os.close(lockfd)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
