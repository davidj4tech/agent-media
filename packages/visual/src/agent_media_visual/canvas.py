"""The canvas: a tiny SSE-driven full-bleed image page any screen can show.

Stdlib-only HTTP server. Endpoints:

  GET  /          the canvas page (cross-fade + Ken Burns, SSE client)
  GET  /events    Server-Sent Events stream of `show` events
  GET  /img/<f>   serve a generated image from the spool dir
  POST /show      {"image": "<spool filename | absolute URL>",
                   "caption": "...", "prompt": "..."}  → broadcast
  GET  /healthz   liveness

Config (env):
  MEDIA_VISUAL_PORT   listen port (default 8781 — clip server is 8780)
  MEDIA_VISUAL_BIND   bind address (default 0.0.0.0, phone reaches it
                      over the tailnet like the speech clip server)
  MEDIA_VISUAL_DEBUG  "1" to log requests

Point the phone / TV browser at http://<host>:8781/ and leave it open.
A screen that is off just misses the show — nothing depends on it.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8781


def spool_dir() -> Path:
    """Where generated images land: XDG_STATE_HOME/agent-media/visual."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    d = root / "agent-media" / "visual"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Hub:
    """Fan-out of show events to connected SSE clients; remembers the last
    event so a screen that (re)connects immediately shows something."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self.last: dict | None = None

    def attach(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=16)
        with self._lock:
            self._clients.append(q)
        return q

    def detach(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def publish(self, event: dict) -> None:
        with self._lock:
            self.last = event
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # a stalled screen skips frames, never blocks the rest


HUB = Hub()

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>agent-media canvas</title>
<style>
  html, body { margin:0; height:100%; background:#000; overflow:hidden; }
  .layer {
    position: fixed; inset: 0; width: 100vw; height: 100vh;
    object-fit: cover; opacity: 0; transition: opacity 1.8s ease;
    will-change: transform, opacity;
  }
  .layer.on { opacity: 1; }
  /* Ken Burns variants — picked at random per image */
  @keyframes kb1 { from { transform: scale(1.06) translate(0,0); }
                   to   { transform: scale(1.18) translate(-2%,-1.5%); } }
  @keyframes kb2 { from { transform: scale(1.18) translate(1.5%,1%); }
                   to   { transform: scale(1.06) translate(0,0); } }
  @keyframes kb3 { from { transform: scale(1.06) translate(-1.5%,1%); }
                   to   { transform: scale(1.16) translate(1.5%,-1%); } }
  @keyframes kb4 { from { transform: scale(1.16) translate(0,-1.5%); }
                   to   { transform: scale(1.08) translate(-1%,1.5%); } }
  #cap {
    position: fixed; left: 50%; bottom: max(4vh, env(safe-area-inset-bottom));
    transform: translateX(-50%); max-width: 82vw;
    padding: .55em 1.1em; border-radius: 999px;
    font: 15px/1.45 system-ui, sans-serif; color: #eee; text-align: center;
    background: rgba(10,10,10,.55); backdrop-filter: blur(12px);
    opacity: 0; transition: opacity 1s ease; pointer-events: none;
  }
  #cap.on { opacity: 1; }
  #dot {
    position: fixed; top: 12px; right: 12px; width: 8px; height: 8px;
    border-radius: 50%; background: #e05c5c; opacity: 0; transition: opacity .5s;
  }
  #dot.off { opacity: .8; }
</style>
</head>
<body>
<img id="a" class="layer" alt="">
<img id="b" class="layer" alt="">
<div id="cap"></div>
<div id="dot" title="disconnected"></div>
<script>
  const layers = [document.getElementById('a'), document.getElementById('b')];
  const cap = document.getElementById('cap');
  const dot = document.getElementById('dot');
  let front = 0, capTimer = null;
  const KB = ['kb1','kb2','kb3','kb4'];

  function show(d) {
    const back = 1 - front;
    const el = layers[back];
    el.onload = () => {
      const dur = 28 + Math.random() * 14;
      el.style.animation = KB[Math.floor(Math.random()*KB.length)] +
        ' ' + dur.toFixed(1) + 's ease-in-out infinite alternate';
      el.classList.add('on');
      layers[front].classList.remove('on');
      front = back;
      if (d.caption) {
        cap.textContent = d.caption;
        cap.classList.add('on');
        clearTimeout(capTimer);
        capTimer = setTimeout(() => cap.classList.remove('on'), 15000);
      }
    };
    el.src = d.image;
  }

  const es = new EventSource('/events');
  es.onmessage = (e) => {
    try { const d = JSON.parse(e.data); if (d.image) show(d); } catch (_) {}
  };
  es.onopen = () => dot.classList.remove('off');
  es.onerror = () => dot.classList.add('off');

  // Keep a phone/tablet screen awake while the canvas is up (best-effort;
  // needs one tap on some browsers to count as a user gesture).
  let lock = null;
  async function wake() {
    try { lock = await navigator.wakeLock.request('screen'); } catch (_) {}
  }
  wake();
  document.addEventListener('click', wake);
  document.addEventListener('visibilitychange',
    () => { if (!document.hidden) wake(); });
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if os.environ.get("MEDIA_VISUAL_DEBUG") == "1":
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
        elif path == "/events":
            self._sse()
        elif path.startswith("/img/"):
            self._image(path[len("/img/"):])
        else:
            self._send(404, b"not found\n", "text/plain")

    def _image(self, name: str) -> None:
        name = os.path.basename(name)  # no traversal
        f = spool_dir() / name
        if not f.is_file():
            self._send(404, b"no such image\n", "text/plain")
            return
        data = f.read_bytes()
        ctype = "image/webp" if name.endswith(".webp") else \
                "image/png" if name.endswith(".png") else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Spool names are unique per image, safe to cache hard.
        self.send_header("Cache-Control", "max-age=86400, immutable")
        self.end_headers()
        self.wfile.write(data)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = HUB.attach()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            if HUB.last is not None:
                self._event(HUB.last)
            while True:
                try:
                    self._event(q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.detach(q)

    def _event(self, event: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/show":
            self._send(404, b"not found\n", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, b"bad json\n", "text/plain")
            return
        image = str(body.get("image") or "").strip()
        if not image:
            self._send(400, b"missing image\n", "text/plain")
            return
        # A bare spool filename becomes a canvas-relative /img/ URL, so the
        # event works from any screen regardless of how it reached us.
        if "/" not in image:
            image = "/img/" + image
        HUB.publish({
            "image": image,
            "caption": (body.get("caption") or None),
            "prompt": (body.get("prompt") or None),
            "t": int(time.time()),
        })
        self._send(200, b"shown\n", "text/plain")


def main() -> None:
    from agent_media_core.intake._env import load_env_file
    load_env_file("visual-canvas")
    ap = argparse.ArgumentParser(description="agent-media visual canvas (spike)")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("MEDIA_VISUAL_PORT") or DEFAULT_PORT))
    ap.add_argument("--bind", default=os.environ.get("MEDIA_VISUAL_BIND") or "0.0.0.0")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    print(f"canvas on http://{args.bind}:{args.port}/  spool={spool_dir()}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
