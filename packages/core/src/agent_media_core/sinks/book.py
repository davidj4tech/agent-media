"""sink-book: longform (audiobook / podcast) player — the book channel.

A second mpv broker, distinct from sink-speech (TTS clips) and sink-music
(Mopidy). The book channel is its own long-running mpv on mel with
book-shaped transport: resume-by-URI bookmarks (held in the state store),
playback speed, skip ±N seconds, and its own PulseAudio/PipeWire stream
(client name `agent-media-book`) so a later phase can mix it under music.

Unlike sink-speech — whose broker is started by a service — this class
lazy-spawns its broker the first time the channel is used, so it works the
moment you call `book play`. Set MEDIA_BOOK_AUTOSPAWN=0 to require an
externally-managed broker instead.

Probe/control methods never spawn: with no broker running they treat the
channel as idle, so the speech coordinator can cheaply ask "is a book
playing?" without starting mpv.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from .._paths import state_dir
from .. import mopidy
from ..types import Target
from . import _mpv_ipc as ipc


log = logging.getLogger(__name__)

DEFAULT_TARGET = Target(name="local")

# mpv playback-speed sane bounds.
_MIN_SPEED = 0.25
_MAX_SPEED = 4.0

# Debian's mpv-mpris drops the cplugin here and symlinks it into
# /etc/mpv/scripts for autoload. We point at the real file, not the autoload
# symlink, because this broker's --no-config is what makes the symlink moot.
_MPRIS_SO = "/usr/lib/mpv-mpris/mpris.so"


def _to_book_uri(uri: str) -> str:
    """Wrap any URI as `mpv:...` so the book Mopidy's mopidy-mpv backend can
    play it. Already-`mpv:` URIs pass through. The `yt:` prefix (legacy from
    the book_play MCP tool) is stripped before wrapping."""
    u = uri.strip()
    if u.startswith("mpv:"):
        return u
    if u.startswith("yt:"):
        u = u[3:]
    return f"mpv:{u}"


def _mopidy_enabled() -> bool:
    return os.environ.get("MEDIA_BOOK_MOPIDY", "0") not in ("", "0", "false", "no")


def normalize_uri(uri: str) -> str:
    """Coerce a URI into something mpv understands — and the bookmark key.

    The agent/music side speaks Mopidy URIs (`yt:https://...`); mpv plays
    YouTube via yt-dlp from the bare URL. Strip a leading `yt:` / `youtube:`
    so the same URI a user hands to `music_play` also works here.
    Everything else (http(s), file://, local paths) passes through.
    """
    u = uri.strip()
    for prefix in ("yt:", "youtube:"):
        if u.startswith(prefix):
            return u[len(prefix):]
    return u


def _socket_path() -> Path:
    override = os.environ.get("MEDIA_BOOK_SOCKET")
    if override:
        return Path(override)
    return state_dir() / "sink-book.sock"


def _socket_for(target: Target) -> "str | Path":
    """Book IPC endpoint for a target.

    Per-target book sockets win. If absent, reuse the speech target's socket so
    `MEDIA_SPEECH_DEFAULT_TARGET=phone` can make longform play on the same
    phone-side mpv bridge by default.
    """
    override = (os.environ.get(_env_key("MEDIA_BOOK_SOCKET", target.name))
                or os.environ.get(_env_key("MEDIA_SPEECH_SOCKET", target.name)))
    if override:
        return override if override.startswith("tcp://") else Path(override)
    return _socket_path()


def _abs_public_url(target: Target, private_url: str) -> str:
    public = os.environ.get("MEDIA_AUDIOBOOKSHELF_PUBLIC_URL") or os.environ.get("ABS_PUBLIC_URL")
    if public:
        return public.rstrip("/")
    if target.name in ("", "local"):
        return private_url.rstrip("/")
    base = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_BASEURL", target.name))
    if not base:
        return private_url.rstrip("/")
    host = urlsplit(base).hostname
    if not host:
        return private_url.rstrip("/")
    p = urlsplit(private_url)
    port = f":{p.port}" if p.port else ""
    return urlunsplit((p.scheme, f"{host}{port}", "", "", "")).rstrip("/")


def _remote_book_cache_dir(target: Target) -> str:
    return os.environ.get(
        _env_key("MEDIA_BOOK_CACHE_DIR", target.name),
        ".cache/agent-media/books",
    )


def _remote_book_cache_ssh(target: Target) -> str:
    return (os.environ.get(_env_key("MEDIA_BOOK_CACHE_SSH", target.name))
            or os.environ.get(_env_key("MEDIA_SPEECH_CLIP_SSH", target.name))
            or os.environ.get("MEDIA_MUSIC_LOCAL_SSH", ""))


def _probe_timeout() -> float:
    """Seconds to allow a remote cache probe.

    Was a flat 12. On a link at 450ms RTT with a fifth of its packets dropped,
    that probe measures 1.2-5s on a good minute and simply exceeds 12 on a bad
    one — and the whole staging step then reports failure for a phone that is
    answering fine. A bound tuned on a fast network becomes a fault injector on
    a slow one, so it is generous by default and configurable.
    """
    try:
        return float(os.environ.get("MEDIA_BOOK_CACHE_PROBE_TIMEOUT", "45"))
    except ValueError:
        return 45.0


def _remote_cache_path(path: Path, target: Target, *, create: bool = True) -> Optional[tuple[str, str, str]]:
    """Return (ssh_host, remote_dir, remote_path) for a target book cache."""
    if target.name in ("", "local"):
        return None
    host = _remote_book_cache_ssh(target)
    if not host:
        return None
    cache_expr = _remote_book_cache_dir(target)
    mkdir = "mkdir -p \"$d\" && " if create else ""
    remote_cmd = (
        f"d={shlex.quote(cache_expr)}; "
        "eval \"d=$d\"; case \"$d\" in /*) ;; *) d=\"$HOME/$d\";; esac; "
        f"{mkdir}cd \"$d\" && pwd"
    )
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, remote_cmd],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=_probe_timeout(), check=False,
    )
    if p.returncode != 0 or not p.stdout.strip():
        return None
    remote_dir = p.stdout.strip().splitlines()[-1]
    return host, remote_dir, f"{remote_dir}/{path.name}"


def remote_cached_path(path: Path, target: Target) -> Optional[str]:
    """Return target-local cached path when the file is already present."""
    try:
        resolved = _remote_cache_path(path, target, create=False)
        if not resolved:
            return None
        host, _remote_dir, remote_path = resolved
        size = path.stat().st_size
        test_cmd = f"test -f {shlex.quote(remote_path)} && test $(wc -c < {shlex.quote(remote_path)}) -eq {size}"
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, test_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_probe_timeout(), check=False,
        )
        return remote_path if p.returncode == 0 else None
    except Exception:
        return None


def http_url_for(path: Path, target: Target) -> Optional[str]:
    """A URL the target can fetch this file from, or None.

    Transport by pull rather than push. `rsync` over ssh needs a reachable
    host, a resolvable alias, a writable cache directory and a probe that
    finishes inside its timeout — and every one of those failed at least once
    today, each time as silence rather than an error. An HTTP GET needs the
    file to be under a served directory, and mpv brings range requests,
    retries and caching with it.

    Only files under `MEDIA_BOOK_HTTP_ROOT` qualify, because only those are
    actually exposed. An audiobook elsewhere on disk still stages over ssh —
    this narrows the ssh path to the case that needs it rather than pretending
    to replace it.
    """
    base = os.environ.get(_env_key("MEDIA_BOOK_BASEURL", target.name))
    root = os.environ.get("MEDIA_BOOK_HTTP_ROOT")
    if not base or not root:
        return None
    try:
        rel = path.resolve().relative_to(Path(root).expanduser().resolve())
    except (ValueError, OSError):
        return None
    return base.rstrip("/") + "/" + "/".join(quote(p) for p in rel.parts)


def _stage_local_for_remote(path: Path, target: Target) -> Optional[str]:
    """Copy a red5-local file to the target's XDG book cache and return remote path."""
    if not path.is_file():
        return None
    try:
        cached = remote_cached_path(path, target)
        if cached:
            return cached
        resolved = _remote_cache_path(path, target, create=True)
        if not resolved:
            return None
        host, remote_dir, remote_path = resolved
        subprocess.run(
            ["rsync", "-a", "-s", "-e", "ssh -o ConnectTimeout=8",
             str(path), f"{host}:{remote_dir}/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=float(os.environ.get("MEDIA_BOOK_CACHE_COPY_TIMEOUT", "600")),
            check=True,
        )
        return remote_path
    except Exception as e:  # noqa: BLE001 - fall back to host path/URL handling
        log.warning("sink-book: staging %s failed: %s", path, e)
        return None


def start_stage_local_for_remote(path: Path, target: Target, *, start_ms: int = -1,
                                 title: str = "") -> bool:
    """Stage a local file to a remote target in the background, then play it."""
    if target.name in ("", "local") or not path.is_file():
        return False
    _write_stage_status({
        "status": "copying", "uri": str(path), "target": target.name,
        "title": title or path.stem, "total": path.stat().st_size,
        "copied": 0, "remote_path": "", "error": "",
    })
    code = r'''
import json
import subprocess
import sys
import time
from pathlib import Path
from agent_media_core.intake._env import load_env_file
load_env_file()
from agent_media_core.sinks.book import _remote_cache_path, remote_cached_path, stage_status_path
from agent_media_core.types import Target
from agent_media_core import mcp_server
path = Path(sys.argv[1])
target = Target(sys.argv[2])
start_ms = int(sys.argv[3])
title = sys.argv[4] or path.stem
total = path.stat().st_size
status_path = stage_status_path()
def write(status, copied=0, remote_path='', error=''):
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        'status': status, 'uri': str(path), 'target': target.name,
        'title': title, 'total': total, 'copied': int(copied or 0),
        'remote_path': remote_path, 'error': error, 'ts': time.time(),
    }))
try:
    cached = remote_cached_path(path, target)
    if cached:
        write('playing', total, cached)
        mcp_server.book_play(cached, resume=(start_ms < 0), start_ms=start_ms, target=target.name, title=title)
        write('done', total, cached)
        raise SystemExit(0)
    resolved = _remote_cache_path(path, target, create=True)
    if not resolved:
        write('error', 0, '', 'could not resolve remote cache')
        raise SystemExit(1)
    host, remote_dir, remote_path = resolved
    write('copying', 0, remote_path)
    proc = subprocess.Popen(
        ['rsync', '-a', '--info=progress2', '-s', '-e', 'ssh -o ConnectTimeout=8',
         str(path), f'{host}:{remote_dir}/'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    copied = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        parts = line.strip().split()
        if parts:
            n = parts[0].replace(',', '')
            if n.isdigit():
                copied = int(n)
                write('copying', min(copied, total), remote_path)
    rc = proc.wait()
    if rc != 0:
        write('error', copied, remote_path, f'rsync exited {rc}')
        raise SystemExit(rc)
    write('playing', total, remote_path)
    mcp_server.book_play(remote_path, resume=(start_ms < 0), start_ms=start_ms, target=target.name, title=title)
    write('done', total, remote_path)
except Exception as e:
    write('error', 0, '', str(e))
    raise
'''
    subprocess.Popen(
        [sys.executable, "-c", code, str(path), target.name, str(start_ms), title],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True


def _abs_config() -> tuple[str, str]:
    """Audiobookshelf URL/token, accepting the existing abs-bridge env names."""
    url = os.environ.get("MEDIA_AUDIOBOOKSHELF_URL") or os.environ.get("ABS_URL") or ""
    token = os.environ.get("MEDIA_AUDIOBOOKSHELF_TOKEN") or os.environ.get("ABS_TOKEN") or ""
    if url and token:
        return url.rstrip("/"), token
    try:
        p = Path.home() / ".config" / "agent-media" / "abs-bridge.env"
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"\'')
            if k == "ABS_URL" and not url:
                url = v
            elif k == "ABS_TOKEN" and not token:
                token = v
    except OSError:
        pass
    return url.rstrip("/"), token


def load_intent_path() -> Path:
    """Marker file the book sink drops on each play() so book_observer.py can
    distinguish agent-media-initiated loads from external (Iris) ones."""
    return state_dir() / "book-load-intent.json"


def stage_status_path() -> Path:
    return state_dir() / "book-stage.json"


def read_stage_status() -> Optional[dict]:
    try:
        data = json.loads(stage_status_path().read_text())
        if time.time() - float(data.get("ts") or 0) > 86400:
            return None
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_stage_status(data: dict) -> None:
    try:
        p = stage_status_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data["ts"] = time.time()
        p.write_text(json.dumps(data))
    except Exception:
        pass


def _write_load_intent(uri: str, start_ms: int) -> None:
    import json
    try:
        p = load_intent_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"uri": uri, "start_ms": int(start_ms), "ts": time.time()}))
    except Exception:  # noqa: BLE001 — best-effort breadcrumb
        pass


def _env_key(prefix: str, target_name: str) -> str:
    return f"{prefix}_{target_name.upper().replace('-', '_')}"


def _device_for(target: Target) -> Optional[str]:
    """mpv `audio-device` for a target. None = mpv's default device.

    The book channel follows the speech channel's routing defaults where
    possible: MEDIA_BOOK_DEVICE_<TARGET> wins, then MEDIA_SPEECH_DEVICE_<TARGET>
    is accepted for shared logical targets such as `phone`.
    """
    override = (os.environ.get(_env_key("MEDIA_BOOK_DEVICE", target.name))
                or os.environ.get(_env_key("MEDIA_SPEECH_DEVICE", target.name)))
    if override is not None:
        return None if override.lower() in ("", "auto", "default") else override
    if target.name == "local":
        override = os.environ.get("MEDIA_BOOK_DEVICE")
        if override and override.lower() not in ("", "auto", "default"):
            return override
        return None
    if target.name == "rooms":
        sink = (os.environ.get("MEDIA_BOOK_ROOMS_SINK")
                or os.environ.get("MEDIA_ROOMS_SINK") or "am")
        return f"pulse/{sink}"
    raise NotImplementedError(
        f"sink-book target {target.name!r} not configured — set "
        f"{_env_key('MEDIA_BOOK_DEVICE', target.name)}")


class SinkBook:
    """The book channel: a longform mpv player with resume + speed + skip."""

    def __init__(self) -> None:
        self._sock = _socket_path()

    # ---- broker lifecycle ------------------------------------------------

    def _running(self) -> bool:
        """True if the broker socket answers. Never spawns."""
        if not self._sock.exists():
            return False
        try:
            ipc.get_property(self._sock, "idle-active", timeout=1.0)
            return True
        except (ipc.MpvIpcError, OSError):
            return False

    def _ensure_broker(self) -> None:
        if self._running():
            return
        if os.environ.get("MEDIA_BOOK_AUTOSPAWN", "1") == "0":
            raise ipc.MpvIpcError(
                "sink-book broker not running and autospawn disabled")
        # A stale socket from a dead broker would block the new bind.
        try:
            self._sock.unlink()
        except FileNotFoundError:
            pass
        self._sock.parent.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        # mpv shells out to yt-dlp for YouTube; ensure ~/.local/bin is found.
        local_bin = str(Path.home() / ".local" / "bin")
        if local_bin not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

        client = os.environ.get("MEDIA_BOOK_AUDIO_CLIENT", "agent-media-book")
        argv = [
            os.environ.get("MEDIA_MPV_BIN", "mpv"),
            "--idle=yes", "--no-video", "--no-terminal", "--no-config",
            f"--input-ipc-server={self._sock}",
            f"--ao={os.environ.get('MEDIA_BOOK_AO', 'pulse')}",
            f"--audio-client-name={client}",
            "--cache=yes",
        ]
        # Optional richer browser UI: simple-mpv-webui loads INTO this mpv as a
        # Lua script (needs luasocket on the Lua-5.1 module path) and serves a
        # full audiobook UI — seek, speed, chapters, playlist. It binds 0.0.0.0,
        # but mel's firewall keeps it tailnet-only (the public zone exposes no
        # such port). Gated on the script existing; path/port/auth overridable.
        webui = os.environ.get(
            "MEDIA_BOOK_WEBUI",
            str(Path.home() / "src" / "simple-mpv-webui" / "main.lua"))
        if webui and Path(webui).is_file():
            opts = "webui-port=" + os.environ.get("MEDIA_BOOK_WEBUI_PORT", "8889")
            htpw = os.environ.get("MEDIA_BOOK_WEBUI_HTPASSWD", "")
            if htpw:
                opts += ",webui-htpasswd_path=" + htpw
            argv += [f"--script={webui}", f"--script-opts={opts}"]
        # MPRIS, so the book channel shows up in `playerctl` alongside music and
        # speech. Every other mpv here picks mpris.so up automatically from
        # /etc/mpv/scripts, but this broker runs --no-config (above), which
        # disables script-dir loading — so it has to be named explicitly.
        # Gated on the file existing, like the webui above: Termux has no D-Bus
        # and no such package, and a missing --script makes mpv exit non-zero.
        mpris = os.environ.get("MEDIA_BOOK_MPRIS", _MPRIS_SO)
        if mpris and Path(mpris).is_file():
            argv.append(f"--script={mpris}")
        subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        # Wait for the socket to come up (cold mpv ~0.3-1s). EOF self-heal and
        # playlist auto-advance are handled by the book event watcher in the
        # long-lived MCP server (mcp_server._autoadvance_loop), which connects
        # to this socket — no per-broker sidecar here.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self._running():
                return
            time.sleep(0.1)
        raise ipc.MpvIpcError("sink-book broker did not come up in time")

    # ---- playback --------------------------------------------------------

    def play(self, uri: str, target: Target = DEFAULT_TARGET,
             start_ms: Optional[int] = None, **_: object) -> str:
        """Load and play `uri` from `start_ms` (or the beginning).

        Returns the normalized URI handed to mpv (i.e. the bookmark key).
        """
        norm = normalize_uri(uri)
        endpoint = _socket_for(target)
        if target.name not in ("", "local") and not norm.startswith(("http://", "https://", "rtsp://")):
            # Pull beats push when the file is already exposed: no ssh, no
            # remote cache to resolve, no copy to time out halfway.
            url = http_url_for(Path(norm).expanduser(), target)
            if url:
                log.debug("sink-book: serving %s over http as %s", norm, url)
                norm = url
                staged = url
            else:
                staged = _stage_local_for_remote(Path(norm).expanduser(), target)
            if staged:
                norm = staged
            else:
                # Staging failed, so `norm` is still a path on *this* host —
                # and handing that to a player on another device asks it to
                # open a file it cannot possibly see. It fails the way an
                # unreadable file always fails on mpv: silently, staying idle,
                # with the caller's play() returning perfectly normally.
                #
                # That is how a dead ssh alias left after a hostname change
                # presented as "nothing happens when I select a document".
                # Nothing in the chain disagreed with the user until someone
                # went looking at the remote player itself.
                log.error("sink-book: could not stage %s for %s — the remote "
                          "player cannot read this host's paths", norm, target.name)
                try:
                    from ..state import StateStore
                    StateStore().log_error(
                        "sink-book", "staging failed; nothing will play",
                        extras={"uri": str(norm), "target": target.name})
                except Exception:  # noqa: BLE001 — reporting must not break playback
                    pass
        if endpoint == self._sock:
            self._ensure_broker()
            device = _device_for(target)
            if device is not None:
                try:
                    ipc.set_property(endpoint, "audio-device", device,
                                     critical=True)
                except ipc.MpvIpcError as e:
                    log.warning("sink-book: set audio-device %s failed: %s", device, e)

        secs = max(0.0, (start_ms or 0) / 1000.0)

        # Inject Audiobookshelf token if URL requires it.
        play_uri = norm
        abs_url, token = _abs_config()
        if "audiobookshelf" in play_uri.lower() or (abs_url and abs_url in play_uri):
            public_abs = _abs_public_url(target, abs_url)
            if public_abs != abs_url:
                play_uri = public_abs + play_uri[len(abs_url):]
            if token and "?" not in play_uri:
                play_uri = f"{play_uri}?token={token}"
            elif token and "token=" not in play_uri:
                play_uri = f"{play_uri}&token={token}"
        # Leave a breadcrumb after final URL rewrite so observers/state agree.
        _write_load_intent(play_uri, start_ms or 0)

        # When MEDIA_BOOK_MOPIDY is on, route the *load* through Mopidy so
        # Iris's history controller records the play. Audio still lands on
        # sink-book.sock (mopidy-mpv is attached to the same broker), and
        # book_observer applies start_ms from resume_pos when it sees the
        # loadfile event — same as Iris-driven plays today.
        if _mopidy_enabled():
            try:
                mopidy.play_uri(mopidy.book_url(), _to_book_uri(norm))
                # mopidy-mpv has no "start" option in tracklist.add, so we
                # apply the resume offset directly on the socket after Mopidy
                # kicks off the load. The observer sees our load-intent and
                # won't re-seek to the bookmark on top of us.
                if secs > 0:
                    self._seek_abs(secs, endpoint)
                return norm
            except mopidy.MopidyRpcError as e:
                log.warning("sink-book: Mopidy RPC failed, falling back to "
                            "direct ipc: %s", e)
        # critical throughout: this call IS the playback. The breaker exists to
        # drop observational chatter, every caller of which treats a skip as
        # "unknown, carry on" — but skipping a loadfile does not degrade
        # playback, it removes it, and the caller has no fallback to reach for.
        # Left non-critical, an open breaker on this endpoint made `book play`
        # return cleanly and produce silence, reported only as "staging failed"
        # from a layer that had nothing to do with it.
        if secs > 0:
            # mpv 0.37: loadfile <url> [<flags> [<options>]] — pass `start`
            # as an option so we resume without racing the async file-load.
            try:
                ipc.command(endpoint, "loadfile", play_uri, "replace",
                            f"start={secs:.3f}", critical=True)
            except (ipc.MpvIpcError, OSError):
                # Arg-order differs across mpv versions; load then seek.
                ipc.command(endpoint, "loadfile", play_uri, "replace",
                            critical=True)
                self._seek_abs(secs, endpoint)
        else:
            ipc.command(endpoint, "loadfile", play_uri, "replace", critical=True)

        # Don't let a lingering pause/mute swallow the start.
        for prop in ("pause", "mute"):
            try:
                ipc.set_property(endpoint, prop, False, critical=True)
            except (ipc.MpvIpcError, OSError):
                pass
        return norm

    def _seek_abs(self, secs: float, endpoint: "str | Path | None" = None) -> None:
        endpoint = endpoint or self._sock
        deadline = time.time() + 1.5
        while time.time() < deadline:
            try:
                ipc.set_property(endpoint, "time-pos", secs)
                return
            except (ipc.MpvIpcError, OSError):
                time.sleep(0.15)

    # Transport is `critical` throughout: the breaker exists to drop the
    # observational chatter that would otherwise delay playback, and every such
    # caller treats a skip as "unknown, carry on". A keypress has no such
    # fallback — skipping it doesn't degrade the control, it removes it, and
    # the user is left pressing pause at an audiobook that keeps talking. The
    # book channel shares the phone's bridge with speech, so it inherits the
    # same open breaker; without this flag a book control was silently dropped
    # for the whole cool-off whenever anything else had tripped it.
    def pause(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.set_property(_socket_for(target), "pause", True, critical=True)
        except (ipc.MpvIpcError, OSError):
            pass

    def resume(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.set_property(_socket_for(target), "pause", False, critical=True)
        except (ipc.MpvIpcError, OSError):
            pass

    def stop(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.command(_socket_for(target), "stop", critical=True)
        except (ipc.MpvIpcError, OSError):
            pass

    def skip(self, seconds: float, target: Target = DEFAULT_TARGET) -> None:
        """Seek ±seconds, clamped by mpv to the file bounds."""
        try:
            ipc.command(_socket_for(target), "seek", seconds, "relative",
                        critical=True)
        except (ipc.MpvIpcError, OSError):
            pass

    def seek_to(self, seconds: float, target: Target = DEFAULT_TARGET) -> Optional[int]:
        """Seek to an absolute position (seconds from the start), clamped by
        mpv to the file bounds. Returns the resulting position in ms."""
        try:
            ipc.command(_socket_for(target), "seek", max(0.0, float(seconds)),
                        "absolute", critical=True)
        except (ipc.MpvIpcError, OSError):
            return None
        return self.position(target)

    def set_speed(self, rate: float, target: Target = DEFAULT_TARGET) -> float:
        rate = max(_MIN_SPEED, min(_MAX_SPEED, float(rate)))
        try:
            ipc.set_property(_socket_for(target), "speed", rate, critical=True)
        except (ipc.MpvIpcError, OSError):
            pass
        return rate

    def speed(self, target: Target = DEFAULT_TARGET) -> Optional[float]:
        try:
            return float(ipc.get_property(_socket_for(target), "speed"))
        except (ipc.MpvIpcError, TypeError, ValueError):
            return None

    def set_volume(self, level: int, target: Target = DEFAULT_TARGET) -> None:
        """Set the book stream volume (0-100). Phase-2 bed mixing uses this."""
        try:
            ipc.set_property(_socket_for(target), "volume", max(0, min(100, level)))
        except (ipc.MpvIpcError, OSError):
            pass

    # ---- observation (spawn-free) ----------------------------------------

    def position(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        try:
            pos = ipc.get_property(_socket_for(target), "time-pos")
        except (ipc.MpvIpcError, OSError):
            return None
        return int(pos * 1000) if pos is not None else None

    def duration(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        try:
            d = ipc.get_property(_socket_for(target), "duration")
        except (ipc.MpvIpcError, OSError):
            return None
        return int(d * 1000) if d is not None else None

    def now_playing_uri(self, target: Target = DEFAULT_TARGET) -> Optional[str]:
        try:
            return ipc.get_property(_socket_for(target), "path")
        except (ipc.MpvIpcError, OSError):
            return None

    def idle(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when no broker is up or nothing is loaded. Never spawns."""
        endpoint = _socket_for(target)
        if endpoint == self._sock and not self._running():
            return True
        try:
            return bool(ipc.get_property(endpoint, "idle-active"))
        except (ipc.MpvIpcError, OSError):
            return True

    def paused(self, target: Target = DEFAULT_TARGET) -> bool:
        try:
            return bool(ipc.get_property(_socket_for(target), "pause"))
        except (ipc.MpvIpcError, OSError):
            return False

    def active(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when a file is loaded and actually playing (not idle, not
        paused). Cheap and spawn-free — the coordinator uses this to decide
        whether a book needs pausing for speech.
        """
        endpoint = _socket_for(target)
        return ((endpoint != self._sock or self._running())
                and not self.idle(target) and not self.paused(target))
