"""`media-abs-book-bridge` — one position, two players.

The book channel plays m4b files out to the rooms through mpv; Audiobookshelf
catalogues the same files but plays them client-side, on the phone or the web.
Neither knows where the other got to, so a book you follow in both places
restarts somewhere wrong every time you swap. This daemon is the join:

  **push** (always)      while the rooms are playing, POST the position to ABS,
                         so the app shows the right resume point and "continue
                         listening" is true. The position and the length are
                         the *item's*: a book in six files is one item with one
                         progress row, and part four starts at its own offset.

  **pull** (`ABS_PULL_ON_LOAD=1`)
                         when a *new* file is loaded into the book channel and
                         ABS is ahead of it, seek there once — start on the
                         phone, send it to the rooms, carry on.

Pull is opt-in because it moves playback under you, and a position that is
merely stale (an app left open on a chapter you abandoned) would then drag the
room back to it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error

from ._abs import Abs, basename_map, load_env, log, pick_library


def mpv_get(sock: str, prop: str):
    """One property from the book channel's mpv, or None if it isn't there.

    None covers every kind of absence — no socket, nothing loaded, a timeout —
    because every caller does the same thing with all of them: wait and ask
    again. This runs beside a player it must never disturb.
    """
    if not os.path.exists(sock):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(sock)
            s.sendall((json.dumps({"command": ["get_property", prop]}) + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                for line in buf.split(b"\n"):
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if "error" in msg:
                        return msg.get("data")
    except (socket.timeout, OSError):
        return None
    return None


def mpv_set(sock: str, prop: str, value) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(sock)
            s.sendall((json.dumps({"command": ["set_property", prop, value]}) + "\n").encode())
            s.recv(65536)
    except OSError:
        pass


def should_push(prev, pos: float, poll_s: float) -> bool:
    """Whether the position has moved enough to be worth sending.

    Every poll would be a write per ten seconds per listener, forever, for a
    number that mostly has not changed — and ABS records progress history. The
    threshold is roughly one poll's worth of playback, so ordinary listening
    pushes about once a cycle and a paused book pushes nothing.
    """
    if prev is None:
        return True
    return abs(pos - prev) >= max(poll_s - 1, 3)


def tracks_of(abs_api: Abs, item_id: str) -> list:
    """The item's tracks — path, startOffset and duration each — or [].

    Only asked for a book that is a folder: the library listing has no
    filenames in it, so a multi-part book cannot be matched without this.
    """
    item = abs_api.req("GET", f"/api/items/{item_id}?expanded=1")
    return ((item.get("media") or {}).get("tracks")
            or (item.get("media") or {}).get("audioFiles") or [])


def item_position(entry: dict, pos: float, file_dur=None):
    """(position in the item, the item's length) from a position in one file.

    The unit of progress in Audiobookshelf is the item, and a book can be six
    files: part four's position is its offset plus the position in it, and the
    length is the whole book's. Sending the file's own numbers told ABS that a
    six-part book was 33 minutes long and the listener two thirds through it.
    `file_dur` (mpv's answer) is the fallback for an item ABS gave no duration
    for — which is every ordinary single-file book, unchanged.
    """
    at = pos + float(entry.get("offset") or 0.0)
    dur = entry.get("duration") or file_dur
    return at, (float(dur) if dur else None)


def pull_target(entry: dict, ct: float, file_dur=None):
    """Where to seek *this file* so the item sits at `ct` — or None.

    None when the saved position is in another part of the book: the rooms
    have the wrong file loaded and no seek can fix that, so say so and leave
    them alone rather than landing in the middle of part one.
    """
    off = float(entry.get("offset") or 0.0)
    part = entry.get("part") or file_dur
    if ct < off:
        return None
    if part and ct >= off + float(part):
        return None
    return ct - off


def build_map(abs_api: Abs, want_library: str = ""):
    """(library id, basename→item) for the whole book library, paged."""
    lib_id = pick_library(abs_api.req("GET", "/api/libraries").get("libraries", []),
                          want_library)
    if not lib_id:
        raise RuntimeError("no ABS libraries")
    out: dict = {}
    page, limit = 0, 500
    while True:
        resp = abs_api.req(
            "GET", f"/api/libraries/{lib_id}/items?limit={limit}&page={page}&minified=0")
        items = resp.get("results", resp.get("libraryItems", []))
        if not items:
            break
        out.update(basename_map(items, expand=lambda i: tracks_of(abs_api, i)))
        if len(items) < limit:
            break
        page += 1
    return lib_id, out


def main(argv=None) -> int:
    load_env()
    abs_api = Abs()
    sock = os.environ.get(
        "BOOK_MPV_SOCK",
        os.path.expanduser("~/.local/state/agent-media/sink-book.sock"))
    poll_s = float(os.environ.get("ABS_POLL_S", "10"))
    map_refresh_s = float(os.environ.get("ABS_MAP_REFRESH_S", "900"))
    pull_on_load = os.environ.get("ABS_PULL_ON_LOAD", "0") == "1"
    finish_frac = float(os.environ.get("ABS_FINISH_FRAC", "0.99"))
    want_library = os.environ.get("ABS_LIBRARY", "")

    if not abs_api.token:
        # Exit 0, not a crash: an unconfigured optional integration is a state,
        # not a failure, and a unit that restart-loops over it buries the one
        # line saying what to do about it.
        log(f"ABS_TOKEN not set in {os.environ.get('ABS_CONFIG', '~/.config/agent-media/abs-bridge.env')} — idle.")
        log("Create an API key in ABS (Settings → API Keys), add it, then "
            "restart this service.")
        return 0

    lib_id, pathmap, last_map = None, {}, 0.0
    last_pushed: dict = {}
    pulled_for: set = set()
    last_path = None

    log(f"bridge up: ABS={abs_api.url} sock={sock} poll={poll_s}s "
        f"pull_on_load={pull_on_load}")
    while True:
        try:
            now = time.time()
            if lib_id is None or now - last_map > map_refresh_s:
                lib_id, pathmap = build_map(abs_api, want_library)
                last_map = now
                log(f"mapped {len(pathmap)} audio files from library {lib_id}")

            path = mpv_get(sock, "path")
            if not path:
                last_path = None
                time.sleep(poll_s)
                continue

            base = os.path.basename(path)
            entry = pathmap.get(base)

            if path != last_path:
                last_path = path
                if entry and pull_on_load and path not in pulled_for:
                    pulled_for.add(path)
                    try:
                        prog = abs_api.req("GET", f"/api/me/progress/{entry['id']}")
                    except urllib.error.HTTPError as e:
                        # 404 is the ordinary answer for "never opened this
                        # one", not an error worth the outer handler.
                        if e.code != 404:
                            raise
                        prog = None
                    ct = (prog or {}).get("currentTime") or 0
                    pos = mpv_get(sock, "time-pos") or 0
                    # Only when mpv is effectively at the start and ABS is
                    # genuinely ahead: seeking a book someone is already
                    # listening to is worse than not seeking at all.
                    if ct > 5 and pos < 5:
                        # ABS counts from the start of the *item*; this file
                        # may be part four of six. Seek inside it only when
                        # the saved position is in fact in here — if the
                        # listener left off in another part, the rooms are
                        # playing the wrong file and a seek cannot fix that.
                        seek = pull_target(entry, ct, mpv_get(sock, "duration"))
                        if seek is None:
                            log(f"ABS is at {ct:.0f}s, outside this part of "
                                f"{base} — left the rooms where they were")
                        else:
                            mpv_set(sock, "time-pos", seek)
                            log(f"pulled ABS position {ct:.0f}s for {base}"
                                + (f" ({seek:.0f}s into this part)"
                                   if seek != ct else ""))

            if not entry:
                time.sleep(poll_s)
                continue

            pos = mpv_get(sock, "time-pos")
            if pos is None:
                time.sleep(poll_s)
                continue
            at, dur = item_position(entry, pos, mpv_get(sock, "duration"))

            if should_push(last_pushed.get(entry["id"]), at, poll_s):
                finished = bool(dur) and at >= dur * finish_frac
                body = {"currentTime": round(at, 3), "isFinished": finished}
                if dur:
                    body["duration"] = round(dur, 3)
                abs_api.req("PATCH", f"/api/me/progress/{entry['id']}", body)
                last_pushed[entry["id"]] = at
                log(f"-> ABS {base} @ {at:.0f}s/{(dur or 0):.0f}s"
                    + (" [finished]" if finished else ""))

        except urllib.error.HTTPError as e:
            log(f"ABS HTTP {e.code}: {e.reason} ({e.url})")
            if e.code in (401, 403):
                log("auth failed — check ABS_TOKEN. idling 60s.")
                time.sleep(60)
        except urllib.error.URLError as e:
            log(f"ABS unreachable: {e.reason}")
        except Exception as e:  # noqa: BLE001 — the daemon outlives its errors
            log(f"error: {e!r}")
        time.sleep(poll_s)


if __name__ == "__main__":
    sys.exit(main())
