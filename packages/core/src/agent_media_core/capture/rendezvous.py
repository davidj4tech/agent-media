"""Transcript rendezvous — the listening half of `converse`.

`converse` (mcp_server) speaks a question and then blocks here waiting for the
human's spoken reply. The reply arrives the way every transcript already
arrives: HA Assist transcribes, HA's conversation agent POSTs to
tmux-voice-bridge's OpenAI-compatible endpoint. voice-bridge's `do_inject()`
calls `offer()` below; if a converse call is waiting, it takes the text and
voice-bridge skips its usual tmux keystroke injection.

media-mcp and voice-bridge are separate processes on the same host, so the
handoff crosses a process boundary. A unix socket in XDG_RUNTIME_DIR beats a
TCP port (no binding decisions, no auth surface, dies with the login session)
and beats a file plus polling (no latency floor).

Fails safe in both directions: no socket, a stale socket, or any error means
`offer()` returns False and voice-bridge injects into tmux exactly as it does
today. A human's words are never dropped on the floor.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
from pathlib import Path


log = logging.getLogger(__name__)

_ACK = b'{"ok":true}\n'


def socket_path() -> Path:
    """Where the rendezvous lives. Override with MEDIA_CONVERSE_SOCK."""
    override = os.environ.get("MEDIA_CONVERSE_SOCK")
    if override:
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "agent-media" / "converse.sock"


class Busy(RuntimeError):
    """Another converse call already holds the rendezvous."""


def _is_live(path: Path) -> bool:
    """True if something is actually accepting on `path` (vs. a stale inode)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(path))
        return True
    except OSError:
        return False


class Rendezvous:
    """Server side. Armed by `converse` for the duration of one question.

        with Rendezvous(timeout_s=90) as rv:
            reply = rv.wait()      # str, or None on timeout
    """

    def __init__(self, timeout_s: float = 90.0) -> None:
        self.timeout_s = timeout_s
        self._srv: socket.socket | None = None

    def __enter__(self) -> "Rendezvous":
        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _is_live(path):
                raise Busy(f"a converse call is already waiting on {path}")
            # Stale socket from a media-mcp that died mid-converse.
            path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(path))
        except OSError as exc:
            srv.close()
            if exc.errno == errno.EADDRINUSE:
                raise Busy(f"rendezvous {path} taken") from exc
            raise
        srv.listen(1)
        srv.settimeout(self.timeout_s)
        self._srv = srv
        return self

    def wait(self) -> str | None:
        """Block for one transcript. Returns the text, or None on timeout."""
        assert self._srv is not None, "use Rendezvous as a context manager"
        try:
            conn, _ = self._srv.accept()
        except socket.timeout:
            return None
        with conn:
            conn.settimeout(5.0)
            buf = b""
            try:
                while b"\n" not in buf and len(buf) < 64 * 1024:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                return None
            if not buf:
                return None
            try:
                text = json.loads(buf.split(b"\n", 1)[0].decode()).get("text", "")
            except (ValueError, UnicodeDecodeError):
                log.warning("converse: unparseable rendezvous payload")
                return None
            text = str(text).strip()
            if not text:
                return None
            # Ack only after we have usable text: an un-acked offer() tells
            # voice-bridge to fall back to tmux injection.
            try:
                conn.sendall(_ACK)
            except OSError:
                # We have the text but the peer vanished. voice-bridge will
                # also inject it into tmux. Duplicated input beats lost input.
                log.warning("converse: peer closed before ack")
            return text

    def __exit__(self, *exc) -> None:
        if self._srv is not None:
            self._srv.close()
            self._srv = None
        socket_path().unlink(missing_ok=True)


def offer(text: str, timeout_s: float = 2.0) -> bool:
    """Client side, called from voice-bridge's `do_inject()`.

    Returns True if a waiting `converse` call took the transcript — meaning the
    caller must NOT also type it into the tmux pane. False means nobody is
    listening and normal injection should proceed.
    """
    if not text or not text.strip():
        return False
    path = socket_path()
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            s.connect(str(path))
            s.sendall(json.dumps({"text": text}).encode() + b"\n")
            return s.recv(len(_ACK) + 16).startswith(b'{"ok"')
    except OSError:
        # Stale socket, media-mcp restarted mid-converse, or a slow peer.
        # Fall through to tmux injection rather than swallowing the words.
        return False
