"""sink-music-app: music played by Sasonica's ExoPlayer on the phone.

The `app` target means "the phone, played by an app rather than Termux". For
speech that app is the companion; for music it is Sasonica, which already
plays books from red5 through its control endpoint (``phone_player``) and
since the URL route (``/play?url=``) plays any http URL as a one-track,
unsynced session. Music gets the app's focus handling, lock-screen controls
and notification, and Termux's mpv is only the fallback.

The app cannot run yt-dlp or read Termux's files, so a YouTube URI is
fetched the way the rooms lane fetches it (``music_fetch.ensure_local``: on
the phone's residential IP, copied into red5's cache) and handed to the app
as a URL on red5's clip server, the same one books are served from
(``MEDIA_BOOK_HTTP_ROOT`` / ``MEDIA_BOOK_BASEURL_PHONE``, or
``MEDIA_MUSIC_APP_BASEURL`` to override the base). Any other http(s) URL is
handed over as it is.

``play`` returns False when the app did not take it (unreachable, frozen,
a URI it cannot play); the router then falls back to the phone's mpv. The
transport verbs act only while the app is holding a *music* session — a
book in the app answers ``item``, music answers ``url`` — so pausing music
never pauses a book.

Ducking is left to Android: the companion's speech takes audio focus, and
ExoPlayer ducks or pauses for it the way it does for any other app. There is
no volume route to turn down, so ``duck``/``unduck`` are deliberately inert.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from pathlib import Path
from typing import Optional

from .. import phone_player
from ..types import Target
from . import music_fetch

log = logging.getLogger(__name__)

APP_TARGET = Target(name="app")
#: /state is asked before every duck and on every routed read, so it must be
#: quick to give up: a frozen app is "not playing music", not a stall.
_STATE_TIMEOUT = 3.0


def configured() -> bool:
    return phone_player.enabled() and bool(phone_player.direct_url(APP_TARGET))


def _served_url(path: str) -> Optional[str]:
    """An http URL on red5's clip server for a file in the rooms cache.

    The server's root is the agent-media cache, not the music cache, so the
    file is linked in under ``music/``. None when no server is configured or
    the file is on another host (a remote rooms host has its own disk).
    """
    root = os.environ.get("MEDIA_BOOK_HTTP_ROOT", "")
    base = (os.environ.get("MEDIA_MUSIC_APP_BASEURL")
            or os.environ.get("MEDIA_BOOK_BASEURL_PHONE") or "").rstrip("/")
    src = Path(path)
    if not root or not base or music_fetch.rooms_ssh_host() is not None or not src.is_file():
        return None
    link = Path(root).expanduser() / "music" / src.name
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if not (link.is_symlink() and link.resolve() == src.resolve()):
            link.unlink(missing_ok=True)
            link.symlink_to(src)
    except OSError as e:
        log.warning("sink-music-app: cannot link %s under %s: %s", src, root, e)
        return None
    return f"{base}/music/{urllib.parse.quote(src.name)}"


def _title_beside(path: str) -> str:
    try:
        return Path(path).with_suffix(".title").read_text().strip()
    except OSError:
        return ""


def resolve(uri: str) -> tuple[Optional[str], str]:
    """``(url, title)`` the app can play for `uri`; url None = it cannot."""
    raw = uri[3:] if uri.startswith("yt:") else uri
    if music_fetch.watch_id(raw) and ("youtu" in raw or uri.startswith("yt:")):
        path = music_fetch.ensure_local(raw)
        return (_served_url(path), _title_beside(path)) if path else (None, "")
    if raw.startswith(("http://", "https://")):
        return raw, ""
    if raw.startswith("/"):
        return _served_url(raw), _title_beside(raw)
    return None, ""


class SinkMusicApp:
    """The music channel's Sasonica backend (phone, played by the app)."""

    def _state(self) -> Optional[dict]:
        if not configured():
            return None
        return phone_player.request(APP_TARGET, "/state", timeout=_STATE_TIMEOUT)

    def _music(self) -> Optional[dict]:
        """The app's state while it holds a music session, else None."""
        s = self._state()
        if not s or s.get("closed") or not s.get("url") or s.get("item"):
            return None
        return s

    def _send(self, route: str, params: Optional[dict] = None) -> None:
        if self._music() is not None:
            phone_player.request(APP_TARGET, route, params, timeout=10.0)

    # ---- playback -----------------------------------------------------------

    def play(self, uri: str, target: Target = APP_TARGET,
             replace: bool = True, **_: object) -> bool:
        """Start `uri` in the app. False = not taken; the caller falls back.

        The app plays one thing at a time, so an append (`replace=False`) is
        not something it can do — that falls back too, to mpv's playlist.
        """
        if not replace or not configured():
            return False
        url, title = resolve(uri)
        if not url:
            log.info("sink-music-app: nothing the app can play for %s", uri)
            return False
        s = phone_player.request(APP_TARGET, "/play",
                                 {"url": url, "title": title or None, "rate": 1.0})
        if s is None:
            return False
        if title:
            from .music_local import _note_title
            _note_title(uri, title)
        return True

    # ---- transport ------------------------------------------------------------

    def pause(self, target: Target = APP_TARGET) -> None:
        self._send("/pause")

    def resume(self, target: Target = APP_TARGET) -> None:
        self._send("/resume")

    def toggle(self, target: Target = APP_TARGET) -> None:
        self._send("/toggle")

    def stop(self, target: Target = APP_TARGET) -> None:
        self._send("/stop")

    def seek_cur(self, target: Target = APP_TARGET, position_ms: int = 0) -> None:
        self._send("/seek", {"t": max(0, position_ms) / 1000.0})

    def seek_relative(self, secs: float, target: Target = APP_TARGET) -> None:
        self._send("/jump", {"by": float(secs)})

    def set_speed(self, rate: float, target: Target = APP_TARGET) -> bool:
        if self._music() is None:
            return False
        return phone_player.request(APP_TARGET, "/speed",
                                    {"rate": float(min(4.0, max(0.25, rate)))}) is not None

    def current_speed(self, target: Target = APP_TARGET) -> Optional[float]:
        s = self._music()
        return float(s["rate"]) if s and s.get("rate") is not None else None

    # Android's audio focus ducks the app for the companion's speech; see the
    # module docstring. Nothing to turn down, and nothing to restore.
    def duck(self, target: Target = APP_TARGET, level: int = 15) -> None:
        return None

    def unduck(self, target: Target = APP_TARGET, restore: int = 100) -> None:
        return None

    def current_volume(self, target: Target = APP_TARGET) -> Optional[int]:
        return None

    def nominal_volume(self, target: Target = APP_TARGET) -> Optional[int]:
        return None

    # ---- observation ----------------------------------------------------------

    def position(self, target: Target = APP_TARGET) -> Optional[int]:
        s = self._music()
        return int(float(s.get("t") or 0) * 1000) if s else None

    def now_playing_uri(self, target: Target = APP_TARGET) -> Optional[str]:
        s = self._music()
        return s.get("url") if s else None

    def active(self, target: Target = APP_TARGET) -> bool:
        s = self._music()
        return bool(s and not s.get("paused"))

    def loaded(self, target: Target = APP_TARGET) -> bool:
        return self._music() is not None
