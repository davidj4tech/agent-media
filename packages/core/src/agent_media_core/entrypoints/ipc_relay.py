"""A local door to a far player, with the connection already open behind it.

Every `media` process is short-lived, so each reply opened its own TCP
connections to the phone's speech player — one round trip each before a byte
could be sent (430 ms from red5 to p8a on 27 Sep 2026), and a whole second
more whenever the first packet was lost, which on that link was one connect in
six. This keeps a few connections to the player open in advance ("spares") and
listens on loopback: a client that connects here is joined to a spare at once
and the bytes are passed through untouched, so the player sees what it always
saw and the client pays a local connect.

One spare per client, never shared: the player's replies, events and
observations are per connection, and a spare is closed with its client. Spares
are recycled after `--max-age` seconds, so a connection a dozing phone or a
NAT has quietly dropped is never the one handed out; one that has already
closed is noticed before it is used.

The link's round trip is still what a call costs, and the breaker in
`sinks/_mpv_ipc.py` sizes its budget from connect times — which through here
are ~0 ms. So the relay writes the far side's connect time where `_mpv_ipc`
looks for it (see `rtt_path`).

    media-ipc-relay --listen 127.0.0.1:16614 --upstream p8a:6614
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path

log = logging.getLogger("media-ipc-relay")

_SAMPLES = 8


def rtt_path(listen_port: int) -> Path:
    """Where the relay on `listen_port` publishes its far side's round trip."""
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "agent-media" / "relay" / f"{listen_port}.json"


def _hostport(s: str) -> tuple[str, int]:
    host, _, port = s.rpartition(":")
    return host or "127.0.0.1", int(port)


class Relay:
    def __init__(self, listen: tuple[str, int], upstream: tuple[str, int],
                 spares: int = 2, max_age: float = 45.0,
                 connect_timeout: float = 5.0):
        self.listen = listen
        self.upstream = upstream
        self.want = max(0, spares)
        self.max_age = max_age
        self.connect_timeout = connect_timeout
        self._spares: list[tuple[socket.socket, float]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._connects: list[float] = []
        self.server: socket.socket | None = None

    # --- the far side ---------------------------------------------------------

    def _connect(self) -> socket.socket:
        t0 = time.monotonic()
        s = socket.create_connection(self.upstream, timeout=self.connect_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(None)
        took = time.monotonic() - t0
        with self._lock:
            self._connects = (self._connects + [took])[-_SAMPLES:]
            best = min(self._connects)
        self._publish(best)
        return s

    def _publish(self, rtt_s: float) -> None:
        if not self.server:
            return
        p = rtt_path(self.server.getsockname()[1])
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"upstream": "%s:%d" % self.upstream,
                                       "rtt_s": rtt_s, "at": time.time()}))
            os.replace(tmp, p)
        except OSError as e:
            log.debug("publishing rtt: %s", e)

    @staticmethod
    def _closed(s: socket.socket) -> bool:
        """Has the far side closed this idle spare? A peek, never a wait."""
        try:
            s.setblocking(False)
            try:
                return s.recv(1, socket.MSG_PEEK) == b""
            finally:
                s.setblocking(True)
        except BlockingIOError:
            return False
        except OSError:
            return True

    @staticmethod
    def _drain(s: socket.socket) -> None:
        """Drop what arrived while it sat idle: nobody asked for it."""
        try:
            s.setblocking(False)
            try:
                while s.recv(65536):
                    pass
            except BlockingIOError:
                pass
            finally:
                s.setblocking(True)
        except OSError:
            pass

    def take(self) -> socket.socket:
        """A live connection to the player: a spare if one is good, else new."""
        now = time.monotonic()
        got, stale = None, []
        with self._lock:
            while self._spares and got is None:
                s, born = self._spares.pop()
                if now - born <= self.max_age and not self._closed(s):
                    got = s
                else:
                    stale.append(s)
        for s in stale:
            _close(s)
        self._wake.set()        # replace what was taken
        if got is not None:
            self._drain(got)
            return got
        return self._connect()

    def _keep_spares(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            now = time.monotonic()
            old = []
            with self._lock:
                keep = [(s, b) for s, b in self._spares if now - b <= self.max_age]
                old = [s for s, b in self._spares if now - b > self.max_age]
                self._spares = keep
                short = self.want - len(self._spares)
            for s in old:
                _close(s)
            try:
                for _ in range(max(0, short)):
                    s = self._connect()
                    with self._lock:
                        self._spares.append((s, time.monotonic()))
                backoff = 1.0
            except OSError as e:
                log.info("upstream %s:%d unreachable: %s", *self.upstream, e)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self._wake.wait(timeout=max(1.0, self.max_age / 3))
            self._wake.clear()

    # --- the near side --------------------------------------------------------

    def _serve(self, client: socket.socket) -> None:
        try:
            far = self.take()
        except OSError as e:
            log.info("no connection to the player for a client: %s", e)
            _close(client)
            return
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        done = threading.Event()

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    b = src.recv(65536)
                    if not b:
                        break
                    dst.sendall(b)
            except OSError:
                pass
            finally:
                # One side is finished: let the other see the end, then both go.
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                if done.is_set():
                    _close(src)
                    _close(dst)
                done.set()

        threading.Thread(target=pump, args=(far, client), daemon=True).start()
        pump(client, far)

    def serve_forever(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(self.listen)
        srv.listen(32)
        self.server = srv
        threading.Thread(target=self._keep_spares, name="spares", daemon=True).start()
        log.info("relaying %s:%d -> %s:%d with %d spare(s)",
                 *srv.getsockname()[:2], *self.upstream, self.want)
        while not self._stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                if self._stop.is_set():
                    break
                raise
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.server:
            _close(self.server)
        with self._lock:
            spares, self._spares = self._spares, []
        for s, _ in spares:
            _close(s)


def _close(s: socket.socket) -> None:
    try:
        s.close()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="media-ipc-relay", description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", default="127.0.0.1:16614")
    ap.add_argument("--upstream", required=True, help="host:port of the player")
    ap.add_argument("--spares", type=int, default=2)
    ap.add_argument("--max-age", type=float, default=45.0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    Relay(_hostport(a.listen), _hostport(a.upstream), a.spares, a.max_age).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
