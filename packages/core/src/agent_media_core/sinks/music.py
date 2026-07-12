"""sink-music: thin MPD client for Mopidy.

Talks to Mopidy's MPD frontend (default port 6600). The actual content
type (music / audiobook / podcast / etc.) is tracked in `state/` and
drives interruption strategy in `route/` — this sink just plays.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
from contextlib import contextmanager
from typing import Iterator, List, Optional

from ..types import Target
from . import _mpv_ipc as ipc


log = logging.getLogger(__name__)

DEFAULT_TARGET = Target(name="local")

_YT_HOST_RE = re.compile(r"(?:^|//|\.)(?:youtube\.com|youtu\.be)/", re.I)
_YT_LIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


def _mpv_socket() -> Optional[str]:
    """Path to the Mopidy-Mpv backend's mpv IPC socket, or None if unknown."""
    override = os.environ.get("MEDIA_MUSIC_MPV_SOCKET")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return f"{runtime}/mopidy-mpv.sock" if runtime else None


def _set_mpv_volume(level: int) -> None:
    """Best-effort: set the Mopidy-Mpv backend's mpv volume.

    Mopidy-routed YouTube plays through mpv (the `mpv:` scheme), whose audio
    bypasses Mopidy's GStreamer SoftwareMixer — so MPD `setvol` can't duck it.
    We mirror the volume onto mpv too. A no-op for GStreamer tracks (mpv is
    idle) and silent if the socket is absent (backend down), so it never
    breaks ducking of GStreamer music.
    """
    sock = _mpv_socket()
    if not sock or not os.path.exists(sock):
        return
    try:
        ipc.set_property(sock, "volume", float(max(0, min(100, level))))
    except Exception:  # noqa: BLE001  (best-effort; mpv may be idle/down)
        pass


def _set_mpv_time(secs: float) -> None:
    """Best-effort: seek the Mopidy-Mpv renderer to an absolute position.

    MPD `seekcur` can't move mpv-routed tracks (they bypass Mopidy's pipeline
    the same way they bypass its mixer — see `_set_mpv_volume`), so seeks are
    mirrored too. A no-op when mpv is idle (GStreamer track) or its socket is
    absent, so GStreamer seeks are never double-applied.
    """
    sock = _mpv_socket()
    if not sock or not os.path.exists(sock):
        return
    try:
        if ipc.get_property(sock, "idle-active") is not False:
            return
        ipc.set_property(sock, "time-pos", float(max(0.0, secs)))
    except Exception:  # noqa: BLE001  (best-effort; mpv may be idle/down)
        pass


def mpv_now_props() -> Optional[dict]:
    """One batched snapshot of the Mopidy-Mpv renderer, or None when it's
    idle/unreachable. Used by status/label paths for `mpv:` tracks: MPD's
    tags for them are just the bare filename, while mpv has the embedded
    media-title, chapter, and a real duration."""
    sock = _mpv_socket()
    if not sock or not os.path.exists(sock):
        return None
    try:
        props = ipc.get_properties(
            sock,
            ["idle-active", "pause", "time-pos", "duration",
             "media-title", "chapter-metadata/by-key/title"],
            timeout=1.0)
    except (ipc.MpvIpcError, OSError):
        return None
    if props.get("idle-active") is not False:
        return None
    return props


def _to_music_uri(uri: str) -> str:
    """Route YouTube through the Mopidy-Mpv backend (robust mpv+yt-dlp) while
    leaving everything else on GStreamer.

    Rewrites a YouTube URI/URL — `yt:<url>`, `youtube:video:<id>`, `yt:<id>`,
    or a bare youtube.com/youtu.be URL — to `mpv:<watch-url>`, which the
    Mopidy-Mpv backend plays via mpv's yt-dlp. Non-YouTube URIs (local:,
    spotify:, plain http(s) streams, file paths) and already-`mpv:` URIs pass
    through untouched. A watch URL with a mix/playlist (`watch?v=…&list=…`) is
    rewritten to just that video. True playlist URLs (no video id) pass through,
    so Mopidy-YouTube keeps expanding them into individual tracks.
    """
    u = uri.strip()
    if u.startswith("mpv:"):
        return u

    inner = u
    had_yt_scheme = False
    for p in ("youtube:", "yt:"):
        if inner.startswith(p):
            inner = inner[len(p):]
            had_yt_scheme = True
            break

    # A YouTube *watch* URL carries a single video id (v=… or youtu.be/<id>);
    # play that one video via mpv even when a mix/playlist (list=) or index=
    # rides along — that's what a user pasting a "watch" link means. Only a true
    # playlist URL (no video id) falls through to the list= branch below for
    # Mopidy-YouTube to expand.
    if _YT_HOST_RE.search(inner):
        m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", inner) or \
            re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", inner)
        if m:
            return f"mpv:https://www.youtube.com/watch?v={m.group(1)}"

    # Leave playlists/search to Mopidy-YouTube's library expansion.
    low = inner.lower()
    if "list=" in low or low.startswith(("playlist:", "ytsearch", "channel:")):
        return u

    if inner.startswith(("http://", "https://")):
        return f"mpv:{inner}" if _YT_HOST_RE.search(inner) else u

    if had_yt_scheme:
        rest = inner[len("video:"):] if inner.startswith("video:") else inner
        if rest and "/" not in rest and ":" not in rest:  # a bare video id
            return f"mpv:https://www.youtube.com/watch?v={rest}"

    return u  # non-YouTube → unchanged (stays on GStreamer)


def _youtube_playlist_url(uri: str) -> Optional[str]:
    """Canonical playlist URL if `uri` is a YouTube *playlist*, else None.

    Matches the `playlist:<id>` scheme form and `.../playlist?list=<id>` URLs
    (with or without a `yt:`/`youtube:` prefix). Deliberately does NOT treat a
    `watch?v=…&list=…` URL as a playlist — that's a single video shared from
    within one, and the listener almost certainly wants just that track.
    """
    inner = uri.strip()
    for p in ("youtube:", "yt:"):
        if inner.startswith(p):
            inner = inner[len(p):]
            break
    if inner.startswith("playlist:"):
        pid = inner[len("playlist:"):]
        return f"https://www.youtube.com/playlist?list={pid}" if pid else None
    if _YT_HOST_RE.search(inner) and "/playlist" in inner.lower():
        m = _YT_LIST_RE.search(inner)
        if m:
            return f"https://www.youtube.com/playlist?list={m.group(1)}"
    return None


def _expand_youtube_playlist(uri: str) -> Optional[List[str]]:
    """Expand a YouTube playlist into per-track `mpv:` URIs via yt-dlp.

    Returns a list of `mpv:https://www.youtube.com/watch?v=<id>` URIs so each
    track plays through the Mopidy-Mpv backend (robust yt-dlp + working duck),
    or None if `uri` isn't a playlist or enumeration fails (caller then falls
    back to the single-URI path). yt-dlp reads ~/.config/yt-dlp/config, so the
    android_vr client + cookies apply here too.
    """
    purl = _youtube_playlist_url(uri)
    if not purl:
        return None
    cap = int(os.environ.get("MEDIA_MUSIC_PLAYLIST_MAX", "50"))
    ytdlp = os.environ.get("MEDIA_YTDLP_BIN", "yt-dlp")
    try:
        proc = subprocess.run(
            [ytdlp, "--flat-playlist", "--no-warnings", "--ignore-errors",
             "--print", "%(id)s", "--playlist-end", str(cap), purl],
            capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("playlist expand failed for %s (%s); using fallback", purl, e)
        return None
    ids = [ln.strip() for ln in proc.stdout.splitlines()
           if ln.strip() and ln.strip() != "NA"]
    if not ids:
        log.warning("playlist expand returned no tracks for %s; using fallback", purl)
        return None
    if len(ids) >= cap:
        log.info("playlist %s capped at %d tracks (MEDIA_MUSIC_PLAYLIST_MAX)", purl, cap)
    return [f"mpv:https://www.youtube.com/watch?v={vid}" for vid in ids]


def _endpoint(target: Target) -> tuple[str, int]:
    if target.name != "local":
        raise NotImplementedError(f"sink-music target {target.name!r} not yet supported")
    host = os.environ.get("MEDIA_MPD_HOST") or os.environ.get("MPD_HOST", "127.0.0.1")
    port = int(os.environ.get("MEDIA_MPD_PORT") or os.environ.get("MPD_PORT", "6600"))
    return host, port


@contextmanager
def _connect(target: Target, timeout: float = 5.0) -> Iterator[socket.socket]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(_endpoint(target))
        # Read MPD greeting (`OK MPD 0.x\n`)
        s.recv(64)
        yield s
    finally:
        s.close()


def _cmd(s: socket.socket, line: str) -> str:
    s.sendall((line + "\n").encode())
    buf = b""
    while b"OK\n" not in buf and not buf.endswith(b"OK") and b"ACK" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"OK\n") or b"\nACK" in buf or buf.startswith(b"ACK"):
            break
    return buf.decode(errors="replace")


def _parse_kv(text: str) -> dict:
    """Parse MPD's `key: value\\n` response block into a dict.

    Keys are unique in `status`/`currentsong`, so first-wins is fine.
    """
    out: dict = {}
    for line in text.splitlines():
        if line in ("OK", "") or line.startswith(("OK ", "ACK")):
            continue
        k, sep, v = line.partition(": ")
        if sep:
            out.setdefault(k, v.strip())
    return out


def _localise_youtube(uri: str) -> str:
    """Swap an `mpv:<watch-url>` for an `mpv:<rooms-local file>` when possible.

    The rooms host can no longer stream YouTube (datacenter IP bot-block), so
    music_fetch acquires the audio via the phone's residential IP into the
    rooms cache. On any failure the watch URL passes through unchanged — the
    old streaming path stays as the fallback.
    """
    if not uri.startswith(("mpv:http://", "mpv:https://")):
        return uri
    from . import music_fetch
    local = music_fetch.ensure_local(uri[len("mpv:"):])
    return f"mpv:{local}" if local else uri


class SinkMusic:
    """Sink protocol implementation for Mopidy / MPD."""

    def play(self, uri: str, target: Target = DEFAULT_TARGET,
             replace: bool = True, **_: object) -> None:
        playlist = _expand_youtube_playlist(uri)
        if playlist:
            # YouTube playlist → play the first track as soon as it's fetched;
            # a detached helper downloads and appends the rest as they land
            # (Mopidy-YouTube's own playlist expansion is unreliable).
            from . import music_fetch
            first = _localise_youtube(playlist[0])
            with _connect(target) as s:
                if replace:
                    _cmd(s, "clear")
                _cmd(s, f'add "{first}"')
                _cmd(s, "play")
            music_fetch.spawn_append_fetched(
                [u[len("mpv:"):] for u in playlist[1:]])
            return
        uri = _to_music_uri(uri)  # YouTube → mpv: backend; else unchanged
        uri = _localise_youtube(uri)  # …and mpv:<watch-url> → local file
        with _connect(target) as s:
            if replace:
                _cmd(s, "clear")
            _cmd(s, f'add "{uri}"')
            _cmd(s, "play")

    def enqueue(self, uri: str, target: Target = DEFAULT_TARGET) -> None:
        """Append to the queue without clearing it or forcing playback."""
        with _connect(target) as s:
            _cmd(s, f'add "{uri}"')

    def pause(self, target: Target = DEFAULT_TARGET) -> None:
        with _connect(target) as s:
            _cmd(s, "pause 1")

    def resume(self, target: Target = DEFAULT_TARGET) -> None:
        with _connect(target) as s:
            _cmd(s, "pause 0")

    def stop(self, target: Target = DEFAULT_TARGET) -> None:
        with _connect(target) as s:
            _cmd(s, "stop")

    def duck(self, target: Target = DEFAULT_TARGET, level: int = 15) -> None:
        lvl = max(0, min(100, level))
        with _connect(target) as s:
            _cmd(s, f"setvol {lvl}")
        _set_mpv_volume(lvl)  # also duck mpv-routed (shared YouTube) tracks

    def unduck(self, target: Target = DEFAULT_TARGET, restore: int = 100) -> None:
        with _connect(target) as s:
            _cmd(s, f"setvol {restore}")
        _set_mpv_volume(restore)

    def position(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        """Current playback position in ms, or None when not playing."""
        with _connect(target) as s:
            status = _cmd(s, "status")
        for line in status.splitlines():
            if line.startswith("elapsed:"):
                try:
                    return int(float(line.split(":", 1)[1].strip()) * 1000)
                except ValueError:
                    return None
        return None

    def seek_cur(self, target: Target = DEFAULT_TARGET,
                 position_ms: int = 0) -> None:
        """Seek the *current* track to an absolute position in ms.

        Used by the coordinator's pause-and-resume path to back up by
        the lead-in window so the listener doesn't miss the word that
        was playing when speech interrupted.
        """
        secs = max(0.0, position_ms / 1000.0)
        with _connect(target) as s:
            _cmd(s, f"seekcur {secs:.3f}")
        _set_mpv_time(secs)  # mpv-routed tracks ignore MPD seekcur

    def next(self, target: Target = DEFAULT_TARGET) -> None:
        with _connect(target) as s:
            _cmd(s, "next")

    def previous(self, target: Target = DEFAULT_TARGET) -> None:
        with _connect(target) as s:
            _cmd(s, "previous")

    def toggle(self, target: Target = DEFAULT_TARGET) -> None:
        """Play/pause toggle (MPD `pause` with no arg toggles state)."""
        with _connect(target) as s:
            _cmd(s, "pause")

    def set_speed(self, rate: float, target: Target = DEFAULT_TARGET) -> bool:
        """Pitch-corrected playback speed (mpv `speed`, clamped 0.25–4.0).

        Only mpv-routed tracks have a speed control — MPD/GStreamer has no
        such concept — so this returns False when the renderer is idle or
        unreachable and the caller can say so instead of silently no-opping.
        """
        sock = _mpv_socket()
        if not sock or not os.path.exists(sock):
            return False
        try:
            if ipc.get_property(sock, "idle-active") is not False:
                return False
            ipc.set_property(sock, "speed",
                             float(min(4.0, max(0.25, rate))))
            return True
        except (ipc.MpvIpcError, OSError):
            return False

    def current_speed(self, target: Target = DEFAULT_TARGET) -> Optional[float]:
        """The renderer's playback speed, or None when no mpv track is live."""
        sock = _mpv_socket()
        if not sock or not os.path.exists(sock):
            return None
        try:
            if ipc.get_property(sock, "idle-active") is not False:
                return None
            v = ipc.get_property(sock, "speed")
            return float(v) if v is not None else None
        except (ipc.MpvIpcError, OSError, TypeError, ValueError):
            return None

    def now_playing_uri(self, target: Target = DEFAULT_TARGET) -> Optional[str]:
        with _connect(target) as s:
            current = _cmd(s, "currentsong")
        for line in current.splitlines():
            if line.startswith("file:"):
                return line.split(":", 1)[1].strip()
        return None

    def status_dict(self, target: Target = DEFAULT_TARGET) -> dict:
        """Parsed MPD `status` (state/elapsed/duration/volume/…)."""
        with _connect(target) as s:
            return _parse_kv(_cmd(s, "status"))

    def current_volume(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        """Mixer volume 0-100, or None when unreadable / mixer disabled (-1)."""
        try:
            v = int(self.status_dict(target).get("volume", ""))
        except (ValueError, TypeError, OSError):
            return None
        return v if v >= 0 else None

    def current_song(self, target: Target = DEFAULT_TARGET) -> dict:
        """Parsed MPD `currentsong` (Title/Artist/Name/file/…)."""
        with _connect(target) as s:
            return _parse_kv(_cmd(s, "currentsong"))

    def seek_relative(self, secs: float,
                      target: Target = DEFAULT_TARGET) -> None:
        """Seek the current track by ±secs. Computed from the live position
        and issued as an absolute seekcur so we don't depend on Mopidy
        supporting relative `seekcur +N` syntax. For mpv-routed tracks the
        renderer's time-pos is the truth (MPD's elapsed drifts after a
        mirrored seek) and the seek is mirrored onto mpv."""
        props = mpv_now_props()
        cur = None
        if props and props.get("time-pos") is not None:
            cur = float(props["time-pos"])
        with _connect(target) as s:
            if cur is None:
                st = _parse_kv(_cmd(s, "status"))
                try:
                    cur = float(st.get("elapsed", "0") or 0)
                except ValueError:
                    cur = 0.0
            dest = max(0.0, cur + secs)
            _cmd(s, f"seekcur {dest:.3f}")
        if props:
            _set_mpv_time(dest)

    def volume_delta(self, delta: int,
                     target: Target = DEFAULT_TARGET) -> None:
        """Change volume by ±delta, clamped to [0, 100]."""
        with _connect(target) as s:
            st = _parse_kv(_cmd(s, "status"))
            try:
                cur = int(st.get("volume", "100") or 100)
            except ValueError:
                cur = 100
            lvl = max(0, min(100, cur + delta))
            _cmd(s, f"setvol {lvl}")
        # Mirror onto the Mopidy-Mpv renderer, same as duck()/unduck():
        # mpv-routed tracks bypass the MPD mixer, so setvol alone is silent.
        _set_mpv_volume(lvl)
