"""Radio: one track, then more like it, until it is turned off.

docs/proposals/2026-09-29-radio.md (David, 29 Sep 2026: "I think I'd like a
radio mode that plays a playlist of similar songs", then "yes that sounds
awesome" to the first version: YouTube's Mix, and 👎).

**Where the songs come from.** Every YouTube video has a Mix, the radio
YouTube itself plays after it (``watch?v=<id>&list=RD<id>``). red5 is
bot-walled, so the phone lists it (yt-dlp ``--flat-playlist``, ~5 s, nothing
downloaded) over the same ssh as play-local. When fewer than
``_LOW_WATER`` songs are left, the list is topped up from the Mix of the
last song liked on the station, else the one playing: a Mix wanders after
twenty songs or so, and a like is the best word on where to wander.

**Staying ahead.** Every track is downloaded on the phone before it can
play, so the station keeps one track queued in the player behind the one
playing (an append, which is the download) and the one after that in the
phone's cache. A track that will not download is dropped and the next tried.

**Who drives it.** :func:`tick`, every few seconds, from the server's loop
(agent_media_server.radio). The station is one file under the state dir,
read and written under a lock, so ``media music radio`` from a shell and the
Media tab start and stop the same station.

**Only the phone's players** (Sasonica's own and the Termux mpv): both queue
with ``loadfile … append-play`` and report ``playlist-pos``. Mopidy has its
own YouTube radio, and is not this.

**Off** when something else is put on: ``media music play`` without
``--add``, and ``stop``, end the station (cli), which is how the Media tab,
a chat and the share sheet all play. A song the station has no record of
coming up is played on: it is not a guess at what the listener meant.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

from . import _lock as fcntl
from ._paths import state_dir

log = logging.getLogger(__name__)

#: Players a station can run on (`_resolve_music_where` names).
WHERES = ("sasonica", "phone")
#: Songs left before the list is topped up from another Mix.
_LOW_WATER = 5
#: How many songs of a Mix to list (YouTube's run to ~50).
_MIX_LEN = 40
#: Thumbs down on this many songs by one channel keep the channel off.
_CHANNEL_STRIKES = 2
#: How many songs the Media tab is shown.
_SHOWN = 10
#: A song the player would not take is tried this many times, then dropped.
_TRIES = 3
#: A song cut off further than this from its end is picked up again.
_RESUME_BEFORE_END_S = 8.0

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _path() -> Path:
    return state_dir() / "radio.json"


def _blank() -> dict:
    return {"on": False, "where": "", "seed": None, "started": 0.0,
            "queue": [], "sent": [], "played": [], "banned": [],
            "strikes": {}, "ready": [], "fetching": None, "current": None, "mixed": [],
            "at": None, "tries": {}}
    # queue: songs to come, in order; sent: queued in the player; played: ids
    # heard; ready: queue ids already in the phone's cache; fetching: the
    # song downloading now; strikes: 👎 per channel; mixed: ids whose Mix
    # has been listed (each once, or a dry station lists one every pass);
    # at: {id, pos, dur} of the song playing, last seen; tries: failed
    # queueings per id.


@contextlib.contextmanager
def _station() -> Iterator[dict]:
    """The station, locked: changes to the dict are written back on exit."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            try:
                st = {**_blank(), **json.loads(p.read_text())}
            except (OSError, ValueError):
                st = _blank()
            before = json.dumps(st, sort_keys=True)
            yield st
            if json.dumps(st, sort_keys=True) != before:
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(st))
                os.replace(tmp, p)
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)


def read() -> dict:
    with _station() as st:
        return dict(st)


def is_on() -> bool:
    try:
        return bool(read().get("on"))
    except OSError:
        return False


def _vid(s: str) -> Optional[str]:
    """The YouTube id in a URI, a URL or a cache path (`…/<id>.mka`)."""
    if not s:
        return None
    from .sinks.music_fetch import watch_id

    vid = watch_id(s)
    if vid:
        return vid
    stem = os.path.basename(s.split("?", 1)[0]).split(".", 1)[0]
    return stem if _ID.match(stem) else None


def _uri(vid: str) -> str:
    return f"yt:https://www.youtube.com/watch?v={vid}"


# ---- the Mix -----------------------------------------------------------------

def mix(vid: str) -> list[dict]:
    """YouTube's Mix for `vid`, listed on the phone: ``[{id, title, channel, dur}]``."""
    from .sinks import music_local

    url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
    remote = ("yt-dlp --no-warnings --flat-playlist --playlist-end "
              f"{_MIX_LEN} --print '%(id)s\t%(title)s\t%(channel)s\t%(duration)s' "
              f"{shlex.quote(url)}")
    try:
        r = subprocess.run(music_local.phone_argv(remote), capture_output=True,
                           text=True, timeout=float(os.environ.get("MEDIA_RADIO_MIX_TIMEOUT", "60")))
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("radio: listing the Mix of %s failed: %s", vid, e)
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not _ID.match(parts[0]):
            continue
        try:
            dur = int(float(parts[3])) if len(parts) > 3 and parts[3] not in ("", "NA") else None
        except ValueError:
            dur = None
        out.append({"id": parts[0], "title": parts[1].strip(),
                    "channel": (parts[2].strip() if len(parts) > 2 and parts[2] != "NA" else ""),
                    "dur": dur})
    if not out:
        log.warning("radio: the Mix of %s listed nothing: %s", vid, (r.stderr or "").strip()[-200:])
    return out


def _norm(title: str) -> str:
    """A title with its "(Official Audio)"-style tails gone, to catch the
    same song uploaded twice."""
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", title.lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _merge(st: dict, songs: list[dict]) -> int:
    """Add the songs the station has not had, and may have; how many."""
    seen = {s["id"] for s in st["queue"]} | {s["id"] for s in st["sent"]} \
        | set(st["played"]) | set(st["banned"])
    if st.get("seed"):
        seen.add(st["seed"]["id"])
    names = {_norm(s.get("title") or "") for s in st["queue"] + st["sent"]}
    names.discard("")
    struck = {c for c, n in st["strikes"].items() if n >= _CHANNEL_STRIKES}
    added = 0
    for s in songs:
        name = _norm(s.get("title") or "")
        if s["id"] in seen or (name and name in names) or (s.get("channel") or "") in struck:
            continue
        st["queue"].append(s)
        seen.add(s["id"])
        if name:
            names.add(name)
        added += 1
    return added


# ---- the player ----------------------------------------------------------------

def _sink(where: str):
    if where == "sasonica":
        from .sinks.music_sasonica import SinkMusicSasonica
        return SinkMusicSasonica()
    from .sinks.music_local import SinkMusicLocal
    return SinkMusicLocal()


def _props(where: str) -> Optional[dict]:
    from .sinks import _mpv_ipc as ipc

    sink = _sink(where)
    try:
        return ipc.get_properties(sink._endpoint(), ["path", "playlist-pos", "playlist-count",
                                                     "idle-active", "time-pos", "duration"])
    except (ipc.MpvIpcError, OSError):
        return None


def _send(where: str, song: dict, replace: bool = False) -> bool:
    """Queue (or play) `song` in the player; this is its download."""
    from .types import Target

    sink = _sink(where)
    try:
        took = sink.play(_uri(song["id"]), Target(name=where), replace=replace)
    except Exception as e:  # noqa: BLE001 — a song that will not come is skipped
        log.info("radio: %s did not play: %s", song["id"], e)
        return False
    # SinkMusicSasonica says False when it could not; the Termux mpv raises.
    return took is not False


def _prefetch(song: dict) -> bool:
    """Download `song` into the phone's cache without playing it."""
    from .sinks import music_local, music_sasonica

    if music_local._phone_cached_path(song["id"]):
        return True
    return music_sasonica._phone_fetch(_uri(song["id"]).removeprefix("yt:")) is not None


def _label(where: str, song: dict) -> None:
    """The app's player keeps one title, art and chapter list, and clears
    them only on a replace; a queued song that comes up is given its own."""
    if where != "sasonica":
        return
    from .music_recent import art
    from .sinks import music_sasonica
    from .sinks import _mpv_ipc as ipc

    sink = music_sasonica.SinkMusicSasonica()
    try:
        if song.get("title"):
            sink._set("force-media-title", song["title"])
        picture = art(song["id"])
        if picture:
            sink._set("user-data/agent-media/art", picture)
        sink._set("user-data/agent-media/chapters", [])
    except (ipc.MpvIpcError, OSError):
        pass


def _note(song: dict) -> None:
    """What is playing, for `media music status` and Recently played."""
    try:
        from .state import StateStore

        # Also the history row Recently played lists (StateStore docs).
        StateStore().set_music_intent(_uri(song["id"]), "music", song.get("title") or None)
    except Exception:  # noqa: BLE001 — a label, not the music
        log.debug("radio: could not note %s", song.get("id"), exc_info=True)


# ---- the verbs -----------------------------------------------------------------

def start(uri: str, where: str, playing: bool) -> dict:
    """A station from `uri`'s Mix, on `where`. `playing`: `uri` is already on
    (the station is built behind it), else the caller puts it on first."""
    vid = _vid(uri)
    if not vid:
        raise ValueError("radio needs a YouTube track")
    if where not in WHERES:
        raise ValueError("radio plays on the phone")
    songs = mix(vid)
    if not songs:
        raise RuntimeError("YouTube's Mix for this track could not be listed")
    seed = next((s for s in songs if s["id"] == vid), {"id": vid, "title": "", "channel": ""})
    with _station() as st:
        st.clear()
        st.update(_blank())
        st.update(on=True, where=where, seed=seed, started=time.time(), current=vid,
                  played=[vid], mixed=[vid])
        _merge(st, songs)
    if playing:
        # What else was queued behind the seed makes way for the station.
        from .sinks import _mpv_ipc as ipc
        with contextlib.suppress(ipc.MpvIpcError, OSError):
            ipc.command(_sink(where)._endpoint(), "playlist-clear")
    return snapshot()


def stop() -> None:
    """Turn the station off; what is playing plays on."""
    with _station() as st:
        if st["on"]:
            st["on"] = False


def dislike() -> Optional[dict]:
    """👎: this song is off the station, a channel twice is too, and the
    next song plays. None when no station is on."""
    with _station() as st:
        if not st["on"]:
            return None
        where, cur = st["where"], st.get("current")
        song = next((s for s in st["sent"] + [st.get("seed") or {}] if s.get("id") == cur), None)
        if cur and cur not in st["banned"]:
            st["banned"].append(cur)
        ch = (song or {}).get("channel") or ""
        if ch:
            st["strikes"][ch] = st["strikes"].get(ch, 0) + 1
            if st["strikes"][ch] >= _CHANNEL_STRIKES:
                st["queue"] = [s for s in st["queue"] if s.get("channel") != ch]
    _skip(where)
    return snapshot()


def play(vid: str) -> Optional[dict]:
    """A song from the list, now (a tap on Up next): the songs before it stay
    to come. None when no station is on; ValueError when `vid` is not on the
    list, RuntimeError when the player would not take it."""
    with _station() as st:
        if not st["on"]:
            return None
        where, cur = st["where"], st.get("current")
        if not any(s["id"] == vid for s in st["sent"] + st["queue"]
                   if s["id"] not in st["played"]):
            raise ValueError("that song is not on the station's list")
        # A replace clears the player's queue: what was queued behind the
        # song playing goes back to the front of the list (downloaded).
        ids = [s["id"] for s in st["sent"]]
        after = st["sent"][ids.index(cur) + 1:] if cur in ids else \
            [s for s in st["sent"] if s["id"] not in st["played"]]
        st["sent"] = [s for s in st["sent"] if s not in after]
        st["queue"][:0] = after
        st["ready"].extend(s["id"] for s in after if s["id"] not in st["ready"])
        song = next(s for s in st["queue"] if s["id"] == vid)
        at = st["queue"].index(song)
        st["queue"].remove(song)
        st["fetching"] = song
    took = _send(where, song, replace=True)
    with _station() as st:
        st["fetching"] = None
        if took:
            st["sent"].append(song)
            st["current"] = song["id"]
            st["played"].append(song["id"])
            st["at"] = None
            if song["id"] in st["ready"]:
                st["ready"].remove(song["id"])
        else:
            st["queue"].insert(min(at, len(st["queue"])), song)
    if not took:
        raise RuntimeError("the player would not take that song")
    _label(where, song)
    _note(song)
    return snapshot()


def _skip(where: str) -> None:
    """To the next song: the queued one, else the next on the list, now."""
    from .sinks import _mpv_ipc as ipc

    p = _props(where) or {}
    pos, count = p.get("playlist-pos"), p.get("playlist-count")
    if isinstance(pos, int) and isinstance(count, int) and pos + 1 < count:
        with contextlib.suppress(ipc.MpvIpcError, OSError):
            ipc.command(_sink(where)._endpoint(), "playlist-next", "weak")
        return
    for _ in range(3):
        with _station() as st:
            if not st["queue"]:
                return
            song = st["queue"].pop(0)
            st["fetching"] = song
        took = _send(where, song, replace=True)
        with _station() as st:
            st["fetching"] = None
            if took:
                st["sent"].append(song)
                st["current"] = song["id"]
                st["played"].append(song["id"])
        if took:
            _note(song)
            return


def tick() -> None:
    """One look at the player: follow what came up, stay a song ahead, and
    top the list up. Blocks while a song downloads."""
    with _station() as st:
        if not st["on"]:
            return
        where = st["where"]
    p = _props(where)
    if p is None:
        return
    cur = None if p.get("idle-active") else _vid(str(p.get("path") or ""))
    pos, count = p.get("playlist-pos"), p.get("playlist-count")
    ahead = count - pos - 1 if isinstance(pos, int) and isinstance(count, int) and pos >= 0 else 0
    t, dur = p.get("time-pos"), p.get("duration")

    # Only an empty player lost its song: idle with a queue is between two.
    if cur is None and not count and _resume(where):
        return

    came_up = None
    with _station() as st:
        if not st["on"]:
            return
        if cur and cur != st.get("current"):
            # A song it has no record of is played on, not taken for the
            # listener's: a play of something else ends the station where
            # it is asked for (cli: play, stop), and a canvas restarted in
            # the middle of a queueing loses the record of a song the player
            # got — twice the station turned itself off on its own songs
            # (29 Sep 2026).
            # One of the list's own that the player had after all (a resume
            # took the player for empty): heard, not to come.
            for s in [s for s in st["queue"] if s["id"] == cur]:
                st["queue"].remove(s)
                st["sent"].append(s)
            st["current"] = cur
            if cur not in st["played"]:
                st["played"].append(cur)
            came_up = next((s for s in st["sent"] if s["id"] == cur), None)
        if cur and isinstance(t, (int, float)) and t > 0:
            st["at"] = {"id": cur, "pos": round(float(t), 1),
                        "dur": float(dur) if isinstance(dur, (int, float)) else None}
    if came_up:
        _label(where, came_up)
        _note(came_up)

    if ahead < 1:
        with _station() as st:
            if not st["on"] or not st["queue"]:
                song = None
            else:
                song = st["queue"].pop(0)
                st["fetching"] = song
        if song:
            # `append-play` also starts a player that ran dry.
            took = _send(where, song)
            with _station() as st:
                st["fetching"] = None
                if took:
                    st["sent"].append(song)
                    if song["id"] in st["ready"]:
                        st["ready"].remove(song["id"])
                else:
                    # A slow phone refuses a command as readily as YouTube
                    # refuses a download: try again before giving it up.
                    n = st["tries"][song["id"]] = st["tries"].get(song["id"], 0) + 1
                    if n < _TRIES:
                        st["queue"].insert(0, song)
            return

    with _station() as st:
        nxt = st["queue"][0] if st["on"] and st["queue"] else None
        low = st["on"] and len(st["queue"]) < _LOW_WATER
        ready = nxt is None or nxt["id"] in st["ready"]
    if nxt and not ready:
        with _station() as st:
            st["fetching"] = nxt
        ok = _prefetch(nxt)
        with _station() as st:
            st["fetching"] = None
            if ok:
                st["ready"].append(nxt["id"])
            else:
                st["queue"] = [s for s in st["queue"] if s["id"] != nxt["id"]]
        return
    if low:
        _refill()


def _resume(where: str) -> bool:
    """The player lost the song it was playing part-way (the app updated or
    restarted, and its player came back empty): put it on again where it
    was. False when it had played out, or cannot be."""
    with _station() as st:
        at, cur = st.get("at") or {}, st.get("current")
        if not st["on"] or not cur or at.get("id") != cur:
            return False
        if at.get("dur") and at["pos"] > at["dur"] - _RESUME_BEFORE_END_S:
            return False
        song = next((s for s in st["sent"] + [st.get("seed") or {}] if s.get("id") == cur), None)
        st["at"] = None
        # The player's queue went with it: what was queued behind the song
        # comes back to the front of the list (downloaded already).
        ids = [s["id"] for s in st["sent"]]
        if song and cur in ids:
            lost = st["sent"][ids.index(cur) + 1:]
            del st["sent"][ids.index(cur) + 1:]
            st["queue"][:0] = lost
            st["ready"].extend(s["id"] for s in lost if s["id"] not in st["ready"])
        elif song:
            lost = [s for s in st["sent"] if s["id"] not in st["played"]]
            st["sent"] = [s for s in st["sent"] if s["id"] in st["played"]]
            st["queue"][:0] = lost
            st["ready"].extend(s["id"] for s in lost if s["id"] not in st["ready"])
    if not song:
        return False
    log.info("radio: %s was cut off at %.0fs; on again", cur, at["pos"])
    if not _send(where, song, replace=True):
        return False
    _label(where, song)
    with contextlib.suppress(Exception):
        _sink(where).seek_cur(position_ms=int(at["pos"] * 1000))
    return True


def _refill() -> None:
    with _station() as st:
        if not st["on"]:
            return
        played = [v for v in st["played"] if v not in st["mixed"] and v not in st["banned"]]
        if not played:
            return
        liked = _liked_ids()
        base = next((v for v in reversed(played) if v in liked), played[-1])
        st["mixed"].append(base)
    songs = mix(base)
    with _station() as st:
        if st["on"]:
            n = _merge(st, songs)
            log.info("radio: +%d from the Mix of %s", n, base)


def _liked_ids() -> set:
    try:
        from .state import StateStore

        return {_vid(r.get("media_id") or "") or r.get("media_id")
                for r in StateStore().list_likes(limit=200, channel="music")}
    except Exception:  # noqa: BLE001
        return set()


def snapshot() -> dict:
    """For the Media tab: ``{on, seed, next: [{id, title, channel, state}]}``,
    `state` "ready" (queued or downloaded), "fetching", or null."""
    st = read()
    if not st["on"]:
        return {"on": False, "seed": None, "next": []}
    cur = st.get("current")
    ids = [s["id"] for s in st["sent"]]
    after = st["sent"][ids.index(cur) + 1:] if cur in ids else \
        [s for s in st["sent"] if s["id"] not in st["played"]]
    rows = [{**_row(s), "state": "ready"} for s in after]
    fetching = st.get("fetching") or {}
    if fetching and all(s["id"] != fetching.get("id") for s in st["queue"]):
        rows.append({**_row(fetching), "state": "fetching"})
    for s in st["queue"]:
        state = "fetching" if s["id"] == fetching.get("id") else \
            "ready" if s["id"] in st["ready"] else None
        rows.append({**_row(s), "state": state})
    seed = st.get("seed") or {}
    return {"on": True, "seed": {"id": seed.get("id"), "title": seed.get("title") or ""},
            "next": rows[:_SHOWN], "more": max(0, len(rows) - _SHOWN)}


def _row(s: dict) -> dict:
    return {"id": s["id"], "title": s.get("title") or "", "channel": s.get("channel") or ""}
