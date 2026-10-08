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
keeps for a listener's own server only (``personal``): off unless
MEDIA_RADIO_YOUTUBE=1 (:func:`youtube_on`). The hand-off player —
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
    """yt-dlp's `id<TAB>title<TAB>channel<TAB>duration[<TAB>query]` lines, as
    songs; a fifth field is the search it answered (``q``, the DJ's line)."""
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not _ID.match(parts[0]):
            continue
        try:
            dur = int(float(parts[3])) if len(parts) > 3 and parts[3] not in ("", "NA") else None
        except ValueError:
            dur = None
        song = {"id": parts[0], "title": parts[1].strip(),
                "channel": (parts[2].strip() if len(parts) > 2 and parts[2] != "NA" else ""),
                "dur": dur, "yt": True}
        if len(parts) > 4 and parts[4].strip() not in ("", "NA"):
            song["q"] = parts[4].strip()
        out.append(song)
    return out


def synthetic_id(line: str) -> str:
    """An id for a song known only by name (a DJ's line, no YouTube): eleven
    characters of its hash, so it passes where a YouTube id does."""
    import base64
    import hashlib

    return base64.urlsafe_b64encode(hashlib.sha1(line.lower().encode()).digest()).decode()[:11]


def query(song: dict) -> tuple[str, str, str]:
    """``(q, artist, title)`` to ask a music app for `song`: the DJ's own
    line when there is one, else its YouTube title with the uploader's
    trimmings ("(Official Audio)", "[4K]", "- Topic") off."""
    line = (song.get("q") or "").strip()
    if not line:
        line = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", song.get("title") or "").strip()
        if " - " not in line:
            ch = re.sub(r"\s*(- Topic|VEVO|Official)$", "", song.get("channel") or "").strip()
            if ch:
                line = f"{ch} - {line}"
    artist, _, title = line.partition(" - ")
    if not title:
        artist, title = "", line
    return line, artist.strip(), title.strip()


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
                                       "idle-active", "time-pos", "duration", "pause"])
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

    def _quiet_self(self) -> None:
        try:
            sink = self._sink()
            if sink.loaded():
                sink.stop()
        except Exception:  # noqa: BLE001 — best-effort tidying
            log.debug("radio: could not quiet %s", self.where, exc_info=True)

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

    def pause(self) -> None:
        with contextlib.suppress(Exception):
            self._sink().pause()


#: The hand-off app's report, as the frames channel answers it (speech_frames.REPORT).
HANDOFF_REPORT = "user-data/agent-media/report"


class HandoffPlayer:
    """The listener's own music app, by Sasonica's hand-off player
    (sasonica-app speech/HandoffMusic.java): each song is asked of Spotify,
    YouTube Music or whichever app the listener chose, by name, under their
    subscription. Nothing is downloaded here or on the phone, so it needs no
    switch (licensed-music proposal, step 3).

    It is driven with the same mpv verbs as the phone's players; a queue
    entry is ``handoff/<id>?q=Artist - Title&artist=…&title=…``. Since 9 Oct
    2026 the endpoint is the server's own `handoff` frames channel
    (agent_media_server.speech_frames, MEDIA_HANDOFF_FRAMES_LISTEN; roadmap
    item 15, #7), which sends them down the phone's stream; it was the app's
    port 6617 on the tailnet.
    """

    personal = False

    def __init__(self, where: str = "handoff") -> None:
        self.where = where

    @staticmethod
    def endpoint() -> Optional[str]:
        return (os.environ.get("MEDIA_RADIO_HANDOFF_ENDPOINT") or "").strip() or None

    def _ep(self) -> str:
        from .sinks import _mpv_ipc as ipc

        ep = self.endpoint()
        if not ep:
            raise ipc.MpvIpcError("MEDIA_RADIO_HANDOFF_ENDPOINT unset")
        return ep

    def ready(self) -> Optional[str]:
        """None when a device runs the hand-off player, else why not (the
        frames channel answers "no device plays handoff" with none on it)."""
        from .sinks import _mpv_ipc as ipc

        try:
            ipc.command(self._ep(), "client_name", timeout=3.0, retry_errors=False)
            return None
        except ipc.MpvIpcError as e:
            why = str(e).removeprefix("client_name: ")
            if why.startswith("no device"):
                return ("no phone has the hand-off player on: open Sasonica on it "
                        "(a build with the hand-off frames)")
            return f"the hand-off player did not answer: {why}"
        except OSError as e:
            return f"the hand-off player did not answer: {e}"

    def report(self) -> Optional[dict]:
        """The app's own word: `{app, song, method, status, error, log, age_s}`
        (HandoffMusic.report), or None before it has said anything."""
        from .sinks import _mpv_ipc as ipc

        try:
            got = ipc.command(self._ep(), "get_property", HANDOFF_REPORT,
                              timeout=2.0, retry_errors=False)
        except (ipc.MpvIpcError, OSError):
            return None
        return got if isinstance(got, dict) else None

    def props(self) -> Optional[dict]:
        from .sinks import _mpv_ipc as ipc

        try:
            return ipc.get_properties(self._ep(), ["path", "playlist-pos", "playlist-count",
                                                   "idle-active", "time-pos", "duration", "pause"])
        except (ipc.MpvIpcError, OSError):
            return None

    @staticmethod
    def entry(song: dict) -> str:
        import urllib.parse

        q, artist, title = query(song)
        fields = {"q": q, "artist": artist, "title": title}
        # The app to ask, when the server names one (else the listener's
        # choice in Settings): MEDIA_RADIO_HANDOFF_APP, a package name.
        app = (os.environ.get("MEDIA_RADIO_HANDOFF_APP") or "").strip()
        if app:
            fields["app"] = app
        if song.get("yt"):
            # A real YouTube id: YouTube Music can be asked by link.
            fields["yt"] = song["id"]
        return f"handoff/{song['id']}?" + urllib.parse.urlencode(fields)

    def send(self, song: dict, replace: bool = False) -> bool:
        from .sinks import _mpv_ipc as ipc

        try:
            ipc.command(self._ep(), "loadfile", self.entry(song),
                        "replace" if replace else "append-play")
            if replace:
                # One player at a time: the phone's own stop for the app's.
                for where in ("sasonica", "phone"):
                    threading.Thread(target=PhonePlayer(where)._quiet_self, daemon=True,
                                     name="radio-quiet-phone").start()
            return True
        except (ipc.MpvIpcError, OSError) as e:
            log.info("radio: the hand-off player did not take %s: %s", song["id"], e)
            return self.holds(song["id"])

    def holds(self, vid: str) -> bool:
        from .sinks import _mpv_ipc as ipc

        try:
            entries = ipc.get_property(self._ep(), "playlist") or []
        except (ipc.MpvIpcError, OSError):
            return False
        return any(vid_of(str((e or {}).get("filename") or "")) == vid for e in entries)

    def prefetch(self, song: dict) -> bool:
        return True                # the music app fetches its own

    def label(self, song: dict) -> None:
        """The name the station knows it by, for the Media tab's now playing."""
        from .sinks import _mpv_ipc as ipc

        with contextlib.suppress(ipc.MpvIpcError, OSError):
            ipc.set_property(self._ep(), "force-media-title", query(song)[0])

    def _command(self, *args) -> None:
        from .sinks import _mpv_ipc as ipc

        with contextlib.suppress(ipc.MpvIpcError, OSError):
            ipc.command(self._ep(), *args)

    def clear(self) -> None:
        self._command("playlist-clear")

    def next(self) -> None:
        self._command("playlist-next", "weak")

    def seek(self, ms: int) -> None:
        self._command("seek", max(0.0, ms / 1000.0), "absolute")

    def pause(self) -> None:
        from .sinks import _mpv_ipc as ipc

        with contextlib.suppress(ipc.MpvIpcError, OSError):
            ipc.set_property(self._ep(), "pause", True)


#: Players a station can run on (`_resolve_music_where` names, and `handoff`).
PLAYERS = {"sasonica": PhonePlayer, "phone": PhonePlayer, "handoff": HandoffPlayer}


def handoff_ready() -> Optional[str]:
    """None when the hand-off player can take a station now, else why not."""
    if not handoff_on():
        return "no hand-off player here (MEDIA_RADIO_HANDOFF_ENDPOINT)"
    return HandoffPlayer().ready()


def handoff_on() -> bool:
    """Whether the hand-off player is configured (MEDIA_RADIO_HANDOFF_ENDPOINT)."""
    return HandoffPlayer.endpoint() is not None


def default_player(where: str) -> str:
    """The player a station goes to when none was named: MEDIA_RADIO_PLAYER,
    else the hand-off player where the YouTube path is off, else `where`
    (the music's own place)."""
    named = (os.environ.get("MEDIA_RADIO_PLAYER") or "").strip()
    if named in PLAYERS and (named != "handoff" or handoff_on()):
        return named
    if not youtube_on() and handoff_on():
        return "handoff"
    return where


def youtube_on() -> bool:
    """Whether the YouTube path may run here: MEDIA_RADIO_YOUTUBE=1.

    Off unless a server's owner turns it on for themselves (licensing
    proposal, step 2): downloading from YouTube is theirs to decide on their
    own server, and not something Sasonica offers anyone by default.
    """
    return (os.environ.get("MEDIA_RADIO_YOUTUBE") or "").strip() == "1"


def available() -> bool:
    """Whether any station can play here: the YouTube path, or the hand-off
    player (a DJ station; a Mix needs YouTube to list it)."""
    return youtube_on() or handoff_on()


def player(where: str):
    try:
        return PLAYERS[where](where)
    except KeyError:
        raise ValueError(f"no radio player {where!r}") from None
