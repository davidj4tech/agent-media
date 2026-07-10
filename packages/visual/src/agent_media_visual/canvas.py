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
        self.last_video: dict | None = None  # latest video-sync event

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
                elif event.get("kind") == "video":
                    self.last_video = event
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


# --- the input box backend: reply to whoever just spoke ----------------------
# POST /input types text into a Claude session: the *last speaker's* tmux pane
# by default (the same source_pane the popup's go-to-source uses), or a named
# amux session via `amux send`. This is remote keystroke injection, so unlike
# the transport controls it requires amux's own auth token — one credential
# for both dashboards (~/.amux/auth_token / AMUX_AUTH_TOKEN).

def _amux_token() -> str:
    tok = os.environ.get("AMUX_AUTH_TOKEN", "")
    if tok:
        return "" if tok.lower() == "none" else tok
    try:
        return (Path.home() / ".amux" / "auth_token").read_text().strip()
    except OSError:
        return ""


def _authorized(handler: "Handler") -> bool:
    token = _amux_token()
    if not token:
        return False  # no token configured → the input surface stays closed
    got = (handler.headers.get("X-Auth-Token")
           or (handler.headers.get("Authorization") or "").removeprefix("Bearer").strip())
    return got == token


# --- one-time pairing: install the token into a device's localStorage ----------
# Typing a 40-char token on a phone keyboard is miserable, so `/pair?c=<code>`
# does it: a ONE-TIME code minted host-side (written to the state dir by
# whoever has shell access — no HTTP path can create one) unlocks a page that
# stores the amux token in localStorage and redirects to the canvas. The code
# file is deleted on first use and expires after PAIR_TTL_S regardless.

PAIR_TTL_S = 600


def _pair_code_path() -> Path:
    return spool_dir() / "pair-code"


def _pair_consume(code: str) -> bool:
    """True (and burn the code) when `code` matches a fresh minted one."""
    p = _pair_code_path()
    try:
        minted = p.read_text().strip()
        fresh = (time.time() - p.stat().st_mtime) <= PAIR_TTL_S
    except OSError:
        return False
    if not code or not minted or not fresh or code != minted:
        return False
    try:
        p.unlink()
    except OSError:
        pass
    return True


_PAIR_PAGE = """<!doctype html><meta charset="utf-8">
<body style="background:#000;color:#eee;font:16px system-ui">
<script>
  localStorage.setItem('amux_token', %s);
  location.replace('/');
</script>
paired — loading the canvas…
</body>"""


def _amux_bin() -> str:
    """amux lives in ~/.local/bin, which a systemd user service's default
    PATH doesn't include."""
    return (shutil.which("amux")
            or str(Path.home() / ".local" / "bin" / "amux"))


def _amux_sessions() -> list[dict]:
    """Parse `amux ls`: columns are id, name, dir[, mode]."""
    out = _media_run([_amux_bin(), "ls"])
    sessions = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0].isdigit():
            sessions.append({"name": fields[1], "dir": fields[2]})
    return sessions


def _media_run(argv: list[str], timeout: int = 10) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _pane_alive(pane: str) -> bool:
    return bool(_media_run(["tmux", "display-message", "-pt", pane,
                            "#{pane_id}"]))


def _last_speaker() -> dict | None:
    """{pane, session, tmux_session} of the most recent speech message whose
    source pane still exists — live now_playing extras first, else a walk
    back through recent history (idle). Panes die and ids get recycled, so
    every candidate is probed before it wins."""
    try:
        import sqlite3

        from agent_media_core.state.store import StateStore
        st = StateStore()
        candidates = [((st.get_now_playing("speech") or {}).get("extras")) or {}]
        db = sqlite3.connect(str(st.path))
        for (raw,) in db.execute(
                "SELECT extras FROM history WHERE sink='speech' AND "
                "extras IS NOT NULL ORDER BY rowid DESC LIMIT 10"):
            try:
                candidates.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        for ex in candidates:
            pane = ex.get("source_pane")
            if pane and _pane_alive(pane):
                return {"pane": pane,
                        "session": (ex.get("source_session")
                                    or ex.get("session") or ""),
                        "tmux_session": ex.get("source_tmux_session") or ""}
        return None
    except Exception:  # noqa: BLE001
        return None


def _send_to_pane(pane: str, text: str) -> str:
    """Type `text` + Enter into a tmux pane (amux's literal-then-Enter timing,
    which Claude Code's input buffering needs). Returns "" or an error."""
    probe = _media_run(["tmux", "display-message", "-pt", pane, "#{pane_id}"])
    if not probe:
        return f"pane {pane} is gone"
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text],
                       timeout=5, check=True)
        time.sleep(0.05)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       timeout=5, check=True)
        return ""
    except (OSError, subprocess.SubprocessError) as e:
        return f"send-keys: {e}"


def send_input(text: str, target: str) -> tuple[bool, str]:
    """Deliver `text` to `target`: "speaker" or "amux:<name>". Returns
    (ok, detail) where detail is the resolved destination or the error."""
    text = (text or "").strip()
    if not text:
        return False, "empty text"
    if target.startswith("amux:"):
        name = target[len("amux:"):]
        if name not in {s["name"] for s in _amux_sessions()}:
            return False, f"unknown amux session {name!r}"
        out = _media_run([_amux_bin(), "send", name, text])
        return (True, f"amux:{name}") if out else (False, "amux send failed")
    speaker = _last_speaker()
    if not speaker:
        return False, "no speaker on record yet"
    err = _send_to_pane(speaker["pane"], text)
    if err:
        return False, err
    return True, speaker.get("tmux_session") or speaker["pane"]


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


def _speech_extras() -> dict:
    """The speech channel's now_playing extras (fresh StateStore per read —
    cheap WAL hit, thread-safe by construction). Empty dict on any problem."""
    try:
        from agent_media_core.state.store import StateStore
        np = StateStore().get_now_playing("speech")
        return (np or {}).get("extras") or {}
    except Exception:  # noqa: BLE001 — subtitles are garnish, never a fault
        return {}


def speech_state() -> dict:
    """One SSE-shaped speech-state snapshot off `media popup-status`, enriched
    with the current sentence (the same per-clip marker that drives the tmux
    copy-mode highlight — this is highlight mode for the canvas) and the
    figure flag for the ▣ badge."""
    line = (_media(["popup-status", "--no-bar", "--show-idle"], timeout=5)
            .splitlines() or [""])[0].strip()
    times = _parse_clock(line)
    state: dict = {"kind": "state", "speaking": line.startswith("▶")}
    if len(times) >= 2:
        state["pos"], state["dur"] = times[0], times[1]
    if state["speaking"]:
        ex = _speech_extras()
        sentence = " ".join(str(ex.get("current_sentence") or "").split())
        if sentence:
            state["sentence"] = sentence[:220]
        if ex.get("visual"):
            state["visual"] = ex["visual"]
        if ex.get("source_session"):
            # Who's talking — the page uses this to dim a figure that belongs
            # to a different session than the current voice.
            state["session"] = str(ex["source_session"])[:80]
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
        key = (st["speaking"], st.get("pos"), st.get("sentence"))
        # Broadcast on any change; while speaking, pos ticks every poll, so
        # watchers get a ~1 Hz progress signal without idle-time chatter.
        if key != last_key:
            HUB.publish(st)
            last_key = key
        time.sleep(1)


# --- video sync: mirror the phone's YouTube audio as muted video --------------
# When the music channel plays on the phone-local backend (a YouTube track
# downloaded to <video-id>.mka and played in the phone's mpv), the canvas can
# show the matching video: the page embeds a muted YouTube IFrame player (the
# browser device sits on the home network, so it streams from YouTube directly —
# red5 itself can't, datacenter IPs get 403) and this poller broadcasts the
# phone's position/pause/speed so the page keeps the video within ~1.5s of the
# audio. Poll only while screens are connected; one batched IPC round-trip.

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Same URL shapes sinks/book.py recognises — a book that fell back to raw-URL
# streaming still identifies its video.
_YT_URL = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})")


def _probe_video(endpoint) -> dict | None:
    """{"vid", "t", "paused", "rate"} for one player, or None when it's idle,
    unreachable, or not on YouTube-identifiable content."""
    from agent_media_core.sinks import _mpv_ipc as ipc
    try:
        props = ipc.get_properties(
            endpoint, ["idle-active", "pause", "time-pos", "speed", "path"],
            timeout=1.5)
    except Exception:  # noqa: BLE001 — down ⇒ no video, never a fault
        return None
    if props.get("idle-active") is not False:
        return None
    path = str(props.get("path") or "")
    stem = Path(path).stem
    # Three shapes carry a video id: the phone music cache (`<id>.mka`), the
    # audiobook library (yt-dlp's `Title [<id>].mka`), and a raw YouTube URL.
    vid = stem if _YT_ID.fullmatch(stem) else None
    if not vid:
        m = re.search(r"\[([A-Za-z0-9_-]{11})\]$", stem)
        vid = m.group(1) if m else None
    if not vid:
        m = _YT_URL.search(path)
        vid = m.group(1) if m else None
    if not vid:
        return None
    return {"vid": vid,
            "t": round(float(props.get("time-pos") or 0.0), 2),
            "paused": bool(props.get("pause")),
            "rate": float(props.get("speed") or 1.0)}


def _selected_channel() -> str:
    """The channel the user last selected (popup Tab / canvas controller both
    persist it via `media popup-channel --set`). The canvas video layer and
    controller follow this, so the two surfaces stay in sync."""
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    try:
        v = (state / "agent-media" / "popup-channel").read_text().strip()
    except OSError:
        return "speech"
    return v if v in ("speech", "music", "book") else "speech"


def _video_state() -> dict:
    """One video-sync snapshot across the channels that can carry a YouTube
    video: the book (red5's sink-book mpv — phone-fetched .mka cache files or
    raw-URL streams) and the music channel's phone mpv.

    The SELECTED channel's player wins when it has a video (even paused — the
    user is looking at that channel); otherwise prefer whichever player is
    actively playing, with a paused one only when nothing else is live. The
    event always carries "chan" so the page's controller follows the popup."""
    sel = _selected_channel()
    probes: dict = {}
    try:
        book_sock = (os.environ.get("MEDIA_BOOK_SOCKET")
                     or str(Path.home()
                            / ".local/state/agent-media/sink-book.sock"))
        if Path(book_sock).exists():
            probes["book"] = _probe_video(book_sock)
        from agent_media_core.sinks import music_local
        if music_local.configured():
            probes["music"] = _probe_video(music_local.endpoint())
    except Exception:  # noqa: BLE001
        pass
    pick = probes.get(sel)
    if pick is None:
        # Not on a video channel (or its player has nothing): only an
        # ACTIVELY PLAYING player may claim the screen. A paused player's
        # frozen video sitting over the speech canvas is noise — it returns
        # when its channel is selected or it resumes.
        candidates = [p for p in (probes.get("book"), probes.get("music")) if p]
        pick = next((c for c in candidates if not c["paused"]), None)
    if pick is None:
        return {"kind": "video", "vid": None, "chan": sel}
    return {"kind": "video", "ts": time.time(), "chan": sel, **pick}


def _video_poller() -> None:
    last_key = None
    while True:
        if HUB.watchers() == 0:
            time.sleep(3)
            continue
        ev = _video_state()
        # While a video is live, publish every poll (the position heartbeat the
        # page drift-corrects against); otherwise only on a change — the hide
        # transition, or a channel switch the controller must follow.
        key = (ev["vid"], ev.get("chan"))
        if ev["vid"] is not None or key != last_key:
            HUB.publish(ev)
        last_key = key
        time.sleep(5 if ev["vid"] else 3)


def ctl_argv(channel: str, action: str, arg: int,
             sarg: str = "") -> list[str] | None:
    """Whitelisted button/key → `media` argv. None = unknown/unsupported combo.
    The maps mirror the popup's handle_key dispatch. `sarg` carries free text
    for the popup's typed-seek (`s`) and open-URL (`o`) keys."""
    if action == "select" and channel in ("speech", "music", "book"):
        # Persist the channel choice — the popup opens on it, and the video
        # poller broadcasts it back so every canvas follows.
        return ["popup-channel", "--set", channel]
    if channel == "speech":
        table = {
            "toggle": ["toggle"],
            "prev": ["replay-prev", "--idx", str(arg)],
            "replay": ["replay", str(arg)],
            "jump-end": ["jump", "end"],
            "vol-": ["volume", "-5"],
            "vol+": ["volume", "5"],
            "mute": ["mute"],
            # M — durable "keep muted" of the popup subject (distinct from m).
            "mute-keep": ["mute-pane", "--subject", "toggle"],
            # v — toggle the copy-mode auto-highlight follow-along.
            "highlight": ["highlight-toggle"],
            # p — play the clip at the caller pane's copy-mode cursor.
            "clip-cursor": ["replay-at-cursor"],
            # g — focus the speaking pane in tmux.
            "goto": ["goto-pane"],
            "speed-": ["speed", "down"],
            "speed+": ["speed", "up"],
            "speed0": ["speed", "reset"],
            # h/l/H/L — the popup's sentence/paragraph steps.
            "skip-": ["skip", "--unit", "sentence", "--dir", "-1",
                      "--seek-fallback", "-5"],
            "skip+": ["skip", "--unit", "sentence", "--dir", "1",
                      "--seek-fallback", "5"],
            "para-": ["skip", "--unit", "paragraph", "--dir", "-1",
                      "--seek-fallback", "-30"],
            "para+": ["skip", "--unit", "paragraph", "--dir", "1",
                      "--seek-fallback", "30"],
        }
        return table.get(action)
    if channel in ("music", "book"):
        table = {
            "prev": [channel, "prev", "--restart-first"],
            "next": [channel, "next"],
            "vol-": [channel, "volume", "-5"],
            "vol+": [channel, "volume", "5"],
            "skip-": [channel, "seek", "-5"],
            "skip+": [channel, "seek", "+5"],
            "para-": [channel, "seek", "-30"],
            "para+": [channel, "seek", "+30"],
            # g — focus the channel's pane/UI (ncmpcpp / mpvc).
            "goto": ["goto-track" if channel == "music" else "goto-book"],
            # w — print the channel's web-UI URL (the browser opens it).
            "web": ["music-web" if channel == "music" else "book-web"],
        }
        # s / o — typed seek and open-URL both carry a free-text arg.
        if action == "seek-to" and sarg:
            return [channel, "seek", "--", sarg]
        if action == "open-url" and sarg:
            return [channel, "play", sarg]
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
  /* Fit-within-screen: the whole image visible, letterboxed on black.
     Figures get this by default (their edge labels are content — cover
     crops them on small screens); the fit button forces it for everything. */
  .layer.fit { object-fit: contain; background: #000; }
  /* A figure whose session ISN'T the one speaking dims to backdrop — it's
     still there, but no longer claims to illustrate the current voice. */
  .layer.stale { filter: brightness(.3) saturate(.5); }
  /* ---- e-ink mode (?eink=1, persisted per device; 'e' toggles) ------------
     Ink on paper for the Pine Note: white page, NO crossfades / pan-zoom /
     breathing (partial-refresh ghosting), no video layer. Dark SVG figures
     invert to black-line-on-white; raster art goes grayscale (inversion
     would make photos negatives). All client-side — one generation serves
     the moody wall and the e-ink page alike. */
  html.eink, html.eink body { background: #fff; }
  html.eink .layer { transition: none; animation: none !important; }
  /* Tuned for DU4 (4-grey fast waveform, David's usual): only two steps
     exist between black and white, so push figures toward pure B/W and
     boost raster contrast — GC16 just renders the same look bolder. */
  html.eink .layer.inkable {
    filter: invert(1) hue-rotate(180deg) grayscale(1) contrast(1.7);
  }
  html.eink .layer:not(.inkable) { filter: grayscale(1) contrast(1.35); }
  html.eink .layer.fit { background: #fff; }
  html.eink .layer.stale.on { opacity: .2; filter: grayscale(1); }
  html.eink #pulse, html.eink #ytwrap { display: none !important; }
  html.eink #title.scroll { animation: none !important; }
  html.eink #cap, html.eink #sub {
    color: #111; background: rgba(255,255,255,.92);
    backdrop-filter: none; text-shadow: none;
  }
  html.eink #sub { color: #000; font-weight: 600; }
  html.eink #fig { color: #000; background: rgba(255,255,255,.92); }
  html.eink #ctl, html.eink #inp {
    background: rgba(255,255,255,.94); backdrop-filter: none;
    color: #111; border: 1.5px solid #000;
  }
  html.eink #ctl button, html.eink #send, html.eink #text { color: #111; }
  html.eink #ctl button.lit { color: #000; background: rgba(0,0,0,.12); }
  html.eink #clock { color: #111; }
  html.eink #target { background: rgba(0,0,0,.08); color: #000; }
  /* Ken Burns variants — picked at random per image */
  @keyframes kb1 { from { transform: scale(1.06) translate(0,0); }
                   to   { transform: scale(1.18) translate(-2%,-1.5%); } }
  @keyframes kb2 { from { transform: scale(1.18) translate(1.5%,1%); }
                   to   { transform: scale(1.06) translate(0,0); } }
  @keyframes kb3 { from { transform: scale(1.06) translate(-1.5%,1%); }
                   to   { transform: scale(1.16) translate(1.5%,-1%); } }
  @keyframes kb4 { from { transform: scale(1.16) translate(0,-1.5%); }
                   to   { transform: scale(1.08) translate(-1%,1.5%); } }
  /* Video layer: the muted YouTube mirror of what the phone is playing.
     Sits above the ambient image layers, below every overlay (#pulse onward
     in DOM order). pointer-events off — taps keep driving the canvas UI. */
  #ytwrap {
    position: fixed; inset: 0; background: #000;
    opacity: 0; transition: opacity 1.4s ease; pointer-events: none;
  }
  #ytwrap.on { opacity: 1; }
  #ytwrap iframe { width: 100vw; height: 100vh; border: 0; }
  #cap {
    position: fixed; left: 50%;
    bottom: calc(max(2vh, env(safe-area-inset-bottom)) + 68px);  /* above #inp */
    transform: translateX(-50%); max-width: 82vw;
    padding: .55em 1.1em; border-radius: 999px;
    font: 15px/1.45 system-ui, sans-serif; color: #eee; text-align: center;
    background: rgba(10,10,10,.55); backdrop-filter: blur(12px);
    opacity: 0; transition: opacity 1s ease; pointer-events: none;
  }
  #cap.on { opacity: 1; }
  #cap.hide { opacity: 0; }
  #dot {
    position: fixed; bottom: 12px; right: 12px; width: 8px; height: 8px;
    border-radius: 50%; background: #e05c5c; opacity: 0; transition: opacity .5s;
  }
  #dot.off { opacity: .8; }
  /* Subtitles: the sentence being spoken right now — highlight mode for a
     screen across the room. */
  #sub {
    position: fixed; left: 50%; bottom: max(4vh, env(safe-area-inset-bottom));
    transform: translateX(-50%); max-width: 86vw;
    padding: .5em 1.1em; border-radius: 14px;
    font: 500 17px/1.5 system-ui, sans-serif; color: #ffe9a8;
    text-align: center; text-wrap: balance;
    background: rgba(8,8,8,.68); backdrop-filter: blur(12px);
    opacity: 0; transition: opacity .5s ease; pointer-events: none;
  }
  #sub.on { opacity: 1; }
  /* Figure badge: this image says something — it isn't wallpaper. */
  #fig {
    position: fixed; top: max(12px, env(safe-area-inset-top)); left: 16px;
    padding: .3em .8em; border-radius: 999px;
    font: 600 13px/1.4 system-ui, sans-serif; letter-spacing: .04em;
    color: #ffd75f; background: rgba(10,10,10,.6); backdrop-filter: blur(10px);
    opacity: 0; transition: opacity .8s ease; pointer-events: none;
  }
  #fig.on { opacity: 1; }
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
  /* --- audio controller (tap anywhere to reveal) — kin of the tmux popup ---
     Geometry mirrors the tmux binding (`display-popup -w 34 -h 4 -x R -y 6`):
     a compact panel anchored to the RIGHT edge near the top, sliding in from
     the right, instead of a wide bottom sheet. */
  /* Bottom dock: the controller shares the input's slot (they're mutually
     exclusive — CONTROL means hotkeys, not typing), sliding up from below. */
  #ctl {
    position: fixed; left: 50%; bottom: max(2vh, env(safe-area-inset-bottom));
    transform: translateX(-50%) translateY(calc(100% + 24px));
    width: min(96vw, 620px); box-sizing: border-box;
    padding: .45em .6em; border-radius: 18px;
    background: rgba(10,10,10,.72); backdrop-filter: blur(14px);
    color: #eee; font: 14px/1.4 system-ui, sans-serif;
    transition: transform .3s ease, opacity .3s ease; opacity: 0;
  }
  #ctl.on { transform: translateX(-50%) translateY(0); opacity: 1; }
  /* CONTROL mode: hotkeys are live — amber ring (matches the input's). */
  #ctl.focused { box-shadow: 0 0 0 2px rgba(255,215,95,.75); }
  #ctl .row { display: flex; align-items: center; gap: .15em; }
  #ctl button {
    background: none; border: 0; color: #eee; font-size: 19px;
    min-width: 36px; min-height: 38px; border-radius: 10px;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
    flex: 0 0 auto;
  }
  /* Narrow screens: tighter buttons so the clock keeps its digits. */
  @media (max-width: 430px) {
    #ctl button { min-width: 31px; min-height: 34px; font-size: 17px; }
    #clock { font-size: 13px; }
  }
  #ctl button:active { background: rgba(255,255,255,.14); }
  #ctl button.lit { color: #ffd75f; }
  .ic { width: 19px; height: 19px; display: block; margin: auto; }
  #target .ic { width: 16px; height: 16px; display: inline-block;
                vertical-align: -3px; margin-right: .35em; }
  #send .ic { margin: auto; }
  #marq { flex: 1; overflow: hidden; white-space: nowrap; }
  #title { display: inline-block; padding-left: 0; }
  #title.scroll { animation: marq var(--marq-dur, 14s) linear infinite; }
  @keyframes marq {
    0%, 12%  { transform: translateX(0); }
    88%, 100% { transform: translateX(var(--marq-shift, -40%)); }
  }
  #clock { flex: 1; min-width: 78px; text-align: center;
           font-variant-numeric: tabular-nums;
           color: #ddd; white-space: nowrap; overflow: hidden;
           border-radius: 8px; padding: .25em .2em; }
  /* Input bar: reply to whoever just spoke, from the canvas itself. Persistent
     along the bottom (the PASSIVE resting surface); focus ring + full opacity
     in INPUT mode. Tapping it (or first Tab) focuses it. */
  #inp {
    position: fixed; left: 50%; transform: translateX(-50%);
    bottom: max(2vh, env(safe-area-inset-bottom));
    width: min(96vw, 620px); box-sizing: border-box;
    display: flex; align-items: center; gap: .4em;
    padding: .5em .6em; border-radius: 18px;
    background: rgba(10,10,10,.5); backdrop-filter: blur(14px);
    box-shadow: 0 0 0 1px rgba(255,255,255,.08);
    transition: opacity .25s ease, background .2s ease, box-shadow .2s ease;
    opacity: .5;                               /* dim while resting (PASSIVE) */
  }
  #inp.on { opacity: 1; background: rgba(10,10,10,.74);
            box-shadow: 0 0 0 2px rgba(255,215,95,.75); }  /* INPUT focus ring */
  #inp.under { opacity: 0; pointer-events: none; }  /* CONTROL: controller takes the dock */
  #target {
    background: rgba(255,255,255,.1); border: 0; color: #ffd75f;
    font: 600 13px/1.4 system-ui, sans-serif; padding: .45em .8em;
    border-radius: 999px; white-space: nowrap; cursor: pointer;
  }
  #text {
    flex: 1; background: none; border: 0; outline: none; color: #eee;
    font: 16px/1.4 system-ui, sans-serif; min-width: 0;
  }
  #send {
    background: none; border: 0; color: #eee; font-size: 20px;
    min-width: 40px; min-height: 40px; cursor: pointer; border-radius: 12px;
  }
  #send:active { background: rgba(255,255,255,.14); }
  /* Keymap help overlay (the popup's `?`). Centered card, tap/`?`/Esc dismiss. */
  #help {
    position: fixed; inset: 0; margin: auto; width: min(92vw, 460px);
    max-height: 82vh; height: max-content; overflow: auto;
    padding: 1em 1.2em; border-radius: 16px; z-index: 30;
    background: rgba(12,12,14,.93); backdrop-filter: blur(16px); color: #eee;
    font: 14px/1.5 system-ui, sans-serif; box-shadow: 0 10px 40px rgba(0,0,0,.5);
    opacity: 0; pointer-events: none; transition: opacity .2s ease;
  }
  #help.on { opacity: 1; pointer-events: auto; }
  #help .hh { font-weight: 600; margin-bottom: .6em; color: #ffd75f; }
  #help .hg { display: grid; grid-template-columns: auto 1fr; gap: .25em .9em; }
  #help .hg b { color: #ffd75f; font-weight: 600; white-space: nowrap; }
  /* Transient toast (top-center): brief status like "web UI not available". */
  #toast {
    position: fixed; left: 50%; transform: translateX(-50%);
    top: max(16px, env(safe-area-inset-top)); max-width: 86vw; z-index: 40;
    padding: .5em 1em; border-radius: 999px;
    background: rgba(20,20,22,.92); backdrop-filter: blur(12px); color: #eee;
    font: 14px/1.4 system-ui, sans-serif; box-shadow: 0 6px 24px rgba(0,0,0,.4);
    word-break: break-word; opacity: 0; pointer-events: none;
    transition: opacity .25s ease;
  }
  #toast.on { opacity: 1; }
</style>
</head>
<body>
<!-- Unified icon set: one monochrome inline-SVG sprite (currentColor, uniform
     2px stroke) instead of the emoji/text-glyph mix — emoji render as colored
     pictographs with per-device fonts, exactly what the tmux popup's
     PAUSE_GLYPH comment warns about. -->
<svg style="display:none" xmlns="http://www.w3.org/2000/svg">
  <symbol id="i-play" viewBox="0 0 24 24"><path fill="currentColor" d="M8 5l11 7-11 7z"/></symbol>
  <symbol id="i-pause" viewBox="0 0 24 24"><path fill="currentColor" d="M7 5h3v14H7zM14 5h3v14h-3z"/></symbol>
  <symbol id="i-prev" viewBox="0 0 24 24"><path fill="currentColor" d="M7 5h2v14H7zM19 5l-9 7 9 7z"/></symbol>
  <symbol id="i-next" viewBox="0 0 24 24"><path fill="currentColor" d="M15 5h2v14h-2zM5 5l9 7-9 7z"/></symbol>
  <symbol id="i-minus" viewBox="0 0 24 24"><path stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="M6 12h12"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="M6 12h12M12 6v12"/></symbol>
  <symbol id="i-slower" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M7 8l-4 4 4 4M21 12H4"/></symbol>
  <symbol id="i-faster" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M17 8l4 4-4 4M3 12h17"/></symbol>
  <symbol id="i-mute" viewBox="0 0 24 24"><path fill="currentColor" d="M4 9v6h4l5 4V5L8 9z"/><path stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M16 9l5 6M21 9l-5 6"/></symbol>
  <symbol id="i-bell" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M6 16v-5a6 6 0 0112 0v5l2 2H4z"/><path stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M10 21h4"/></symbol>
  <symbol id="i-bell-off" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M6 16v-5a6 6 0 0112 0v5l2 2H4z"/><path stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M10 21h4M4 4l16 16"/></symbol>
  <symbol id="i-cc" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><path stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M7 12h4M7 15.5h7"/></symbol>
  <symbol id="i-fit" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/></symbol>
  <symbol id="i-skipb" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M11 7l-5 5 5 5M18 7l-5 5 5 5"/></symbol>
  <symbol id="i-skipf" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M13 7l5 5-5 5M6 7l5 5-5 5"/></symbol>
  <symbol id="i-kbd" viewBox="0 0 24 24"><rect x="2.5" y="6" width="19" height="12" rx="2.5" fill="none" stroke="currentColor" stroke-width="2"/><path stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M6.5 10h.01M10.5 10h.01M14.5 10h.01M18 10h.01M7.5 14.5h9"/></symbol>
  <symbol id="i-close" viewBox="0 0 24 24"><path stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="M6 6l12 12M18 6L6 18"/></symbol>
  <symbol id="i-send" viewBox="0 0 24 24"><path fill="currentColor" d="M3 11l18-8-7 18-3-7z"/></symbol>
  <symbol id="i-note" viewBox="0 0 24 24"><path fill="currentColor" d="M9 18.5A2.5 2.5 0 116.5 16c.6 0 1.1.2 1.5.5V5l10-2v12.5a2.5 2.5 0 11-2.5-2.5c.6 0 1.1.2 1.5.5V7L9 8.6z"/></symbol>
  <symbol id="i-notes" viewBox="0 0 24 24"><path fill="currentColor" d="M7 19a2 2 0 110-4c.4 0 .7.1 1 .2V6l9-1.8V15a2 2 0 11-2-2c.4 0 .7.1 1 .2V7.5L9 8.9V19z"/><path stroke="currentColor" stroke-width="1.6" stroke-linecap="round" d="M4 4.5l1.5 1"/></symbol>
  <symbol id="i-book" viewBox="0 0 24 24"><path stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="M4 7h16M4 12h16M4 17h10"/></symbol>
  <symbol id="i-reply" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M9 6L4 11l5 5M4 11h11a5 5 0 015 5v2"/></symbol>
</svg>
<img id="a" class="layer" alt="">
<img id="b" class="layer" alt="">
<div id="ytwrap"><div id="yt"></div></div>
<div id="pulse"></div>
<div id="cap"></div>
<div id="sub"></div>
<div id="fig">▣ figure</div>
<div id="dot" title="disconnected"></div>
<div id="toast"></div>
<div id="inp">
  <button id="target"></button>
  <input id="text" type="text" autocomplete="off" enterkeyhint="send"
         placeholder="reply…">
  <button id="send"></button>
</div>
<div id="ctl">
  <div class="row">
    <button id="chan"></button>
    <div id="marq"><span id="title">agent-media</span></div>
    <button id="kbd"></button>
    <button id="cc"></button>
    <button id="fit"></button>
    <button id="sfx"></button>
    <button id="xbtn"></button>
  </div>
  <!-- Transport row: turn-prev · sentence-back · play/pause · clock ·
       sentence-fwd · turn-next. The volume/speed/mute cluster gets its own
       row below — nine buttons plus the clock overflowed a phone-width panel
       and clipped the time display. -->
  <div class="row">
    <button id="prev"></button>
    <button id="skb"></button>
    <button id="pp"></button>
    <span id="clock">○</span>
    <button id="skf"></button>
    <button id="next"></button>
  </div>
  <div class="row">
    <button id="vdn"></button>
    <button id="vup"></button>
    <button id="sdn" class="sp"></button>
    <button id="sup" class="sp"></button>
    <button id="mute" class="sp"></button>
  </div>
</div>
<div id="help">
  <div class="hh">canvas keys · Tab: passive→input→control · Esc / q → passive</div>
  <div class="hg">
    <b>Space</b><span>play / pause</span>
    <b>h · l</b><span>sentence −/+ (music/book: seek ∓5s)</span>
    <b>H · L</b><span>paragraph −/+ (music/book: seek ∓30s)</span>
    <b>&lt; · &gt;</b><span>prev / next</span>
    <b>− · =</b><span>volume −/+</span>
    <b>[ · ]</b><span>speed −/+  ·  0 / ⌫ reset</span>
    <b>r · p</b><span>replay last · clip at cursor  (speech)</span>
    <b>m · M</b><span>mute · keep-muted 🔒  (speech)</span>
    <b>v</b><span>highlight follow-along  (speech)</span>
    <b>g</b><span>go to source pane</span>
    <b>s · o</b><span>typed seek · open URL  (music/book)</span>
    <b>w</b><span>web UI  (music/book)</span>
    <b>Tab</b><span>cycle channel  (in control)</span>
    <b>c · f</b><span>captions · sound fx</span>
    <b>Enter</b><span>reply input</span>
    <b>?</b><span>this help</span>
  </div>
</div>
<script>
  const $ = (id) => document.getElementById(id);
  const icon = (n) => '<svg class="ic"><use href="#i-' + n + '"/></svg>';
  // ---- e-ink mode: ?eink=1 arms it for this device, ?eink=0 (or 'e') back --
  const qs = new URLSearchParams(location.search);
  if (qs.has('eink')) {
    localStorage.setItem('eink', qs.get('eink') === '0' ? '0' : '1');
    history.replaceState(null, '', location.pathname);
  }
  function einkOn() { return localStorage.getItem('eink') === '1'; }
  if (einkOn()) document.documentElement.classList.add('eink');
  const layers = [$('a'), $('b')];
  // Static icons; stateful ones (pp, sfx, chan, target) are set in their
  // draw functions below.
  $('prev').innerHTML = icon('prev');   $('next').innerHTML = icon('next');
  $('skb').innerHTML = icon('skipb');   $('skf').innerHTML = icon('skipf');
  $('vdn').innerHTML = icon('minus');   $('vup').innerHTML = icon('plus');
  $('sdn').innerHTML = icon('slower');  $('sup').innerHTML = icon('faster');
  $('mute').innerHTML = icon('mute');   $('kbd').innerHTML = icon('kbd');
  $('cc').innerHTML = icon('cc');       $('xbtn').innerHTML = icon('close');
  $('fit').innerHTML = icon('fit');
  $('send').innerHTML = icon('send');   $('chan').innerHTML = icon('note');
  $('pp').innerHTML = icon('play');
  let front = 0, capTimer = null;
  const KB = ['kb1','kb2','kb3','kb4'];

  // ---- fit setting: auto (figures fit, art fills) · fit · fill -------------
  // cover + the Ken Burns zoom crops edges — fatal for a figure's labels on a
  // small screen. Fitted images letterbox (object-fit: contain) and skip the
  // pan/zoom (which would push the letterboxed image off-frame again).
  function fitMode() { return localStorage.getItem('fit') || 'auto'; }
  function wantFit(purpose) {
    const m = fitMode();
    return m === 'fit' || (m === 'auto' && purpose === 'figure');
  }
  let lastPurpose = null;
  function kenBurns(el) {
    if (einkOn()) return;            // motion is ghosting on e-ink
    const dur = 28 + Math.random() * 14;
    el.style.animation = KB[Math.floor(Math.random()*KB.length)] +
      ' ' + dur.toFixed(1) + 's ease-in-out infinite alternate';
    if (speaking)
      for (const a of el.getAnimations())
        (a.updatePlaybackRate ? a.updatePlaybackRate(2.6) : a.playbackRate = 2.6);
  }
  function applyFit(el, fit) {
    el.classList.toggle('fit', fit);
    if (fit) el.style.animation = 'none';
  }

  function show(d) {
    const back = 1 - front;
    const el = layers[back];
    lastPurpose = d.purpose || null;
    const fit = wantFit(d.purpose);
    el.onload = () => {
      applyFit(el, fit);
      el.classList.remove('stale');   // a fresh image is never pre-dimmed
      // Ink-invertible? SVG figures are dark-bg line art — invert() turns
      // them into black-on-white; raster stays grayscale (see .eink CSS).
      el.classList.toggle('inkable', /\\.svg(\\?|$)/i.test(d.image || ''));
      if (!fit) kenBurns(el);
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
  // down when it stops. Quiet by design; the bell button toggles, state
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
  // A figure deserves its own arrival sound: a bright three-note rise that
  // says "look at the screen", distinct from the ambient whoosh.
  function figureCue() {
    if (!sfxOn()) return;
    try {
      const c = actx();
      [523, 659, 784].forEach((f, i) => {
        const t = c.currentTime + i * 0.13;
        const o = c.createOscillator(), g = c.createGain();
        o.type = 'triangle'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.055, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.5);
        o.connect(g).connect(c.destination);
        o.start(t); o.stop(t + 0.55);
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
  // Figure badge has two feeders: the showing image's purpose, and the
  // speaking message's [[visual:]] flag (so it lights before the image lands).
  let figImg = false, figMsg = false;
  function updFig() { $('fig').classList.toggle('on', figImg || figMsg); }
  // Cross-session honesty: remember which session's reply the shown visual
  // belongs to; while a DIFFERENT session speaks, a figure dims to backdrop
  // and drops its badge (it doesn't illustrate that voice). null session on
  // either side = unknown → leave it alone.
  let shownFigure = false, shownSession = null;
  function applyStale(speakSess) {
    const stale = !!(shownFigure && shownSession && speakSess &&
                     speakSess !== shownSession);
    layers[front].classList.toggle('stale', stale);
    figImg = shownFigure && !stale;
    updFig();
  }
  // Subtitles: the sentence being spoken, straight off the same per-clip
  // marker that drives the tmux copy-mode highlight.
  function subsOn() { return localStorage.getItem('subs') !== '0'; }
  function setSubtitle(text) {
    const show = !!(text && subsOn());
    if (show) $('sub').textContent = text;
    $('sub').classList.toggle('on', show);
    if (show) $('cap').classList.add('hide');
    else if (!visible) $('cap').classList.remove('hide');
  }
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
    vidVisible();                              // video yields while speaking
    if (!on) { setSubtitle(null); figMsg = false; updFig(); }
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

  // ---- video sync: muted YouTube mirror of the phone's music ---------------
  // The server streams {"kind":"video", vid, t, paused, rate} while the phone
  // plays a YouTube-cached track. The page keeps a muted IFrame player within
  // ~1.5s of the audio (seek on drift), and yields the screen to figures for a
  // minute whenever one arrives — a figure is content, the video is ambience.
  let ytP = null, ytReady = false, ytVid = null, ytApiAsked = false;
  let pendingV = null, figHold = 0;
  function ytEnsureApi() {
    if (ytApiAsked) return; ytApiAsked = true;
    const s = document.createElement('script');
    s.src = 'https://www.youtube.com/iframe_api';
    document.head.appendChild(s);
  }
  window.onYouTubeIframeAPIReady = () => {
    ytP = new YT.Player('yt', {
      width: '100%', height: '100%',
      playerVars: { autoplay: 1, controls: 0, disablekb: 1, fs: 0, rel: 0,
                    iv_load_policy: 3, playsinline: 1 },
      events: {
        onReady: () => { ytReady = true; ytP.mute();
                         if (pendingV) { const v = pendingV; pendingV = null; syncVideo(v); } },
        // Embed-blocked / removed video → fall back to the ambient artwork.
        onError: () => { ytVid = null; vidVisible(); },
      },
    });
  };
  function vidVisible() {
    // Speech owns the canvas while it's talking (subtitles, artwork,
    // figures) — the video yields and returns when the voice stops.
    // e-ink never shows video (CSS hides the layer; don't even sync it).
    document.getElementById('ytwrap').classList
      .toggle('on', !!ytVid && !speaking && !einkOn() && Date.now() > figHold);
  }
  setInterval(vidVisible, 5000);        // restores the video after a fig hold
  function syncVideo(d) {
    if (einkOn()) return;            // no video on e-ink — don't even load the API
    if (!d.vid) {
      ytVid = null; vidVisible();
      if (ytReady) try { ytP.stopVideo(); } catch (_) {}
      return;
    }
    ytEnsureApi();
    if (!ytReady) { pendingV = d; return; }
    const now = d.t + (Date.now() - d.rx) / 1000;   // rx stamped on arrival
    try {
      if (d.vid !== ytVid) {
        ytVid = d.vid;
        ytP.loadVideoById({ videoId: d.vid, startSeconds: now });
        ytP.mute();
      } else if (!d.paused && Math.abs(ytP.getCurrentTime() - now) > 1.5) {
        ytP.seekTo(now, true);
      }
      if (d.paused) { if (ytP.getPlayerState() === 1) ytP.pauseVideo(); }
      else if (ytP.getPlayerState() !== 1) ytP.playVideo();
      if (d.rate && ytP.setPlaybackRate)
        ytP.setPlaybackRate(Math.max(0.25, Math.min(2, d.rate)));
    } catch (_) {}
    vidVisible();
  }

  const es = new EventSource('/events');
  es.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.kind === 'video') {
        d.rx = Date.now(); syncVideo(d);
        // Follow the selected channel (popup Tab / another canvas) — unless
        // the user just tapped the channel button here (their choice is on
        // its way to the server; adopting a stale event would flip it back).
        if (d.chan && d.chan !== ch && Date.now() - chTouched > 8000) {
          ch = d.chan; histIdx = 1;
          if (visible) { $('title').textContent = '…'; poll(); }
        }
      }
      else if (d.kind === 'state') {
        setSpeaking(!!d.speaking);
        if (d.speaking) {
          setSubtitle(d.sentence || null);
          figMsg = !!d.visual; updFig();
          applyStale(d.session || null);
        } else {
          applyStale(null);            // no voice → nothing is misattributed
        }
        applySeq();
      }
      else if (d.sequence) {
        seq = d.sequence; seqIdx = -1; seqEst = d.estdur || 0;
        seqCap = d.caption || null;
        shownFigure = false; shownSession = d.session || null;
        figImg = false; updFig();
        figHold = Date.now() + 60000; vidVisible();   // beats own the screen
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
      else if (d.image) {
        seq = null; seqIdx = -1; show(d);
        shownFigure = d.purpose === 'figure'; shownSession = d.session || null;
        figImg = shownFigure; updFig();
        if (figImg) { figHold = Date.now() + 60000; vidVisible(); }
        figImg ? figureCue() : whoosh();
      }
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
  const GLYPH = { speech: 'note', music: 'notes', book: 'book' };
  const ORDER = ['speech', 'music', 'book'];
  let ch = 'speech', histIdx = 1, visible = false;
  let hideTimer = null, pollTimer = null;
  let chTouched = 0;   // last local channel tap — wins over server sync briefly

  function speechOnly(showIt) {
    for (const el of document.querySelectorAll('#ctl .sp'))
      el.style.display = showIt ? '' : 'none';
  }

  function render(d) {
    $('chan').innerHTML = icon(GLYPH[ch]);
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
    $('pp').innerHTML = icon(playing ? 'pause' : 'play');
    // "00:12 / 02:05" → "00:12/02:05": two columns saved, same trick as the
    // tmux popup — the difference between fitting and clipping on a phone.
    const clock = (d.status.replace(/^[▶⏸○]\\s*/, '') || '○').replace(' / ', '/');
    $('clock').textContent = clock;
    // Background-fill progress: the clock doubles as the bar (no extra row).
    const secs = (s) => s.split(':').reduce((a, v) => a * 60 + (+v || 0), 0);
    const m = clock.match(/^([\\d:]+)\\/([\\d:]+)$/);
    const frac = m && secs(m[2]) > 0
      ? Math.max(0, Math.min(1, secs(m[1]) / secs(m[2]))) : null;
    // e-ink fill dark enough to survive DU4's 4-level quantization (a 16%
    // grey rounds to white there); the track stays as a hairline via border.
    const fill = einkOn() ? 'rgba(0,0,0,.32)' : 'rgba(255,215,95,.28)';
    const track = einkOn() ? 'rgba(0,0,0,.08)' : 'rgba(255,255,255,.07)';
    $('clock').style.background = frac === null ? 'none'
      : 'linear-gradient(90deg, ' + fill + ' ' + (frac * 100).toFixed(1)
        + '%, ' + track + ' ' + (frac * 100).toFixed(1) + '%)';
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

  // Three focus states, mirroring the tmux-popup model:
  //   passive — just the image; the bottom input rests dim, hotkeys OFF.
  //   input   — bottom field focused; type a reply (Enter sends, Esc → passive).
  //   control — controller focused; single-key hotkeys live, Tab cycles channel.
  // Tab walks passive→input→control; Esc / q drop back to passive.
  let mode = 'passive';

  function resetHide() {                        // idle CONTROL auto-returns to passive
    clearTimeout(hideTimer);
    if (mode === 'control')
      hideTimer = setTimeout(() => setMode('passive'), 15000);
  }

  function setMode(m) {
    mode = m;
    const ctrl = (m === 'control');
    const active = (m === 'control' || m === 'input');   // dock is engaged
    visible = ctrl;                          // controller (polled) only in CONTROL
    $('inp').classList.toggle('on', m === 'input');       // focus ring
    $('inp').classList.toggle('under', ctrl);             // hidden beneath controller
    $('ctl').classList.toggle('on', ctrl);
    $('ctl').classList.toggle('focused', ctrl);
    $('cap').classList.toggle('hide', active);
    if (m === 'input') $('text').focus();
    else if (document.activeElement === $('text')) $('text').blur();
    clearInterval(pollTimer);
    if (ctrl) { poll(); pollTimer = setInterval(poll, 2000); }
    if (!active && !$('sub').classList.contains('on'))
      $('cap').classList.remove('hide');
    resetHide();
  }

  async function act(action, arg, sarg) {
    resetHide();
    let r = null;
    try {
      r = await fetch('/ctl', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel: ch, action: action, arg: arg, sarg: sarg }),
      }).then(r => r.json());
      // Speech ⏮ semantics ride on replay-prev's echoed cursor (the popup
      // folds the same echo into hist_idx).
      if (action === 'prev' && ch === 'speech' && r.out && /^\\d+$/.test(r.out))
        histIdx = parseInt(r.out, 10);
    } catch (_) {}
    setTimeout(poll, 300);                     // let the action land, then refresh
    return r;
  }

  // Transient top-center status message (~2.6s).
  let toastT = null;
  function toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.add('on');
    clearTimeout(toastT);
    toastT = setTimeout(() => t.classList.remove('on'), 2600);
  }
  // Popup `w` — open the active channel's web UI (music → Iris, book → mpvc).
  // No UI configured/installed (empty result) → a toast instead of a dead tab;
  // a blocked popup → surface the URL so it's still reachable.
  async function openWeb() {
    if (ch === 'speech') { toast('web UI — music / book only'); return; }
    const r = await act('web');
    const url = r && r.out && r.out.trim();
    if (!url || url.slice(0, 4) !== 'http') {
      toast(ch + ' web UI not available');
      return;
    }
    // A loopback URL is the media host's own localhost — not reachable from a
    // remote canvas (phone/wall). Surface the address rather than a dead tab.
    if (url.indexOf('//127.0.0.1') >= 0 || url.indexOf('//localhost') >= 0) {
      toast('web UI (on media host): ' + url);
      return;
    }
    if (!window.open(url, '_blank')) toast(url);   // popup blocked → show it
  }
  // Popup `s` / `o` — typed seek and open-URL (music/book only; speech uses h/l).
  function typedSeek() {
    if (ch === 'speech') { toast('typed seek — music / book only'); return; }
    const t = prompt(ch + ' seek — H:MM:SS · +90 · -5:00');
    if (t && t.trim()) act('seek-to', 1, t.trim());
  }
  function typedOpen() {
    if (ch === 'speech') { toast('open URL — music / book only'); return; }
    const u = prompt(ch + ' — paste a URL to play');
    if (u && u.trim()) act('open-url', 1, u.trim());
  }
  function toggleHelp() { $('help').classList.toggle('on'); }

  // ---- input box: reply to whoever just spoke (token-authed) ---------------
  let targets = ['speaker'], tIdx = 0;
  function token() { return localStorage.getItem('amux_token') || ''; }
  function askToken() {
    const t = prompt('amux auth token (from ~/.amux/auth_token):');
    if (t) localStorage.setItem('amux_token', t.trim());
    return !!t;
  }
  async function authed(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({'X-Auth-Token': token()}, opts.headers);
    let r = await fetch(url, opts);
    if (r.status === 401 && askToken()) {
      opts.headers['X-Auth-Token'] = token();
      r = await fetch(url, opts);
    }
    return r;
  }
  function drawTarget() {
    const t = targets[tIdx];
    $('target').innerHTML = t === 'speaker'
      ? icon('reply') + 'speaker'
      : icon('book') + t.slice(5);
  }
  drawTarget();
  async function openInput() {
    setMode('input');
    try {
      const d = await authed('/sessions').then(r => r.json());
      targets = ['speaker'].concat((d.amux || []).map(n => 'amux:' + n));
      tIdx = 0; drawTarget();
    } catch (_) {}
  }
  function closeInput() { setMode('passive'); }
  $('kbd').onclick = (e) => { e.stopPropagation(); openInput(); };
  $('target').onclick = (e) => {
    e.stopPropagation();
    tIdx = (tIdx + 1) % targets.length;
    drawTarget();
  };
  async function sendText() {
    const text = $('text').value.trim();
    if (!text) return;
    $('send').textContent = '…';
    try {
      const r = await authed('/input', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text, target: targets[tIdx]}),
      }).then(r => r.json());
      if (r.ok) { $('text').value = ''; $('send').textContent = '✓'; }
      else { $('send').textContent = '✕'; console.warn(r.detail); }
    } catch (_) { $('send').textContent = '✕'; }
    setTimeout(() => { $('send').innerHTML = icon('send'); }, 1200);
  }
  $('send').onclick = (e) => { e.stopPropagation(); sendText(); };
  $('text').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendText(); }
    else if (e.key === 'Escape') { e.preventDefault(); setMode('passive'); }
    else if (e.key === 'Tab') { e.preventDefault(); setMode('control'); }  // input → control
  });

  document.body.addEventListener('click', (e) => {
    wake();
    if ($('help').classList.contains('on')) { toggleHelp(); return; }
    if ($('ctl').contains(e.target)) { resetHide(); return; }  // buttons self-handle
    if ($('inp').contains(e.target)) { openInput(); return; }  // tap field → INPUT
    // Tap on the bare canvas: reveal / dismiss the controller (passive ⇄ control).
    setMode(mode === 'control' ? 'passive' : 'control');
  });

  // ---- popup-parity key bindings (for canvases with a keyboard) ------------
  // Focus walks with Tab (passive→input→control) and unwinds with Esc/q. In
  // CONTROL the full tmux-popup (prefix a) hotkey set is live: Tab channel ·
  // Space play/pause · h/l sentence · H/L paragraph · </> prev/next · -/= vol ·
  // m mute · M keep-muted · v highlight · p clip@cursor · g source · w web UI ·
  // s typed-seek · o open-URL · [/] speed, 0/⌫ reset · r replay · c cc · f sfx ·
  // ? help. Enter → input; Esc/q → passive.
  document.addEventListener('keydown', (e) => {
    if (e.target === $('text')) return;          // the input box owns its keys
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    if (k === 'Tab') {                           // walk / cycle
      e.preventDefault();
      if (mode === 'control') $('chan').onclick();          // control: next channel
      else setMode(mode === 'passive' ? 'input' : 'control');
      return;
    }
    if (k === 'Escape' || (k === 'q' && mode === 'control')) {
      e.preventDefault();
      if ($('help').classList.contains('on')) { toggleHelp(); return; }
      setMode('passive');
      return;
    }
    if (k === 'Enter') { e.preventDefault(); openInput(); return; }
    if (mode !== 'control') return;              // hotkeys are live only in CONTROL
    const keys = {
      ' ': () => act('toggle'),
      'h': () => act('skip-'),  'l': () => act('skip+'),
      'H': () => act('para-'),  'L': () => act('para+'),
      '<': () => $('prev').onclick(), '>': () => $('next').onclick(),
      ',': () => $('prev').onclick(), '.': () => $('next').onclick(),
      '-': () => act('vol-'),   '=': () => act('vol+'), '+': () => act('vol+'),
      'm': () => act('mute'),   'M': () => act('mute-keep'),
      'v': () => act('highlight'), 'p': () => act('clip-cursor', 1),
      'g': () => act('goto'),   'w': () => openWeb(),
      's': () => typedSeek(),   'o': () => typedOpen(),
      '[': () => act('speed-'), ']': () => act('speed+'),
      '0': () => act('speed0'), 'Backspace': () => act('speed0'),
      'r': () => act('replay', 1),
      'c': () => $('cc').onclick(new Event('x')),
      'x': () => $('sfx').onclick(new Event('x')),   // sfx — s is typed-seek, f is fit
      'f': () => $('fit').onclick(new Event('x')),
      'e': () => { localStorage.setItem('eink', einkOn() ? '0' : '1');
                   location.reload(); },
      '?': () => toggleHelp(),
    };
    const fn = keys[k];
    if (!fn) return;
    e.preventDefault();
    fn();
    resetHide();
  });
  $('xbtn').onclick = (e) => { e.stopPropagation(); setMode('passive'); };
  function drawSfx() {
    $('sfx').innerHTML = icon(sfxOn() ? 'bell' : 'bell-off');
    $('sfx').classList.toggle('lit', sfxOn());
  }
  drawSfx();
  function drawCc() { $('cc').classList.toggle('lit', subsOn()); }
  drawCc();
  $('cc').onclick = (e) => {
    e.stopPropagation();
    localStorage.setItem('subs', subsOn() ? '0' : '1');
    drawCc();
    if (!subsOn()) setSubtitle(null);
    resetHide();
  };
  $('sfx').onclick = (e) => {
    e.stopPropagation();
    localStorage.setItem('sfx', sfxOn() ? '0' : '1');
    drawSfx();
    if (sfxOn()) chime(true);              // audible confirmation + unlocks audio
    resetHide();
  };
  function drawFit() {
    $('fit').classList.toggle('lit', fitMode() !== 'auto');
    $('fit').style.opacity = fitMode() === 'fill' ? 0.55 : 1;
  }
  drawFit();
  $('fit').onclick = (e) => {
    e.stopPropagation();
    const next = { auto: 'fit', fit: 'fill', fill: 'auto' }[fitMode()] || 'auto';
    localStorage.setItem('fit', next);
    drawFit();
    // Re-style the image on screen right away, restoring the pan/zoom when
    // the new mode un-fits it.
    const el = layers[front];
    const f = wantFit(lastPurpose);
    applyFit(el, f);
    if (!f) kenBurns(el);
    $('cap').textContent = { auto: 'fit: auto — figures fit, art fills',
                             fit:  'fit: everything fits the screen',
                             fill: 'fill: everything covers the screen' }[next];
    $('cap').classList.remove('hide');
    $('cap').classList.add('on');
    clearTimeout(capTimer);
    capTimer = setTimeout(() => $('cap').classList.remove('on'), 2500);
    resetHide();
  };
  $('chan').onclick = () => {
    ch = ORDER[(ORDER.indexOf(ch) + 1) % ORDER.length];
    histIdx = 1;
    chTouched = Date.now();
    act('select');                 // persist → popup + other canvases follow
    $('title').textContent = '…';
    poll();
    resetHide();
  };
  $('pp').onclick  = () => act('toggle');
  $('skb').onclick = () => act('skip-');    // sentence back (±5s music/book)
  $('skf').onclick = () => act('skip+');    // sentence forward
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
        elif path == "/last":
            # When the canvas last received something to show — the reveal
            # flow polls this to know the image is up before speech resumes.
            last = HUB.last or {}
            self._json(200, {"t": last.get("t") or 0,
                             "kind": "sequence" if last.get("sequence")
                                     else "image" if last.get("image") else None})
        elif path == "/sessions":
            if not _authorized(self):
                self._json(401, {"error": "unauthorized"})
                return
            speaker = _last_speaker()
            self._json(200, {
                "speaker": ({"label": speaker.get("tmux_session")
                             or "last speaker"} if speaker else None),
                "amux": [s["name"] for s in _amux_sessions()],
            })
        elif path == "/pair":
            code = ""
            for kv in query.split("&"):
                k, _, v = kv.partition("=")
                if k == "c":
                    code = v
            token = _amux_token()
            if not token:
                self._send(503, b"no amux token configured on the host\n",
                           "text/plain")
            elif _pair_consume(code):
                self._send(200, (_PAIR_PAGE % json.dumps(token)).encode(),
                           "text/html; charset=utf-8")
            else:
                self._send(403, b"invalid or expired pairing code\n",
                           "text/plain")
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
            if HUB.last_video is not None:
                self._event(HUB.last_video)
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
        elif path == "/input":
            if not _authorized(self):
                self._json(401, {"error": "unauthorized"})
                return
            body = self._read_json() or {}
            ok, detail = send_input(str(body.get("text") or ""),
                                    str(body.get("target") or "speaker"))
            self._json(200 if ok else 400, {"ok": ok, "detail": detail})
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
            "purpose": ("figure" if body.get("purpose") == "figure" else None),
            # Which session's reply this visual belongs to — the page dims a
            # figure while a DIFFERENT session is speaking (else a stale
            # diagram reads as belonging to whatever voice is talking).
            "session": (str(body.get("session"))[:80]
                        if body.get("session") else None),
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
        sarg = str(body.get("sarg") or "")[:512]
        try:
            arg = max(1, min(999, int(body.get("arg") or 1)))
        except (TypeError, ValueError):
            arg = 1
        argv = ctl_argv(channel, action, arg, sarg)
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
    if os.environ.get("MEDIA_VISUAL_VIDEO", "1") != "0":
        threading.Thread(target=_video_poller, daemon=True).start()
    print(f"canvas on http://{args.bind}:{args.port}/  spool={spool_dir()}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
