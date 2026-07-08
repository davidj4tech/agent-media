"""The canvas: a tiny SSE-driven full-bleed image page any screen can show.

Stdlib-only HTTP server. Endpoints:

  GET  /          the canvas page (cross-fade + Ken Burns, SSE client,
                  tap-to-reveal audio controller)
  GET  /events    Server-Sent Events stream of `show` events
  GET  /img/<f>   serve a generated image from the spool dir
  POST /show      {"image": "<spool filename | absolute URL>",
                   "caption": "...", "prompt": "..."}  → broadcast
  GET  /status?channel=speech|music|book   controller state (shells to
                  the `media` CLI, same one-code-path as the tmux popup)
  POST /ctl       {"channel": ..., "action": ..., "arg": ...} → run a
                  whitelisted `media` transport command
  GET  /healthz   liveness

Config (env):
  MEDIA_VISUAL_PORT   listen port (default 8781 — clip server is 8780)
  MEDIA_VISUAL_BIND   bind address (default 0.0.0.0; the shipped systemd
                      unit passes the Tailscale IP for a tailnet-only bind)
  MEDIA_VISUAL_DEBUG  "1" to log requests

Point the phone / TV browser at http://<host>:8781/ and leave it open.
A screen that is off just misses the show — nothing depends on it.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import socket as _socket
import subprocess
import sys
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


# --- audio controller backend: shell to the `media` CLI ----------------------
# One code path with the tmux popup: every button runs the same CLI verb the
# popup's hotkey runs, on this host — where `media` already resolves the
# remote speech target (the phone), Mopidy, and the book socket.

def _media_bin() -> str:
    exe = shutil.which("media")
    if exe:
        return exe
    # Installed alongside us in the same venv even when PATH is bare (systemd).
    return str(Path(sys.executable).parent / "media")


def _media(args: list[str], timeout: int = 10) -> str:
    try:
        out = subprocess.run([_media_bin(), *args], capture_output=True,
                             text=True, timeout=timeout)
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _book_title() -> str:
    """The book channel's media-title straight off its mpv IPC socket (the
    popup does the same via socat) — `media book now` is a bare URI."""
    sock = (os.environ.get("MEDIA_BOOK_SOCKET")
            or str(Path.home() / ".local/state/agent-media/sink-book.sock"))
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(sock)
            s.sendall(b'{"command":["get_property","media-title"]}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            for line in buf.decode("utf-8", "replace").splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "data" in d:
                    return " ".join(str(d["data"]).split())
    except OSError:
        pass
    return ""


def channel_status(channel: str) -> dict:
    """Controller snapshot for one channel: marquee label + progress line +
    indicator flags. Mirrors the popup's fetch()."""
    muted = False
    part = ""
    if channel == "music":
        status = _media(["music", "status", "--show-idle", "--no-bar"])
        label = " ".join(_media(["music", "now"]).split()) or "(no music)"
    elif channel == "book":
        status = _media(["book", "status", "--show-idle", "--no-bar"])
        label = _book_title() or "(audiobook)"
    else:
        out = _media(["popup-status", "--show-idle", "--no-bar"])
        lines = (out.splitlines() + ["", "", ""])[:3]
        status, label, _mutecount = (ln.strip() for ln in lines)
        label = label or "agent-media"
    if "[M]" in status:
        muted = True
        status = status.replace(" [M]", "").replace("[M]", "").strip()
    return {"channel": channel, "label": label, "status": status or "○",
            "muted": muted, "part": part}


def ctl_argv(channel: str, action: str, arg: int) -> list[str] | None:
    """Whitelisted button → `media` argv. None = unknown/unsupported combo.
    The maps mirror the popup's handle_key dispatch."""
    if channel == "speech":
        table = {
            "toggle": ["toggle"],
            "prev": ["replay-prev", "--idx", str(arg)],
            "replay": ["replay", str(arg)],
            "jump-end": ["jump", "end"],
            "vol-": ["volume", "-5"],
            "vol+": ["volume", "5"],
            "mute": ["mute"],
            "speed-": ["speed", "down"],
            "speed+": ["speed", "up"],
            "speed0": ["speed", "reset"],
        }
        return table.get(action)
    if channel in ("music", "book"):
        table = {
            "prev": [channel, "prev", "--restart-first"],
            "next": [channel, "next"],
            "vol-": [channel, "volume", "-5"],
            "vol+": [channel, "volume", "5"],
        }
        if action == "toggle":
            if channel == "music":
                return ["music", "toggle"]
            # The book channel has no `toggle`; derive it like the popup does:
            # only an actively-playing status pauses — paused OR idle resumes
            # (pausing an already-stopped channel is a no-op that strands you).
            status = _media(["book", "status", "--no-bar"])
            return ["book", "pause"] if status.startswith("▶") else ["book", "resume"]
        return table.get(action)
    return None


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
  #cap.hide { opacity: 0; }
  #dot {
    position: fixed; top: 12px; right: 12px; width: 8px; height: 8px;
    border-radius: 50%; background: #e05c5c; opacity: 0; transition: opacity .5s;
  }
  #dot.off { opacity: .8; }
  /* --- audio controller (tap anywhere to reveal) — kin of the tmux popup --- */
  #ctl {
    position: fixed; left: 50%; bottom: max(3vh, env(safe-area-inset-bottom));
    transform: translate(-50%, 130%); width: min(92vw, 460px);
    padding: .5em .8em; border-radius: 22px;
    background: rgba(10,10,10,.62); backdrop-filter: blur(14px);
    color: #eee; font: 15px/1.4 system-ui, sans-serif;
    transition: transform .35s ease, opacity .35s ease; opacity: 0;
  }
  #ctl.on { transform: translate(-50%, 0); opacity: 1; }
  #ctl .row { display: flex; align-items: center; gap: .2em; }
  #ctl button {
    background: none; border: 0; color: #eee; font-size: 21px;
    min-width: 44px; min-height: 42px; border-radius: 12px;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  #ctl button:active { background: rgba(255,255,255,.14); }
  #ctl button.lit { color: #ffd75f; }
  #marq { flex: 1; overflow: hidden; white-space: nowrap; }
  #title { display: inline-block; padding-left: 0; }
  #title.scroll { animation: marq var(--marq-dur, 14s) linear infinite; }
  @keyframes marq {
    0%, 12%  { transform: translateX(0); }
    88%, 100% { transform: translateX(var(--marq-shift, -40%)); }
  }
  #clock { flex: 1; text-align: center; font-variant-numeric: tabular-nums;
           color: #ddd; white-space: nowrap; overflow: hidden; }
</style>
</head>
<body>
<img id="a" class="layer" alt="">
<img id="b" class="layer" alt="">
<div id="cap"></div>
<div id="dot" title="disconnected"></div>
<div id="ctl">
  <div class="row">
    <button id="chan">♪</button>
    <div id="marq"><span id="title">agent-media</span></div>
    <button id="xbtn">×</button>
  </div>
  <div class="row">
    <button id="prev">⏮</button>
    <button id="pp">▶</button>
    <span id="clock">○</span>
    <button id="next">⏭</button>
    <button id="vdn">−</button>
    <button id="vup">+</button>
    <button id="sdn" class="sp">🐢</button>
    <button id="sup" class="sp">⏩</button>
    <button id="mute" class="sp">🔇</button>
  </div>
</div>
<script>
  const $ = (id) => document.getElementById(id);
  const layers = [$('a'), $('b')];
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
        $('cap').textContent = d.caption;
        $('cap').classList.add('on');
        clearTimeout(capTimer);
        capTimer = setTimeout(() => $('cap').classList.remove('on'), 15000);
      }
    };
    el.src = d.image;
  }

  const es = new EventSource('/events');
  es.onmessage = (e) => {
    try { const d = JSON.parse(e.data); if (d.image) show(d); } catch (_) {}
  };
  es.onopen = () => $('dot').classList.remove('off');
  es.onerror = () => $('dot').classList.add('off');

  // Keep a phone/tablet screen awake while the canvas is up (best-effort;
  // needs one tap on some browsers to count as a user gesture).
  let lock = null;
  async function wake() {
    try { lock = await navigator.wakeLock.request('screen'); } catch (_) {}
  }
  wake();
  document.addEventListener('visibilitychange',
    () => { if (!document.hidden) wake(); });

  // ---- audio controller: same verbs as the tmux popup, as touch buttons ----
  const GLYPH = { speech: '♪', music: '♫', book: '☰' };
  const ORDER = ['speech', 'music', 'book'];
  let ch = 'speech', histIdx = 1, visible = false;
  let hideTimer = null, pollTimer = null;

  function speechOnly(showIt) {
    for (const el of document.querySelectorAll('#ctl .sp'))
      el.style.display = showIt ? '' : 'none';
  }

  function render(d) {
    $('chan').textContent = GLYPH[ch];
    const t = $('title');
    if (t.textContent !== d.label) {
      t.textContent = d.label;
      t.classList.remove('scroll');
      requestAnimationFrame(() => {           // measure after reflow
        const over = t.scrollWidth - $('marq').clientWidth;
        if (over > 8) {
          t.style.setProperty('--marq-shift', (-over) + 'px');
          t.style.setProperty('--marq-dur', (8 + over / 20) + 's');
          t.classList.add('scroll');
        }
      });
    }
    // The play/pause BUTTON shows the action it triggers (popup convention):
    // playing (▶ status) → show pause, else show play.
    const playing = d.status.startsWith('▶');
    $('pp').textContent = playing ? '‖' : '▶';
    $('clock').textContent = d.status.replace(/^[▶⏸○]\\s*/, '') || '○';
    $('mute').classList.toggle('lit', !!d.muted);
    speechOnly(ch === 'speech');
  }

  async function poll() {
    if (!visible) return;
    try {
      const d = await fetch('/status?channel=' + ch).then(r => r.json());
      if (d.channel === ch) render(d);
    } catch (_) {}
  }

  function resetHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideCtl, 12000);
  }

  function showCtl() {
    visible = true;
    $('ctl').classList.add('on');
    $('cap').classList.add('hide');
    poll();
    clearInterval(pollTimer);
    pollTimer = setInterval(poll, 2000);
    resetHide();
  }

  function hideCtl() {
    visible = false;
    $('ctl').classList.remove('on');
    $('cap').classList.remove('hide');
    clearInterval(pollTimer);
    clearTimeout(hideTimer);
  }

  async function act(action, arg) {
    resetHide();
    try {
      const r = await fetch('/ctl', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel: ch, action: action, arg: arg }),
      }).then(r => r.json());
      // Speech ⏮ semantics ride on replay-prev's echoed cursor (the popup
      // folds the same echo into hist_idx).
      if (action === 'prev' && ch === 'speech' && r.out && /^\\d+$/.test(r.out))
        histIdx = parseInt(r.out, 10);
    } catch (_) {}
    setTimeout(poll, 300);                     // let the action land, then refresh
  }

  document.body.addEventListener('click', (e) => {
    wake();
    if ($('ctl').contains(e.target)) { resetHide(); return; }
    visible ? hideCtl() : showCtl();
  });
  $('xbtn').onclick = (e) => { e.stopPropagation(); hideCtl(); };
  $('chan').onclick = () => {
    ch = ORDER[(ORDER.indexOf(ch) + 1) % ORDER.length];
    histIdx = 1;
    $('title').textContent = '…';
    poll();
    resetHide();
  };
  $('pp').onclick  = () => act('toggle');
  $('vdn').onclick = () => act('vol-');
  $('vup').onclick = () => act('vol+');
  $('sdn').onclick = () => act('speed-');
  $('sup').onclick = () => act('speed+');
  $('mute').onclick = () => act('mute');
  $('prev').onclick = () => act('prev', histIdx);
  $('next').onclick = () => {
    if (ch !== 'speech') { act('next'); return; }
    if (histIdx > 1) { histIdx -= 1; act('replay', histIdx); }
    else act('jump-end');
  };
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

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
        elif path == "/events":
            self._sse()
        elif path == "/status":
            channel = "speech"
            for kv in query.split("&"):
                k, _, v = kv.partition("=")
                if k == "channel" and v in ("speech", "music", "book"):
                    channel = v
            self._json(200, channel_status(channel))
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
        path = self.path.split("?", 1)[0]
        if path == "/show":
            self._show()
        elif path == "/ctl":
            self._ctl()
        else:
            self._send(404, b"not found\n", "text/plain")

    def _read_json(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def _show(self) -> None:
        body = self._read_json()
        if body is None:
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

    def _ctl(self) -> None:
        body = self._read_json()
        if body is None:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        channel = str(body.get("channel") or "")
        action = str(body.get("action") or "")
        try:
            arg = max(1, min(999, int(body.get("arg") or 1)))
        except (TypeError, ValueError):
            arg = 1
        argv = ctl_argv(channel, action, arg)
        if argv is None:
            self._json(400, {"ok": False, "err": "unknown action"})
            return
        out = _media(argv)
        self._json(200, {"ok": True, "out": out})


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
