"""Regression tests for _mpv_ipc.get_properties.

An idle mpv answers some properties with success and others with
"property unavailable" (time-pos/duration when nothing is loaded). The
snapshot must return as soon as every request is *answered* — not spin
until the timeout waiting for the unavailable ones to succeed, which made
every popup / status-bar redraw pay a ~2s stall.
"""

import json
import socket
import threading
import time

from agent_media_core.sinks import _mpv_ipc as ipc


def _fake_mpv(sock_path, available):
    """A one-shot Unix-socket mpv stub: reads pipelined get_property requests
    and replies success for names in `available`, "property unavailable"
    otherwise. Never sends anything for the unavailable ones beyond the error
    reply, so a caller that waits for them to succeed would hang to timeout."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            buf = b""
            seen = 0
            # Expect 7 requests (the snapshot's property list).
            while seen < 7:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    msg = json.loads(line.decode())
                    name = msg["command"][1]
                    rid = msg["request_id"]
                    seen += 1
                    if name in available:
                        reply = {"error": "success", "data": f"val-{name}",
                                 "request_id": rid}
                    else:
                        reply = {"error": "property unavailable",
                                 "request_id": rid}
                    conn.sendall((json.dumps(reply) + "\n").encode())
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def test_snapshot_returns_when_all_answered_not_all_success(tmp_path):
    sock = tmp_path / "mpv.sock"
    props = ["idle-active", "time-pos", "duration", "pause", "mute", "speed",
             "playlist-pos"]
    # Mimic idle mpv: time-pos/duration unavailable, the rest succeed.
    available = set(props) - {"time-pos", "duration"}
    _fake_mpv(sock, available)
    # Wait for the server to bind.
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.01)

    start = time.time()
    out = ipc.get_properties(sock, props, timeout=2.0)
    elapsed = time.time() - start

    # Only the successful ones come back...
    assert out == {n: f"val-{n}" for n in available}
    assert "time-pos" not in out and "duration" not in out
    # ...and crucially we didn't wait out the 2s deadline for the missing ones.
    assert elapsed < 1.0, f"snapshot stalled ({elapsed:.2f}s) — regressed to deadline wait"
