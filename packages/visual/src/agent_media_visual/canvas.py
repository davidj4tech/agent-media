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
import re
import shutil
import socket as _socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .state import spool_dir

DEFAULT_PORT = 8781


class Hub:
    """Fan-out of show events to connected SSE clients; remembers the last
    image event (and the latest speech state) so a screen that (re)connects
    immediately shows something."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self.last: dict | None = None        # last *image* event only
        self.last_state: dict | None = None  # latest speech-state event

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

    def watchers(self) -> int:
        with self._lock:
            return len(self._clients)

    def publish(self, event: dict, *, remember: bool = True) -> None:
        with self._lock:
            if remember:
                if event.get("kind") == "state":
                    self.last_state = event
                else:
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


# --- speech-state poller: the canvas reacts to the voice ---------------------
# While at least one screen is connected, poll the speech channel (~1 Hz, one
# `media popup-status` spawn — the popup's own cadence) and broadcast
# {"kind": "state", "speaking": bool, "pos": s, "dur": s} over the SSE stream.
# The page uses it to drive motion (faster Ken Burns + a breathing vignette
# while the voice is talking) and the start/stop sound cues. The local mpv
# socket can't be the source: speech usually plays on the *phone*, and only
# the `media` CLI sees the remote target's state.

_TIME_PAIR = re.compile(r"(?:(\d+):)?(\d+):(\d{2})")


def _parse_clock(text: str) -> list[int]:
    """All H:MM:SS / MM:SS times in `text`, as seconds."""
    out = []
    for h, m, s in _TIME_PAIR.findall(text):
        out.append((int(h or 0)) * 3600 + int(m) * 60 + int(s))
    return out


def speech_state() -> dict:
    """One SSE-shaped speech-state snapshot off `media popup-status`."""
    line = (_media(["popup-status", "--no-bar", "--show-idle"], timeout=5)
            .splitlines() or [""])[0].strip()
    times = _parse_clock(line)
    state: dict = {"kind": "state", "speaking": line.startswith("▶")}
    if len(times) >= 2:
        state["pos"], state["dur"] = times[0], times[1]
    return state


def _state_poller() -> None:
    last_key = None
    while True:
        if HUB.watchers() == 0:
            time.sleep(2)
            continue
        try:
            st = speech_state()
        except Exception:  # noqa: BLE001 — the poller must outlive any hiccup
            time.sleep(2)
            continue
        key = (st["speaking"], st.get("pos"))
        # Broadcast on any change; while speaking, pos ticks every poll, so
        # watchers get a ~1 Hz progress signal without idle-time chatter.
        if key != last_key:
            HUB.publish(st)
            last_key = key
        time.sleep(1)


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
  /* While the voice is talking the scene breathes: a soft vignette swells in
     time-ish with speech (the Ken Burns pan also speeds up, via JS). */
  #pulse {
    position: fixed; inset: 0; pointer-events: none; opacity: 0;
    background: radial-gradient(ellipse at center,
                transparent 55%, rgba(0,0,0,.55) 100%);
    transition: opacity 1s ease;
  }
  body.speaking #pulse { animation: breathe 2.8s ease-in-out infinite; }
  @keyframes breathe { 0%, 100% { opacity: 0; } 50% { opacity: .65; } }
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
<div id="pulse"></div>
<div id="cap"></div>
<div id="dot" title="disconnected"></div>
<div id="ctl">
  <div class="row">
    <button id="chan">♪</button>
    <div id="marq"><span id="title">agent-media</span></div>
    <button id="sfx">🔈</button>
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
      if (speaking)
        for (const a of el.getAnimations())
          (a.updatePlaybackRate ? a.updatePlaybackRate(2.6) : a.playbackRate = 2.6);
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

  // ---- sound effects: tiny synthesized cues, no assets (WebAudio) ----------
  // Whoosh when a new image lands; a two-note chime up when the voice starts,
  // down when it stops. Quiet by design; 🔈 in the controller toggles, state
  // persists per device. Browsers gate audio behind a first user gesture —
  // the same tap that opens the controller / takes the wake lock unlocks it.
  let ac = null;
  function actx() {
    if (!ac) ac = new (window.AudioContext || window.webkitAudioContext)();
    if (ac.state === 'suspended') ac.resume().catch(() => {});
    return ac;
  }
  function sfxOn() { return localStorage.getItem('sfx') !== '0'; }
  function chime(up) {
    if (!sfxOn()) return;
    try {
      const c = actx(), notes = up ? [523, 659] : [659, 523];
      notes.forEach((f, i) => {
        const t = c.currentTime + i * 0.11;
        const o = c.createOscillator(), g = c.createGain();
        o.type = 'sine'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.05, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.4);
        o.connect(g).connect(c.destination);
        o.start(t); o.stop(t + 0.45);
      });
    } catch (_) {}
  }
  function whoosh() {
    if (!sfxOn()) return;
    try {
      const c = actx(), dur = 0.45;
      const buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
      const d = buf.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
      const src = c.createBufferSource(); src.buffer = buf;
      const f = c.createBiquadFilter(); f.type = 'bandpass'; f.Q.value = 1.2;
      const t = c.currentTime;
      f.frequency.setValueAtTime(300, t);
      f.frequency.exponentialRampToValueAtTime(1400, t + dur * 0.7);
      const g = c.createGain();
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.06, t + 0.08);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      src.connect(f).connect(g).connect(c.destination);
      src.start(t); src.stop(t + dur);
    } catch (_) {}
  }

  // ---- audio-reactive motion: the scene moves with the voice ---------------
  // While speaking: pan/zoom runs faster (seamless via updatePlaybackRate)
  // and the vignette breathes (CSS class). State arrives over the SSE stream.
  let speaking = false, speakStartT = 0;
  function setSpeaking(on) {
    if (on === speaking) return;
    speaking = on;
    if (on) speakStartT = Date.now();
    document.body.classList.toggle('speaking', on);
    for (const el of layers)
      for (const a of el.getAnimations())
        (a.updatePlaybackRate ? a.updatePlaybackRate(on ? 2.6 : 1)
                              : a.playbackRate = on ? 2.6 : 1);
    chime(on);
    if (!on && seq) setBeat(seq.length - 1);   // speech over → the conclusion
  }

  // ---- beats: a sequence of images that flips in step with the voice -------
  // The pusher sends per-beat start fractions plus an estimated spoken
  // duration; progress = elapsed time since the voice started (or since
  // generation began, for a screen that joined mid-reply) over that estimate.
  // Speech ending parks the canvas on the final beat, whatever the estimate
  // got wrong.
  let seq = null, seqIdx = -1, seqBase = 0, seqEst = 0, seqCap = null;
  function tick() {
    if (!sfxOn()) return;
    try {
      const c = actx(), t = c.currentTime;
      const o = c.createOscillator(), g = c.createGain();
      o.type = 'sine'; o.frequency.value = 880;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.03, t + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.09);
      o.connect(g).connect(c.destination);
      o.start(t); o.stop(t + 0.1);
    } catch (_) {}
  }
  function setBeat(i) {
    if (!seq || i === seqIdx || !seq[i]) return;
    const first = seqIdx < 0;
    seqIdx = i;
    show({ image: seq[i].image, caption: first ? seqCap : null });
    first ? whoosh() : tick();
  }
  function applySeq() {
    if (!seq || seqIdx >= seq.length - 1 || !speaking || seqEst <= 0) return;
    const frac = (Date.now() - seqBase) / 1000 / seqEst;
    let idx = 0;
    for (let i = 0; i < seq.length; i++) if (frac >= seq[i].at) idx = i;
    if (idx > seqIdx) setBeat(idx);
  }
  setInterval(applySeq, 1000);

  const es = new EventSource('/events');
  es.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.kind === 'state') { setSpeaking(!!d.speaking); applySeq(); }
      else if (d.sequence) {
        seq = d.sequence; seqIdx = -1; seqEst = d.estdur || 0;
        seqCap = d.caption || null;
        // Anchor progress to the real speech start when we saw it; else
        // reconstruct it from how long generation took.
        seqBase = (speaking && speakStartT)
          ? speakStartT : Date.now() - (d.gen_secs || 0) * 1000;
        // If the voice already finished — generation outlasted a short reply,
        // or this is a replay to a late-joining screen — park on the
        // conclusion instead of restarting the story from beat 0.
        const elapsed = (Date.now() - seqBase) / 1000;
        if (!speaking && seqEst > 0 && elapsed > seqEst) setBeat(seq.length - 1);
        else { setBeat(0); applySeq(); }
      }
      else if (d.image) { seq = null; seqIdx = -1; show(d); whoosh(); }
    } catch (_) {}
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
  function drawSfx() { $('sfx').textContent = sfxOn() ? '🔈' : '🔇'; }
  drawSfx();
  $('sfx').onclick = (e) => {
    e.stopPropagation();
    localStorage.setItem('sfx', sfxOn() ? '0' : '1');
    drawSfx();
    if (sfxOn()) chime(true);              // audible confirmation + unlocks audio
    resetHide();
  };
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
                "image/svg+xml" if name.endswith(".svg") else \
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
            if HUB.last_state is not None:
                self._event(HUB.last_state)
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

        # A bare spool filename becomes a canvas-relative /img/ URL, so the
        # event works from any screen regardless of how it reached us.
        def ref(img: str) -> str:
            return img if "/" in img else "/img/" + img

        event: dict = {
            "caption": (body.get("caption") or None),
            "prompt": (body.get("prompt") or None),
            "t": int(time.time()),
        }
        seq = body.get("sequence")
        if isinstance(seq, list) and seq:
            beats = []
            for entry in seq:
                img = str((entry or {}).get("image") or "").strip()
                if not img:
                    continue
                try:
                    at = max(0.0, min(1.0, float(entry.get("at") or 0)))
                except (TypeError, ValueError):
                    at = 0.0
                beats.append({"image": ref(img), "at": at})
            if not beats:
                self._send(400, b"empty sequence\n", "text/plain")
                return
            event["sequence"] = beats
            for k in ("estdur", "gen_secs"):
                try:
                    event[k] = max(0.0, float(body.get(k) or 0))
                except (TypeError, ValueError):
                    pass
        else:
            image = str(body.get("image") or "").strip()
            if not image:
                self._send(400, b"missing image\n", "text/plain")
                return
            event["image"] = ref(image)
        HUB.publish(event)
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
    threading.Thread(target=_state_poller, daemon=True).start()
    print(f"canvas on http://{args.bind}:{args.port}/  spool={spool_dir()}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
