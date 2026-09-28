"""sink-music-sasonica: music played by Sasonica's own media player.

The `sasonica` music target. Sasonica (``com.sasonica.app``) runs a second
Media3 player beside its speech player, on its own port (6615), answering
mpv's JSON IPC the way the speech one does (sasonica-app ``Media3Music``). So
this is the phone-mpv backend (``SinkMusicLocal``) with a different endpoint
and one different verb:

  - **play** cannot hand the app a path in Termux's cache, or run yt-dlp on
    it. A YouTube URI is fetched on the phone (play-local ``--fetch-only``,
    skipped when the phone already has it) and handed over as a loopback URL
    on the phone's own music-files service (deploy/phone/service/music-files,
    127.0.0.1:6616). The ``abs`` route (copy it to red5, serve it back) is
    the fallback: over the phone's link it moved ~8 KB/s on 28 Sep 2026, so
    a 52 MB mix would have taken an hour to start.
  - **next / previous** move by chapter inside a mix (a DJ set is one file
    of many tracks): ExoPlayer does not read Matroska chapters, so they are
    read on the phone (``ffprobe``, over the same ssh as its titles) and
    kept per file. Past the last chapter, or in a file without any, they are
    the playlist's.
  - **everything else** (pause, seek, speed, volume, the observation reads
    the coordinator follows) is the mpv IPC it inherits.

The app takes ordinary media audio focus, and its speech takes transient
focus, so a reply pauses the music inside the phone with no round trip here.
The coordinator's own duck still reaches it as a volume write, on the app's
0-100 scale rather than the Termux mpv's 0-170.

Why a second app player rather than Sasonica ABS's: docs/proposals/
2026-09-28-music-tab.md (David, 28 Sep 2026).

Config: ``MEDIA_MUSIC_SASONICA_ENDPOINT``, e.g. ``tcp://p8a:6615``. Unset,
the target is not offered. ``MEDIA_MUSIC_SASONICA_FILES`` is the music-files
base the app reads from (default ``http://localhost:6616``: the app permits
cleartext to ``localhost`` by name, not to 127.0.0.1), or ``off`` to
use the red5 route only.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import urllib.parse
from typing import Optional

from ..types import Target
from . import _mpv_ipc as ipc
from . import music_app, music_fetch, music_local
from .music_local import SinkMusicLocal, _note_title

log = logging.getLogger(__name__)

SASONICA_TARGET = Target(name="sasonica")


def endpoint() -> Optional[str]:
    ep = os.environ.get("MEDIA_MUSIC_SASONICA_ENDPOINT", "").strip()
    return ep or None


def configured() -> bool:
    return endpoint() is not None


def files_base() -> Optional[str]:
    base = os.environ.get("MEDIA_MUSIC_SASONICA_FILES", "http://localhost:6616").strip()
    return None if base.lower() in ("", "off", "0", "no") else base.rstrip("/")


_CHAPTERS: dict[str, list[dict]] = {}


def phone_path(url: str) -> Optional[str]:
    """The phone's own path for a music-files URL, else None."""
    base = files_base()
    if not base or not url or not url.startswith(base + "/"):
        return None
    name = urllib.parse.unquote(url[len(base) + 1:])
    if not name or "/" in name or name.startswith("."):
        return None
    return f"$HOME/{music_local.cache_dir()}/{name}"


def chapters(url: str) -> list[dict]:
    """``[{"title", "start", "end"}]`` (seconds) of a file played from the
    phone's cache; [] when it has none or they cannot be read. Kept per URL:
    a file's chapters do not change."""
    if url in _CHAPTERS:
        return _CHAPTERS[url]
    path = phone_path(url)
    if not path:
        return []
    remote = f"ffprobe -v quiet -print_format json -show_chapters \"{path}\""
    try:
        r = subprocess.run(music_local.phone_argv(remote),
                           capture_output=True, text=True, timeout=15)
        raw = json.loads(r.stdout or "{}").get("chapters") or []
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []
    out = []
    for c in raw:
        try:
            out.append({"title": str((c.get("tags") or {}).get("title") or "").strip(),
                        "start": float(c["start_time"]), "end": float(c["end_time"])})
        except (KeyError, TypeError, ValueError):
            continue
    if r.returncode == 0:
        _CHAPTERS[url] = out
    return out


def chapter_at(chs: list[dict], t: Optional[float]) -> int:
    """Index of the chapter playing at `t` seconds, or -1."""
    if t is None:
        return -1
    at = -1
    for i, c in enumerate(chs):
        if c["start"] <= t + 0.5:
            at = i
    return at


def _phone_fetch(uri: str) -> Optional[str]:
    """Download `uri` into the phone's cache (play-local --fetch-only); its path."""
    timeout = float(os.environ.get("MEDIA_MUSIC_LOCAL_FETCH_TIMEOUT", "600"))
    remote = f"{music_local.fetch_cmd()} --fetch-only {shlex.quote(uri)}"
    try:
        r = subprocess.run(music_local.phone_argv(remote),
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("sink-music-sasonica: phone fetch failed: %s", e)
        return None
    lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln]
    if r.returncode != 0 or not lines or not lines[-1].startswith("/"):
        log.warning("sink-music-sasonica: phone fetch failed (%d): %s", r.returncode,
                    (r.stderr or r.stdout or "").strip()[-300:])
        return None
    return lines[-1]


def phone_resolve(uri: str) -> tuple[Optional[str], str]:
    """``(url, title)`` on the phone's music-files service; url None = not there."""
    base = files_base()
    raw = uri[3:] if uri.startswith("yt:") else uri
    vid = music_fetch.watch_id(raw)
    if not base or not vid:
        return None, ""
    path = music_local._phone_cached_path(vid) or _phone_fetch(raw)
    if not path:
        return None, ""
    name = path.rsplit("/", 1)[-1]
    return f"{base}/{urllib.parse.quote(name)}", music_local._phone_title(vid)


def resolve(uri: str) -> tuple[Optional[str], str]:
    """The phone's own copy first, then the red5 route ``abs`` uses."""
    url, title = phone_resolve(uri)
    return (url, title) if url else music_app.resolve(uri)


class SinkMusicSasonica(SinkMusicLocal):
    """The music channel's Sasonica backend (the app's media player)."""

    def _endpoint(self) -> str:
        ep = self._ep_override or endpoint()
        if not ep:
            raise ipc.MpvIpcError("sink-music-sasonica: MEDIA_MUSIC_SASONICA_ENDPOINT unset")
        return ep

    def play(self, uri: str, target: Target = SASONICA_TARGET,
             replace: bool = True, **_: object) -> bool:
        """Load `uri` in the app. False = it could not be; the caller falls back."""
        if not (self._ep_override or configured()):
            return False
        url, title = resolve(uri)
        if not url:
            log.info("sink-music-sasonica: nothing the app can play for %s", uri)
            return False
        try:
            ipc.command(self._endpoint(), "loadfile", url,
                        "replace" if replace else "append-play")
            if title and replace:
                self._set("force-media-title", title)
            if replace:
                from ..music_recent import art
                picture = art(uri)
                if picture:
                    # The lock screen's picture (sasonica-app MusicSession).
                    self._set("user-data/agent-media/art", picture)
                # The app's lock-screen next/prev move by these (sasonica-app
                # MusicSession); it forgets the last track's at the load.
                chs = chapters(url)
                if chs:
                    self._set("user-data/agent-media/chapters",
                              [[c["start"], c["title"]] for c in chs])
        except (ipc.MpvIpcError, OSError) as e:
            log.info("sink-music-sasonica: the app did not take %s: %s", uri, e)
            return False
        self._unpause_if(replace)
        if title:
            _note_title(uri, title)
        return True

    def seek_cur(self, target: Target = SASONICA_TARGET, position_ms: int = 0) -> None:
        # The app's socket takes `seek`, not a write to `time-pos` (the
        # speech channel never needed one), so a jump is the command.
        try:
            ipc.command(self._endpoint(), "seek", max(0.0, position_ms / 1000.0), "absolute")
        except (ipc.MpvIpcError, OSError):
            pass

    def _where(self) -> tuple[Optional[str], Optional[float]]:
        p = ipc.get_properties(self._endpoint(), ["path", "time-pos"])
        t = p.get("time-pos")
        return p.get("path"), (float(t) if isinstance(t, (int, float)) else None)

    def next(self, target: Target = SASONICA_TARGET) -> None:
        try:
            path, t = self._where()
            chs = chapters(path or "")
            i = chapter_at(chs, t)
            if chs and i + 1 < len(chs):
                self.seek_cur(position_ms=int(chs[i + 1]["start"] * 1000))
                return
        except (ipc.MpvIpcError, OSError):
            return
        super().next(target)

    def previous(self, target: Target = SASONICA_TARGET) -> None:
        """The chapter before, or this one's start when past its first
        seconds (a ⏮ that restarts first, as the popup's does)."""
        try:
            path, t = self._where()
            chs = chapters(path or "")
            i = chapter_at(chs, t)
            if chs and i >= 0:
                if t is not None and t - chs[i]["start"] > 3 or i == 0:
                    self.seek_cur(position_ms=int(chs[i]["start"] * 1000))
                else:
                    self.seek_cur(position_ms=int(chs[i - 1]["start"] * 1000))
                return
        except (ipc.MpvIpcError, OSError):
            return
        super().previous(target)

    # The app's volume is ExoPlayer's 0-100, not the Termux mpv's 0-170.
    def nominal_volume(self, target: Target = SASONICA_TARGET) -> int:
        return 100

    def unduck(self, target: Target = SASONICA_TARGET, restore: int = 100) -> None:
        try:
            self._set("volume", max(0, min(100, restore)))
        except (ipc.MpvIpcError, OSError):
            pass

    def volume_delta(self, delta: int, target: Target = SASONICA_TARGET) -> None:
        try:
            cur = ipc.get_property(self._endpoint(), "volume")
            self._set("volume", max(0, min(100, int(round((cur or 100) + delta)))))
        except (ipc.MpvIpcError, OSError, TypeError, ValueError):
            pass
