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

Nothing here is voice-specific. `media converse-reply` calls the same `offer()`
with typed text, which is the only route for an answerer who cannot reach HA
Assist's microphone — another agent, over the relay. Such an answerer also
cannot *hear* the question, so arming publishes it as a sidecar (see
`question_path`).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import time
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


def question_path() -> Path:
    """Sidecar naming the armed question, beside the socket.

    The question is *spoken*, so an answerer who cannot hear (an agent replying
    over the relay rather than through HA Assist) has no other way to know what
    is being asked — or that anything is. Written on arm, removed on disarm.
    """
    return socket_path().with_suffix(".question")


def pending_question() -> dict | None:
    """The question currently awaiting an answer, or None.

    None when nothing is armed, when the sidecar is unreadable, and — the case
    that matters — when the sidecar outlived its socket, since a media-mcp that
    died mid-converse leaves a question nobody is listening for.
    """
    if not socket_path().exists():
        return None
    try:
        data = json.loads(question_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("text") else None


def wait_for_question(timeout_s: float, poll_s: float = 0.3) -> dict | None:
    """Block until a question is armed, or `timeout_s` elapses.

    For an answerer who has just been told there's a question but whose own
    channel is slow: a relay command is already 5s-granular, so without this
    it races the arm and gets "nothing waiting" for a question that lands a
    second later. Polling (not inotify) because the caller's own latency floor
    dwarfs the difference and this has no daemon to keep alive.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        q = pending_question()
        if q is not None:
            return q
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)


def _mirror(action: str, ttl_s: float | None = None, note: str = "") -> None:
    """Best-effort publish to the relay's floor mirror. Never load-bearing.

    Wrapped and lazy for the same reason the speech side is: the rendezvous
    must work identically on a host with no relay installed, and a mirror that
    can fail an arm would be worse than no mirror.
    """
    try:
        from .floor import publish
        publish("input", os.environ.get("MEDIA_FLOOR_OWNER", "sam"),
                action, ttl_s, note)
    except Exception:  # noqa: BLE001
        pass


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

    def __init__(self, timeout_s: float = 90.0,
                 question: str | None = None) -> None:
        self.timeout_s = timeout_s
        self.question = question
        self._srv: socket.socket | None = None

    def __enter__(self) -> "Rendezvous":
        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _is_live(path):
                raise Busy(f"a converse call is already waiting on {path}")
            # Stale socket from a media-mcp that died mid-converse.
            path.unlink(missing_ok=True)
        # Published *before* the bind, because the socket appearing is what
        # tells an answerer to look: the other order leaves a window where the
        # rendezvous is armed and the question reads as absent.
        self._write_question()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(path))
        except OSError as exc:
            srv.close()
            question_path().unlink(missing_ok=True)
            if exc.errno == errno.EADDRINUSE:
                raise Busy(f"rendezvous {path} taken") from exc
            raise
        srv.listen(1)
        srv.settimeout(self.timeout_s)
        self._srv = srv
        # The input channel's half of the floor mirror: an armed rendezvous is
        # a claim on David's next utterance, and until now the only way to see
        # one was to be on this host and stat a socket.
        _mirror("arm", self.timeout_s, self.question or "")
        return self

    def _write_question(self) -> None:
        """Publish the sidecar. Best-effort: never fail the arm over it."""
        if not self.question:
            return
        qp = question_path()
        tmp = qp.with_suffix(".question.tmp")
        try:
            tmp.write_text(json.dumps({
                "text": self.question,
                "asked_at": time.time(),
                "timeout_s": self.timeout_s,
            }))
            tmp.replace(qp)          # atomic: a reader sees whole JSON or none
        except OSError:
            log.warning("converse: could not write %s", qp)
            tmp.unlink(missing_ok=True)

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
        question_path().unlink(missing_ok=True)
        _mirror("disarm")


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
