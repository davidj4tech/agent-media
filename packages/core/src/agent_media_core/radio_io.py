"""Where a radio station's songs come from, and what plays them.

docs/proposals/2026-09-29-licensed-music.md, step 1 (David, 29 Sep 2026:
"So how do we get around copyright issues with all of this?"). The station
(agent_media_core.radio) keeps the list, 👎, the likes and Up next; what it
asks of the outside world goes through two small seams, so a source or a
player can be swapped without touching it:

**A source** lists songs: ``more(station) -> (songs, note)``, songs as
``{id, title, channel, dur}``.

- ``mix``: YouTube's Mix for a song, listed on the phone (yt-dlp).
- ``dj``: a model's picks for the moment, found on YouTube (radio_dj).

**A player** plays them: ``props()`` in mpv's words (path, playlist-pos,
playlist-count, idle-active, time-pos, duration), ``send(song, replace)``,
``holds(id)``, ``prefetch(song)``, ``label(song)``, ``clear()``, ``next()``,
``seek(ms)``.

- ``sasonica`` / ``phone``: the phone's own players (Sasonica's Media3 and
  the Termux mpv), each song downloaded from YouTube by yt-dlp on the phone.

Everything here today is the **YouTube path**, which the licensing proposal
keeps for a listener's own server only (``personal``). The hand-off player —
the listener's own music app, by Android's play-from-search — will be one
more player beside these.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import subprocess
import threading
from typing import Optional

log = logging.getLogger(__name__)

#: How many songs of a Mix to list (YouTube's run to ~50).
MIX_LEN = 40

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def vid_of(s: str) -> Optional[str]:
    """The YouTube id in a URI, a URL or a cache path (`…/<id>.mka`)."""
    if not s:
        return None
    from .sinks.music_fetch import watch_id

    vid = watch_id(s)
    if vid:
        return vid
    stem = os.path.basename(s.split("?", 1)[0]).split(".", 1)[0]
    return stem if _ID.match(stem) else None


def yt_uri(vid: str) -> str:
    return f"yt:https://www.youtube.com/watch?v={vid}"


def parse(text: str) -> list[dict]:
    """yt-dlp's `id<TAB>title<TAB>channel<TAB>duration` lines, as songs."""
    out = []
    for line in text.splitlines():
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
    return out


# ---- sources -------------------------------------------------------------------

def youtube_mix(vid: str) -> list[dict]:
    """YouTube's Mix for `vid`, listed on the phone: ``[{id, title, channel, dur}]``."""
    from .sinks import music_local

    url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
    remote = ("yt-dlp --no-warnings --flat-playlist --playlist-end "
              f"{MIX_LEN} --print '%(id)s\t%(title)s\t%(channel)s\t%(duration)s' "
              f"{shlex.quote(url)}")
    try:
        r = subprocess.run(music_local.phone_argv(remote), capture_output=True,
                           text=True, timeout=float(os.environ.get("MEDIA_RADIO_MIX_TIMEOUT", "60")))
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("radio: listing the Mix of %s failed: %s", vid, e)
        return []
    out = parse(r.stdout or "")
    if not out:
        log.warning("radio: the Mix of %s listed nothing: %s", vid, (r.stderr or "").strip()[-200:])
    return out


# ---- players -------------------------------------------------------------------

class PhonePlayer:
    """The phone's own players, fed from YouTube by yt-dlp on the phone.

    Queues with ``loadfile … append-play`` (the queueing is the download) and
    reports ``playlist-pos``: Sasonica's Media3 player (``sasonica``) and the
    Termux mpv (``phone``) answer the same mpv verbs.
    """

    #: The YouTube path: a listener's own server only (licensing proposal).
    personal = True

    def __init__(self, where: str) -> None:
        self.where = where

    def _sink(self):
        if self.where == "sasonica":
            from .sinks.music_sasonica import SinkMusicSasonica
            return SinkMusicSasonica()
        from .sinks.music_local import SinkMusicLocal
        return SinkMusicLocal()

    def props(self) -> Optional[dict]:
        from .sinks import _mpv_ipc as ipc

        try:
            return ipc.get_properties(self._sink()._endpoint(),
                                      ["path", "playlist-pos", "playlist-count",
                                       "idle-active", "time-pos", "duration"])
        except (ipc.MpvIpcError, OSError):
            return None

    def playing(self) -> Optional[str]:
        """The id of the song loaded, if any."""
        p = self.props()
        if p and not p.get("idle-active"):
            return vid_of(str(p.get("path") or ""))
        return None

    def send(self, song: dict, replace: bool = False) -> bool:
        """Queue (or play) `song`; this is its download."""
        from .types import Target

        try:
            took = self._sink().play(yt_uri(song["id"]), Target(name=self.where), replace=replace)
        except Exception as e:  # noqa: BLE001 — a song that will not come is skipped
            log.info("radio: %s did not play: %s", song["id"], e)
            took = False
        # SinkMusicSasonica says False when it could not; the Termux mpv raises.
        # Over a slow link the load can land and its answer time out: the first
        # DJ station queued A-Punk three times that way, then forgot it
        # (29 Sep 2026). The player's own list says whether it was taken.
        if took is False and self.holds(song["id"]):
            log.info("radio: %s was taken after all", song["id"])
            took = True
        if took is not False and replace:
            threading.Thread(target=self._quiet_other, daemon=True,
                             name="radio-quiet-other").start()
        return took is not False

    def holds(self, vid: str) -> bool:
        """Whether `vid` is in the player's list (a load whose answer was lost)."""
        from .sinks import _mpv_ipc as ipc

        try:
            entries = ipc.get_property(self._sink()._endpoint(), "playlist") or []
        except (ipc.MpvIpcError, OSError):
            return False
        return any(vid_of(str((e or {}).get("filename") or "")) == vid for e in entries)

    def _quiet_other(self) -> None:
        """One player at a time, as music_router's play keeps it: a song put on
        in one phone player stops what the other was playing."""
        other = PhonePlayer("phone" if self.where == "sasonica" else "sasonica")
        try:
            sink = other._sink()
            if sink.loaded():
                sink.stop()
        except Exception:  # noqa: BLE001 — best-effort tidying
            log.debug("radio: could not quiet %s", other.where, exc_info=True)

    def prefetch(self, song: dict) -> bool:
        """Download `song` into the phone's cache without playing it."""
        from .sinks import music_local, music_sasonica

        if music_local._phone_cached_path(song["id"]):
            return True
        return music_sasonica._phone_fetch(yt_uri(song["id"]).removeprefix("yt:")) is not None

    def label(self, song: dict) -> None:
        """The app's player keeps one title, art and chapter list, and clears
        them only on a replace; a queued song that comes up is given its own."""
        if self.where != "sasonica":
            return
        from .music_recent import art
        from .sinks import _mpv_ipc as ipc

        sink = self._sink()
        try:
            if song.get("title"):
                sink._set("force-media-title", song["title"])
            picture = art(song["id"])
            if picture:
                sink._set("user-data/agent-media/art", picture)
            sink._set("user-data/agent-media/chapters", [])
        except (ipc.MpvIpcError, OSError):
            pass

    def _command(self, *args) -> None:
        from .sinks import _mpv_ipc as ipc

        with contextlib.suppress(ipc.MpvIpcError, OSError):
            ipc.command(self._sink()._endpoint(), *args)

    def clear(self) -> None:
        """Everything but the song playing leaves the queue."""
        self._command("playlist-clear")

    def next(self) -> None:
        self._command("playlist-next", "weak")

    def seek(self, ms: int) -> None:
        with contextlib.suppress(Exception):
            self._sink().seek_cur(position_ms=int(ms))


#: Players a station can run on (`_resolve_music_where` names).
PLAYERS = {"sasonica": PhonePlayer, "phone": PhonePlayer}


def player(where: str):
    try:
        return PLAYERS[where](where)
    except KeyError:
        raise ValueError(f"no radio player {where!r}") from None
