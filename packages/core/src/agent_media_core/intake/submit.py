"""Submit: render text → play through sink-speech.

The intake "happy path" for any event source. Bypasses the legacy
drop-dir + watcher chain — render is in-process, playback is dispatched
straight to sink-speech via the route Coordinator.

Adapters (`hook_claude_code`, `cli`, future `matrix` / `ha-stt`) all
land here. The shape is intentionally narrow: take a populated
`Event`, hand back a history row id.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .._notify import notify
from ..render import render_text
from ..route import Coordinator
from ..sinks.speech import SinkSpeech
from ..state import StateStore
from ..types import Event, Priority, Target


log = logging.getLogger(__name__)


_DEFAULT_ENGINE = "edge"
_DEFAULT_VOICE: Optional[str] = None


def _default_engine() -> str:
    return (os.environ.get("MEDIA_RENDER_ENGINE")
            or os.environ.get("CLAUDE_TTS_ENGINE")  # legacy
            or _DEFAULT_ENGINE)


def _resolve_engine(event: Event) -> str:
    return event.engine or _default_engine()


def _resolve_voice(event: Event, engine: str) -> Optional[str]:
    """Resolve the voice for the *selected* engine.

    Voices live in engine-specific namespaces (edge 'en-AU-NatashaNeural',
    openai 'marin', qwen 'Cherry'), so resolution must know which engine will
    render — otherwise one engine's voice gets force-fed to another, which e.g.
    makes DashScope reject the request (qwen 400 InvalidParameter). Precedence:

      1. event.voice                  — explicit per-event override
      2. MEDIA_RENDER_VOICE_<ENGINE>  — per-engine config (the canonical knob)
      3. MEDIA_RENDER_VOICE           — generic, but ONLY when this engine is
                                        the configured default engine, so a
                                        generic voice can't bleed onto another
                                        engine
      4. CLAUDE_TTS_VOICE             — legacy, ONLY when this engine matches
                                        the legacy CLAUDE_TTS_ENGINE it paired
                                        with
      5. None                         — render_text falls back to the engine's
                                        own built-in default voice

    Returning None is safe: render_text applies the right per-engine default.
    """
    if event.voice:
        return event.voice

    per_engine = os.environ.get(f"MEDIA_RENDER_VOICE_{engine.upper().replace('-', '_')}")
    if per_engine:
        return per_engine

    if engine == (os.environ.get("MEDIA_RENDER_ENGINE") or _DEFAULT_ENGINE):
        generic = os.environ.get("MEDIA_RENDER_VOICE")
        if generic:
            return generic

    legacy_engine = os.environ.get("CLAUDE_TTS_ENGINE")
    if legacy_engine and engine == legacy_engine:
        legacy_voice = os.environ.get("CLAUDE_TTS_VOICE")
        if legacy_voice:
            return legacy_voice

    return _DEFAULT_VOICE


def _ext_for(engine: str) -> str:
    """qwen / realtime emit WAV, others MP3."""
    return "wav" if engine in ("qwen", "realtime") else "mp3"


def _split_sentences_with_paragraphs(text: str) -> tuple[list[str], list[int]]:
    """Segment text into sentences plus a parallel paragraph index per sentence.

    Splits on paragraph breaks first, then on sentence-ending punctuation
    within each paragraph. Common abbreviations are masked so they don't
    produce spurious splits. The returned paragraph indices are 0-based and
    monotonically non-decreasing; the popup uses them so H/L can jump a whole
    paragraph at a time while h/l step one sentence.
    """
    _ABBREV = re.compile(
        r'\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc|e\.g|i\.e|'
        r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec|[A-Z])\.'
    )

    def _sentences_in(para: str) -> list[str]:
        masked = _ABBREV.sub(lambda m: m.group(0)[:-1] + '\x00', para)
        parts = re.split(r'(?<=[.!?])\s+', masked.strip())
        return [p.replace('\x00', '.').strip() for p in parts if p.strip()]

    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    raw: list[tuple[str, int]] = []  # (sentence, paragraph index)
    for pi, para in enumerate(paragraphs):
        for s in _sentences_in(para):
            raw.append((s, pi))

    # Merge very short fragments (< 20 chars) into the preceding sentence
    # so standalone words like "Yes." or "OK." don't become solo clips — but
    # only within the same paragraph, so a short sentence that opens a new
    # paragraph stays its own clip and H/L paragraph-nav keeps working.
    sentences: list[str] = []
    para_idx: list[int] = []
    for part, pi in raw:
        if len(part) < 20 and sentences and para_idx[-1] == pi:
            sentences[-1] += ' ' + part
        else:
            sentences.append(part)
            para_idx.append(pi)
    if not sentences:
        return [text.strip()], [0]
    return sentences, para_idx


def _split_sentences(text: str) -> list[str]:
    """Sentence-level chunks for progressive TTS + highlight (paragraph map dropped)."""
    return _split_sentences_with_paragraphs(text)[0]


def _highlight_flag_path() -> Path:
    """File flag controlling auto-highlight: contents "1" = on, anything else = off."""
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "auto-highlight"


def _is_auto_highlight_enabled() -> bool:
    """Auto-highlight is opt-in. Env override wins; otherwise read flag file."""
    env = os.environ.get("MEDIA_AUTO_HIGHLIGHT")
    if env is not None:
        return env != "0"
    try:
        return _highlight_flag_path().read_text().strip() == "1"
    except OSError:
        return False


def toggle_auto_highlight() -> bool:
    """Flip the auto-highlight flag. Returns the new state (True = on)."""
    new_state = not _is_auto_highlight_enabled()
    p = _highlight_flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("1" if new_state else "0")
    return new_state


def _pane_scroll_pos(pane: str) -> tuple[bool, str]:
    """(in_copy_mode, scroll_position) for `pane`.

    scroll_position is lines scrolled up from the live bottom; it is only
    meaningful while the pane is in copy-mode (empty otherwise).
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{pane_in_mode}\t#{scroll_position}"],
            capture_output=True, text=True)
        if r.returncode != 0:
            return (False, "")
        in_mode, _, pos = r.stdout.rstrip("\n").partition("\t")
        return (in_mode.strip() == "1", pos.strip())
    except Exception:  # noqa: BLE001
        return (False, "")


def _last_client_activity(pane: str) -> Optional[int]:
    """Epoch of the most recent *user input* on `pane`'s session, or None.

    We want last-keystroke time, not last-output. `#{window_activity}` /
    `#{pane_activity}` track output, which is useless here: a Claude Code (or
    any TUI) pane redraws its spinner/status continuously, so output-activity
    is always ≈now even when the user is idle. `#{client_activity}` instead
    tracks when the *attached client* last sent data — i.e. real keystrokes —
    and freezes while the user isn't typing.

    A client is per-attachment, and this runs in a hook subprocess with no
    client of its own, so we resolve the pane's session and take the max
    client_activity across the clients attached to it. None = couldn't tell
    (no session / no clients / tmux error).
    """
    try:
        s = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_id}"],
            capture_output=True, text=True)
        if s.returncode != 0:
            return None
        sid = s.stdout.strip()
        if not sid:
            return None
        r = subprocess.run(
            ["tmux", "list-clients", "-t", sid, "-F", "#{client_activity}"],
            capture_output=True, text=True)
        if r.returncode != 0:
            return None
        epochs = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        return max(epochs) if epochs else None
    except Exception:  # noqa: BLE001
        return None


def _pane_recent_keystrokes(pane: str, within_s: float) -> bool:
    """True if the user typed in `pane`'s session within the last `within_s`s.

    Used to skip a highlight turn while the user is actively typing (the
    highlight would otherwise yank copy-mode out from under them). Backed by
    `_last_client_activity` (client input, not pane output — see there).
    Fails open (returns False) when we can't tell, so highlighting still
    happens rather than silently never running.
    """
    if within_s <= 0:
        return False
    last = _last_client_activity(pane)
    if last is None:
        return False
    return (time.time() - last) < within_s


def _force_highlight_flag_path() -> Path:
    """File flag for "highlight the next turn(s) even if I just typed".

    Contents = the epoch the user pressed the force key (popup/tmux
    `highlight-now`). Stays in effect until they type again (client activity
    moves past that epoch), at which point the gate clears it.
    """
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "force-highlight"


def set_force_highlight() -> None:
    """Stamp the force-highlight flag with the current time (the key press)."""
    p = _force_highlight_flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(time.time())))


def _popup_open_flag_path() -> Path:
    """Marker written by `media-popup-open` while the control popup is open.

    Contents = the pane the popup is controlling. An open popup means the user
    is attending to playback, so the highlight overrides its keystroke-skip
    while it's up."""
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "popup-open"


def _popup_open_for(pane: str) -> bool:
    """True if the control popup is currently open for `pane`."""
    try:
        return _popup_open_flag_path().read_text().strip() == pane
    except OSError:
        return False


def _force_highlight_active(pane: str) -> bool:
    """True if a force-highlight press is still in effect for `pane`.

    Active from the press until the user types again. "Types again" = client
    activity strictly past the press epoch; pressing the force key is itself
    client input at the press second, so equal-second still counts as active.
    Expired flags are unlinked so they don't linger. Fails open to *inactive*
    (no flag → normal skip behaviour applies)."""
    p = _force_highlight_flag_path()
    try:
        pressed = int(p.read_text().strip())
    except (OSError, ValueError):
        return False
    last = _last_client_activity(pane)
    if last is not None and last > pressed:
        p.unlink(missing_ok=True)
        return False
    return True


def _cursor_sig(pane: str) -> str:
    """A signature of the copy-mode cursor/viewport, to detect movement."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{scroll_position}\t#{copy_cursor_x}\t#{copy_cursor_y}"],
            capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _strip_markdown_inline(s: str) -> str:
    """Drop inline markdown markers the terminal renderer hides, so a snippet
    built from the *raw* spoken text matches the *rendered* pane text.

    e.g. the agent says "use `media toggle`" but Claude Code renders the code
    span without backticks, so a search for the literal backticked snippet
    never matches. Only markers that are unambiguously formatting are removed
    (backticks, **bold**, ~~strike~~, [text](url), heading #) — single * / _
    are left alone since they're often literal (source_pane, a*b)."""
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)   # [text](url) -> text
    s = s.replace("`", "")                            # inline code backticks
    s = re.sub(r"\*\*|__|~~", "", s)                  # bold / strikethrough
    s = re.sub(r"^\s*#{1,6}\s+", "", s)               # ATX heading marker
    return s


def _pane_anchor_width(pane: str) -> int:
    """Max anchor length that fits on one visual row of `pane`.

    Claude Code wraps its output at the full pane width (measured: a 32-col
    pane's content rows reach exactly 32), so cap to pane_width − 1 (a hair of
    slack at the wrap column), clamped to [15, 50]. Falls back to 50 (the old
    fixed cap) when the width can't be resolved.
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_width}"],
            capture_output=True, text=True, timeout=2)
        w = int(r.stdout.strip()) if r.returncode == 0 else 0
    except Exception:  # noqa: BLE001
        w = 0
    if w <= 0:
        return 50
    return min(50, max(15, w - 1))


def _anchor_for(text: str, max_len: int = 50) -> Optional[str]:
    """Single-line search anchor for spoken `text`, normalized to match the
    *rendered* pane (markdown stripped). Returns the plain (un-escaped) snippet
    — the longest line, trimmed to a word boundary within `max_len` chars so it
    fits on one visual row — or None if no line is long enough (>=15 chars) to
    be a unique search target. Shared by the auto-highlight (clip->cursor) and
    `replay-at-cursor` (cursor->clip) so both normalize text identically; if
    they drift, a clip that highlights wouldn't match-at-cursor.

    `max_len` defaults to 50 but the highlight path passes the target pane's
    width: tmux's search-backward matches within ONE visual row, so on a narrow
    pane (e.g. a 32-col phone) a 50-char anchor wraps and never matches. The
    anchor is always a prefix of a logical line, which renders from the left
    margin, so capping it to the pane width keeps it on that first row.
    """
    text = _strip_markdown_inline(text)
    # tmux search matches within one visual row, so flattening newlines (which
    # span wrapped rows) would never match — anchor on the longest single line.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    anchor = max(lines, key=len) if lines else text.strip()
    # A too-short anchor (one-word sentence, a bare heading) isn't unique:
    # search-backward then lands on spurious text. Skip those.
    if len(anchor) < 15:
        return None
    if len(anchor) <= max_len:
        return anchor
    if max_len < 15:
        # Pane too narrow to hold a unique (>=15 char) single-row anchor.
        return None
    cut = anchor[:max_len].rfind(" ")
    return anchor[:cut] if cut > 15 else anchor[:max_len]


def _tmux_highlight_text(text: str, *, first: bool = False,
                         force: bool = False) -> None:
    """Re-anchor copy-mode in the source pane onto the spoken text.

    Each call jumps to the bottom and searches backward for this sentence,
    so it tracks the right line regardless of prior position — including
    while the user has scrolled up in copy-mode. (We used to no-op when the
    user scrolled away from our last highlight; that rule is gone — the
    keystroke-recency skip in `_run` is the gentler way to stay out of the
    user's way, so highlighting now always follows the spoken text.)

    Off by default — opt-in via the popup's `v` toggle (which writes to
    `$XDG_STATE_HOME/agent-media/auto-highlight`). `MEDIA_AUTO_HIGHLIGHT=1`
    in env can override on a per-host basis. `first` and `force` are accepted
    for call-site compatibility but no longer change anchoring (every call
    re-anchors).
    """
    if not os.environ.get("TMUX"):
        return
    if not _is_auto_highlight_enabled():
        return
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return

    # Build the search anchor (markdown stripped, longest single line, trimmed
    # to one visual row). Cap to the target pane's width: tmux search-backward
    # is row-bound, so on a narrow pane (a 32-col phone) a 50-char anchor wraps
    # and matches nothing — the main reason highlighting "doesn't work on the
    # phone". None = no line unique enough to search for — leave the prior
    # highlight in place rather than stranding the view at the bottom.
    snippet = _anchor_for(text, max_len=_pane_anchor_width(pane))
    if snippet is None:
        return

    # Selection length = snippet length, capped so the highlight always fits
    # within a single visual row. cursor-right N beyond the row would drag
    # the viewport down, breaking the "viewport stays at sentence start"
    # invariant. Plain snippet (pre-regex-escape) gives the visual char count.
    select_len = len(snippet)

    snippet = re.sub(r'([][(){}^$.*+?|\\])', r'\\\1', snippet)

    # Flash duration: the selection stays visible for this long, then is
    # cleared while staying in copy-mode — pane stays scrolled to the
    # spoken text but the highlight fades. 0 = no auto-clear (selection
    # persists until the next sentence's highlight replaces it).
    flash_ms = int(os.environ.get("MEDIA_HIGHLIGHT_FLASH_MS", "1500"))

    import signal as _signal
    _pane_safe = re.sub(r"[^A-Za-z0-9_-]", "_", pane)
    pidfile = f"/tmp/media-highlight-clear-{_pane_safe}.pid"

    # Per-pane PID file so each new highlight can kill the previous
    # sentence's pending clear-timer before it races into our selection.
    try:
        with open(pidfile) as _f:
            _old_pgid = int(_f.read().strip())
        try:
            os.killpg(_old_pgid, _signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    except (OSError, ValueError):
        pass

    # Remember where the pane sat before we touch it, so a failed search can
    # put the view back instead of stranding the reader at the bottom.
    _prev_in_mode, _prev_pos = _pane_scroll_pos(pane)

    try:
        # Ensure copy-mode is active (no-op if it already is, e.g. the user
        # scrolled the pane — which leaves it in copy-mode).
        subprocess.run(["tmux", "copy-mode", "-t", pane],
                       capture_output=True)
        # Re-anchor from the bottom on EVERY sentence, then search backward.
        # The old code searched *forward* from the previous match's cursor
        # for sentences 2..N, which a manual scroll between sentences would
        # throw off (the cursor moves with the user). history-bottom +
        # search-backward finds the latest occurrence of this sentence
        # regardless of where the viewport currently sits.
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "history-bottom"],
                       capture_output=True)
        _before = _cursor_sig(pane)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X",
                        "search-backward", snippet],
                       capture_output=True)
        # If the search matched nothing, the cursor is still at the bottom
        # (history-bottom moved it there). Don't strand the reader at the end
        # of the buffer: restore the prior viewport if we had one (scroll back
        # up the same number of lines), otherwise just leave copy-mode.
        if _cursor_sig(pane) == _before:
            if _prev_in_mode and _prev_pos.isdigit() and int(_prev_pos) > 0:
                subprocess.run(["tmux", "send-keys", "-t", pane, "-X",
                                "-N", _prev_pos, "scroll-up"],
                               capture_output=True)
            else:
                subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "cancel"],
                               capture_output=True)
            return
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X",
                        "begin-selection"],
                       capture_output=True)
        if select_len > 0:
            subprocess.run(["tmux", "send-keys", "-t", pane,
                            "-X", "-N", str(select_len), "cursor-right"],
                           capture_output=True)
        if flash_ms > 0:
            # Detached clear-selection after flash window. start_new_session
            # makes this proc the session leader, so its PID is its pgid;
            # we record it so the next highlight can killpg it cleanly.
            proc = subprocess.Popen(
                ["sh", "-c",
                 f"sleep {flash_ms / 1000:.2f}; "
                 f"tmux send-keys -t {pane} -X clear-selection 2>/dev/null"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                with open(pidfile, "w") as _f:
                    _f.write(str(proc.pid))
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


class _HighlightScheduler:
    """Fire `_tmux_highlight_text` so the on-screen highlight lands *with* the
    audio instead of ahead of it.

    Speech routed through Snapcast (the `rooms` target) is only audible a fixed
    buffer later (snapserver.conf `buffer`, exposed as MEDIA_SNAPCAST_LATENCY_MS).
    play() returns as soon as mpv starts feeding the sink, so highlighting then
    runs ~that buffer ahead of the listener. We defer each natural highlight by
    that delay on a daemon timer — non-blocking, so the reader keeps feeding the
    next clip gaplessly meanwhile (a blocking sleep would inject silence on clips
    shorter than the delay).

    Because the reader advances on mpv-idle (feed done) while the listener is
    still hearing the clip, several timers can be in flight for back-to-back
    short clips; they're left to fire in order at their own onset times rather
    than cancelling one another. `cancel_pending()` drops the queue on a manual
    skip-to-end; `show(force=True)` (a manual h/l/H/L jump) abandons the queue
    and highlights immediately for instant feedback on the keypress; `drain()`
    lets the natural tail fire before the (often short-lived) process exits.
    """

    def __init__(self, delay_s: float, enabled: bool):
        self._delay = delay_s
        self._enabled = enabled
        self._lock = threading.Lock()
        self._timers: list[threading.Timer] = []
        self._dbg = bool(os.environ.get("MEDIA_HL_DEBUG"))
        if self._dbg:
            self._log(f"INIT delay_s={delay_s} enabled={enabled} pid={os.getpid()}")

    def _log(self, msg: str) -> None:
        try:
            with open("/tmp/am-hl-debug.log", "a") as f:
                f.write(f"{time.time():.3f} {msg}\n")
        except OSError:
            pass

    def _reap(self) -> None:
        with self._lock:
            self._timers = [t for t in self._timers if t.is_alive()]

    def show(self, sentence: str, *, first: bool, force: bool) -> None:
        if not self._enabled:
            return
        if force or self._delay <= 0:
            self.cancel_pending()
            if self._dbg:
                self._log(f"SHOW-NOW force={force} delay={self._delay} {sentence[:30]!r}")
            _tmux_highlight_text(sentence, first=first, force=force)
            return
        self._reap()
        if self._dbg:
            self._log(f"SCHEDULE +{self._delay}s {sentence[:30]!r}")

        def _fire():
            if self._dbg:
                self._log(f"FIRE {sentence[:30]!r}")
            _tmux_highlight_text(sentence, first=first)

        t = threading.Timer(self._delay, _fire)
        t.daemon = True
        with self._lock:
            self._timers.append(t)
        t.start()

    def cancel_pending(self) -> None:
        with self._lock:
            timers, self._timers = self._timers, []
        for t in timers:
            t.cancel()

    def drain(self) -> None:
        with self._lock:
            timers = list(self._timers)
        for t in timers:
            t.join()


def _speech_lock_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "speech-playback.lock"


def _speech_wait_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "speech-waiters"


# Priority -> numeric rank. Higher rank preempts lower; equal ranks queue.
_PRIO_RANK = {
    Priority.LOW: 0,
    Priority.NORMAL: 10,
    Priority.HIGH: 20,
    Priority.URGENT: 30,
}


def _rank_of(priority: Priority) -> int:
    return _PRIO_RANK.get(priority, _PRIO_RANK[Priority.NORMAL])


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check used to reap stale waiter entries."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. EPERM — alive but not ours
    return True


class _SpeechPlaybackLock:
    """Priority-aware serialization of speech *playback* across processes.

    Every session's hook talks to the one shared sink-speech broker and the
    one `am` Snapcast stream, so only one may play at a time. An exclusive
    `flock` on `speech-playback.lock` is the "currently speaking" token.
    Without it concurrent readers `loadfile replace` over each other and poll
    the same idle state, so their audio interleaves and each pane's highlight
    desyncs hopelessly.

    Priority (set by the intake hooks: notifications/prompts = HIGH, responses
    = NORMAL) decides what happens when someone else wants the token:

      * HIGH / URGENT  -> preempt: the current speaker steps aside at its next
                          sentence boundary (`should_yield` -> `yield_to_higher`)
                          and resumes when the higher clip finishes.
      * NORMAL         -> queue: wait for the token, never interrupt.
      * LOW            -> skip: if the token is already held, give up rather
                          than queue (ambient announcements aren't worth a wait).

    Waiters announce themselves in a lockless registry — one file per waiter
    under `speech-waiters/`, named `<pid>.<token>` and holding the waiter's
    rank — so a holder can tell whether anyone *higher* is waiting. Dead-pid
    entries are reaped on scan, so a crashed waiter never wedges anyone, and
    `flock` is released on fd close / process death, so a crashed holder frees
    the token. A paused response would hold it indefinitely, so non-LOW waiters
    give up after MEDIA_SPEECH_LOCK_TIMEOUT_S (default 600) and play
    unserialized rather than be lost. Set MEDIA_SPEECH_SERIALIZE=0 to disable.

    Rendering is intentionally left outside the lock so sessions still render
    their clips in parallel; only the broker hand-off serializes.
    """

    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._rank: int = _PRIO_RANK[Priority.NORMAL]
        # Lazily-created read-only handle for polling holder progress while we
        # wait for the token (see _holder_progress_sig).
        self._progress_store: Optional[StateStore] = None
        # Unique per instance so two locks in one process (e.g. tests) don't
        # collide; the pid prefix lets _max_other_rank reap dead waiters.
        self._token = f"{os.getpid()}.{uuid.uuid4().hex}"

    # ---- waiter registry -------------------------------------------------

    def _register(self) -> None:
        try:
            d = _speech_wait_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / self._token).write_text(str(self._rank))
        except OSError:
            pass

    def _unregister(self) -> None:
        try:
            (_speech_wait_dir() / self._token).unlink()
        except OSError:
            pass

    def _max_other_rank(self) -> int:
        """Highest rank among *other* live waiters; -1 if none. Reaps stale
        (dead-pid) entries as a side effect."""
        best = -1
        try:
            entries = list(_speech_wait_dir().iterdir())
        except OSError:
            return best
        for f in entries:
            if f.name == self._token:
                continue
            try:
                pid = int(f.name.split(".", 1)[0])
            except (ValueError, IndexError):
                continue
            if not _pid_alive(pid):
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            try:
                r = int(f.read_text().strip())
            except (OSError, ValueError):
                continue
            best = max(best, r)
        return best

    # ---- token acquisition ----------------------------------------------

    @staticmethod
    def _disabled() -> bool:
        return os.environ.get("MEDIA_SPEECH_SERIALIZE", "1").lower() in ("0", "false", "no")

    def acquire(self, priority: Priority = Priority.NORMAL) -> None:
        if self._disabled():
            return
        self._rank = _rank_of(priority)
        # LOW announcements skip rather than queue when anything's playing.
        self._take(skip_if_busy=self._rank <= _PRIO_RANK[Priority.LOW])

    def _holder_progress_sig(self) -> Optional[tuple]:
        """A cheap signature of the current speaker's progress: (clip uri,
        message start). The shared speech now_playing row is rewritten every
        sentence with the new clip uri, so this changes as long as someone is
        actively speaking — and stays put when the holder is paused, wedged, or
        gone. Returns None if it can't be read (treated as "no progress info").
        """
        store = self._progress_store
        if store is None:
            try:
                store = self._progress_store = StateStore()
            except Exception:  # noqa: BLE001
                return None
        try:
            np = store.get_now_playing("speech")
        except Exception:  # noqa: BLE001
            return None
        if not np:
            return None
        return (np.get("uri"), np.get("started_at"))

    def _take(self, *, skip_if_busy: bool = False) -> None:
        try:
            path = _speech_lock_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:  # noqa: BLE001
            log.warning("speech lock: open failed (%s); proceeding unserialized", e)
            return
        self._register()
        # Progress-aware give-up: the timeout measures how long the *current
        # speaker* has been STUCK, not how long we've waited. While someone is
        # actively speaking their clip `uri` in the shared speech now_playing
        # row advances every sentence; each change pushes the deadline forward.
        # So a long-but-healthy reply (or a queue of them) never forces us to
        # bail and play unserialized — only a genuinely wedged/paused holder,
        # whose clip stops advancing, still times out after `timeout`. Without
        # this, two long replies tripped the flat 600s deadline and interleaved.
        timeout = float(os.environ.get("MEDIA_SPEECH_LOCK_TIMEOUT_S", "600"))
        deadline = time.monotonic() + timeout
        last_sig = self._holder_progress_sig()
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    # Token held by someone else.
                    if skip_if_busy:
                        log.info("speech lock: low-priority clip skipped (busy)")
                        os.close(fd)
                        return
                    sig = self._holder_progress_sig()
                    if sig is not None and sig != last_sig:
                        # Current speaker advanced a clip — it's healthy, reset.
                        last_sig = sig
                        deadline = time.monotonic() + timeout
                    if time.monotonic() >= deadline:
                        log.warning("speech lock: holder stalled >%ss; proceeding "
                                    "unserialized", timeout)
                        os.close(fd)
                        return
                    time.sleep(0.2)
                    continue
                # Got the token, but hand it back if someone strictly higher is
                # also waiting — so priority wins admission no matter who won
                # the raw flock race.
                if self._max_other_rank() > self._rank:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    time.sleep(0.2)
                    continue
                self._fd = fd
                return
        finally:
            # A holder is no longer a waiter; also clears the entry on give-up.
            self._unregister()

    def should_yield(self) -> bool:
        """True when a strictly higher-priority speaker is waiting."""
        if self._fd is None:
            return False
        return self._max_other_rank() > self._rank

    def yield_to_higher(self) -> None:
        """Step aside for a higher-priority waiter, then re-take the token.

        Blocks until re-acquired. Call only between clips (broker idle), so
        there's nothing to pause or seek — the caller just replays its next
        sentence once this returns.
        """
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        time.sleep(0.05)  # grace for the higher waiter to grab the token
        # Reclaim with precedence over fresh *equal-rank* replies. We're an
        # in-progress message that was forced aside for a notification; without
        # this bump we'd race a not-yet-started reply of the same priority for
        # the token on the way back and could lose — so a whole other long reply
        # plays before we resume (the A -> notif -> B -> A interleave). Bump just
        # above our base rank but below the next tier, so genuine HIGH speakers
        # still preempt us; clamp so repeated yields can't creep into HIGH.
        if self._rank < _PRIO_RANK[Priority.HIGH]:
            self._rank = min(self._rank + 1, _PRIO_RANK[Priority.HIGH] - 1)
        self._take()

    def release(self) -> None:
        if self._fd is None:
            self._unregister()
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._unregister()

    def __enter__(self) -> "_SpeechPlaybackLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> bool:
        self.release()
        return False


def _audio_dir() -> Path:
    """Where rendered audio lands. Cache-y, GC-able. Per-user.

    On Termux+proot, the proot-side `/home/<user>` is bind-mounted to
    the Termux-native `/data/data/com.termux/files/home`, but services
    running outside the proot (sink-speech under runit) only see the
    Termux-native path. Prefer that root when it's present so audio
    paths handed over IPC resolve identically from both sides.
    """
    explicit = os.environ.get("MEDIA_AUDIO_DIR")
    if explicit:
        d = Path(explicit)
        d.mkdir(parents=True, exist_ok=True)
        return d
    termux_home = Path("/data/data/com.termux/files/home")
    base: Path
    if termux_home.is_dir():
        base = Path(os.environ.get("XDG_CACHE_HOME",
                                   str(termux_home / ".cache")))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME",
                                   str(Path.home() / ".cache")))
    d = base / "agent-media" / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clip_duration(path: Path) -> float:
    """Return audio duration in seconds via ffprobe, or 0.0 on failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        return float(r.stdout.strip()) if r.returncode == 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _tmux_session_for_pane(pane: str) -> str:
    """Resolve a tmux pane id (e.g. ``%41``) to its session name, or "".

    Best-effort: returns "" when there's no pane, no tmux server, or the pane
    has already closed.
    """
    if not pane or "#{" in pane:
        return ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _nav_flag_path(target: Target) -> Path:
    """File the popup writes to request a sentence/paragraph jump (`media skip`).

    Holds the absolute target sentence index for the live reader loop to jump
    to next. One per target since there's a single broker per target.
    """
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    d = state / "agent-media"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"nav-request-{target.name}"


def _read_nav_request(target: Target) -> Optional[int]:
    """Pop a pending nav request (target sentence index), or None. Clears it."""
    path = _nav_flag_path(target)
    try:
        raw = path.read_text().strip()
        path.unlink()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _wait_for_clip(sink: SinkSpeech, target: Target,
                   on_poll: Optional[Callable[[], None]] = None) -> Optional[int]:
    """Wait for sink-speech to start then finish the current clip.

    Returns None on natural end-of-clip; returns an absolute sentence index
    when the popup requested a sentence/paragraph jump (`media skip`), so the
    reader loop can re-load that sentence instead of advancing by one. The
    nav check runs even while paused, so you can step the highlight forward or
    back through a paused response.

    `on_poll`, if given, is invoked once per polling iteration (including
    while paused and during the initial wait-for-start) — used to watch the
    broker mute state and toggle the music duck to match.

    Requires two consecutive idle readings before declaring done — a
    single True from sink.idle() can be a transient IPC error mid-play
    (the method returns True on MpvIpcError), which would otherwise cause
    the next clip to cut the current one short.
    """
    for _ in range(20):
        if on_poll is not None:
            on_poll()
        if not sink.idle(target):
            break
        time.sleep(0.05)
    idle_streak = 0
    elapsed = 0
    while elapsed < 1200:
        if on_poll is not None:
            on_poll()
        nav = _read_nav_request(target)
        if nav is not None:
            return nav
        # A user pause (popup Space) holds the clip here indefinitely: a
        # paused clip never goes idle, so without this it would burn the
        # ~120s budget and then force-advance to the next sentence,
        # resuming the response on its own. Hold without consuming budget;
        # resume picks up where it left off.
        if sink.paused(target):
            time.sleep(0.1)
            continue
        if sink.idle(target):
            idle_streak += 1
            if idle_streak >= 3:
                break
        else:
            idle_streak = 0
        time.sleep(0.1)
        elapsed += 1
    return None


class _MuteDuckWatcher:
    """Track the speech broker's mute state across a response and toggle the
    music duck to match: a mid-response mute (popup `m`) makes the remaining
    sentences silent, so the ducked music can come back up; un-muting re-ducks
    it while audible speech resumes.

    State lives here (not in `_wait_for_clip`, which is per-clip) so a mute set
    during one sentence is remembered across the sentence boundary and isn't
    re-ducked when the next silent clip loads. Restore at end-of-response is
    left to `coordinator.after_speech()`, which is idempotent with whatever
    duck state we leave behind.
    """

    def __init__(self, sink: SinkSpeech, target: Target,
                 coordinator: Coordinator) -> None:
        self._sink = sink
        self._target = target
        self._coord = coordinator
        # A fresh response always un-mutes itself on sentence 0
        # (reset_state), so we start from "audible / ducked".
        self._muted = False

    def poll(self) -> None:
        try:
            muted = self._sink.muted(self._target)
        except Exception:  # noqa: BLE001
            return  # can't read mute → don't touch the duck
        if muted == self._muted:
            return
        self._muted = muted
        if muted:
            self._coord.release_music_duck()
        else:
            self._coord.reapply_music_duck()


def submit_event(event: Event,
                 *,
                 state: Optional[StateStore] = None,
                 coordinator: Optional[Coordinator] = None,
                 sink: Optional[SinkSpeech] = None) -> Optional[int]:
    """Render `event` sentence-by-sentence and play through sink-speech.

    Each sentence is rendered to its own clip and played in order. The
    source tmux pane highlights the current sentence as it starts playing
    (karaoke-style). Returns the history-row id, or None on failure.

    Blocks until all clips finish. Callers that need fire-and-forget
    should run this in a thread.
    """
    text = event.text.strip()
    if not text:
        return None

    state = state or StateStore()
    coordinator = coordinator or Coordinator(state=state)
    sink = sink or SinkSpeech()
    target = event.target or Target(
        name=os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET", "local"))

    engine = _resolve_engine(event)
    voice = _resolve_voice(event, engine)
    ext = _ext_for(engine)

    audio_dir = _audio_dir()
    # Per-submission unique: second-resolution time is NOT enough — two
    # concurrent sessions (both source "claude-code") finishing a reply in the
    # same second would render to identical clip paths and clobber each other's
    # audio, so one session could end up playing another's clips. pid + a short
    # random token guarantees uniqueness across processes and within one.
    stamp = (time.strftime("%Y%m%dT%H%M%S")
             + f"-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    started_at = time.time()

    # The tmux pane that produced this speech (the Claude Code TTS hook runs
    # inside the agent's pane, so TMUX_PANE points at it). Persisted into
    # now_playing/history so the popup can show *which* pane is currently
    # talking, rather than the pane that happens to be active.
    source_pane = (event.metadata or {}).get("pane") or os.environ.get("TMUX_PANE", "")
    # The Claude Code session id (from the hook payload), persisted so the
    # popup can resume the conversation when its source pane has since been
    # closed — `goto-pane` falls back to `claude --resume <session>`.
    source_session = (event.metadata or {}).get("session") or ""
    # The tmux session that owns the source pane, captured now while the pane
    # is guaranteed alive. Persisted so the popup's < / > can scope history
    # traversal to "this tmux session's clips" without resolving a (possibly
    # since-closed) pane id back to its session at browse time.
    source_tmux_session = _tmux_session_for_pane(source_pane)

    # Durable per-pane / per-session mute (popup `M` / `media mute-pane`): a
    # muted pane still renders its clips and records a replayable history row,
    # but is never played through the broker and never ducks music. Decided
    # once, up front, so we also skip the remote pre-pause below.
    muted = state.resolve_mute(source_pane, source_tmux_session)

    fallback_info: dict = {}
    _fallback_lock = threading.Lock()

    def _on_fallback(failed_engine: str, err: str) -> None:
        short = err.strip().splitlines()[0] if err else "no detail"
        kind = "render-fallback"
        if "insufficient_quota" in err:
            kind = "render-quota"
        log.warning("intake: %s engine failed (%s); falling back to edge",
                    failed_engine, short)
        state.log_error("intake",
                        f"render {failed_engine} failed, fell back to edge",
                        extras={"kind": kind, "engine": failed_engine,
                                "detail": short[:300],
                                "source": event.source.value})
        with _fallback_lock:
            fallback_info.update({
                "from_engine": failed_engine,
                "fallback_engine": "edge",
                "kind": kind,
                "detail": short[:300],
            })
        if kind == "render-quota":
            title = f"agent-media: {failed_engine} quota exhausted"
            body = "Falling back to edge for now."
        else:
            title = f"agent-media: {failed_engine} render failed"
            body = f"Falling back to edge. {short[:120]}"
        notify(key=f"render-fallback-{failed_engine}",
               title=title, content=body)

    # Start remote MPRIS detect-and-pause in background so SSH cold-connect
    # (~4.8s) overlaps with sentence rendering below. Skipped when muted —
    # nothing will play, so there's nothing to pause for.
    if not muted:
        coordinator.pre_pause_remote()

    sentences, sent_para = _split_sentences_with_paragraphs(text)

    # Submit all sentence renders in parallel. Sentence 0 starts playing as
    # soon as its render finishes (~0.5s); the rest are done by then.
    outfiles = [
        audio_dir / f"{stamp}--{event.source.value}--{i:03d}.{ext}"
        for i in range(len(sentences))
    ]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(sentences) or 1)
    futures = [
        executor.submit(render_text, sentence, outfile,
                        engine=engine, voice=voice, on_fallback=_on_fallback)
        for sentence, outfile in zip(sentences, outfiles)
    ]
    executor.shutdown(wait=False)  # don't block; futures stay live

    # First clip path for sidecar / now_playing (render may still be in flight).
    first_clip = outfiles[0]

    # Sidecar lives next to the first clip so the popup can show the full text.
    try:
        first_clip.with_suffix(".txt").write_text(text)
    except OSError as e:  # noqa: BLE001
        log.warning("intake: text sidecar write failed: %s", e)

    # Only highlight for hook sources — CLI text is never in the pane.
    from ..types import Source as _Source
    do_highlight = event.source not in (_Source.CLI,)

    # Skip this turn's highlighting if the user has typed in the source pane
    # recently: grabbing copy-mode mid-keystroke would yank the view out from
    # under them. Threshold via MEDIA_HIGHLIGHT_KEYSTROKE_S (0 disables the
    # skip). Two things override the skip ("I'm attending — follow this one"):
    # the `highlight-now` force key (tmux `prefix V`, until you type again), and
    # the control popup being open for this pane.
    if do_highlight:
        _ks_window_s = float(
            os.environ.get("MEDIA_HIGHLIGHT_KEYSTROKE_S", "5"))
        _src_pane = os.environ.get("TMUX_PANE")
        if (_src_pane and _pane_recent_keystrokes(_src_pane, _ks_window_s)
                and not _force_highlight_active(_src_pane)
                and not _popup_open_for(_src_pane)):
            do_highlight = False

    # Phase 1: resolve all render futures and collect clip durations.
    # Parallel renders are mostly done by now; future.result() is instant
    # for finished ones and waits briefly for the last stragglers.
    clip_data: list[tuple[str, Path]] = []  # (sentence, clip_path)
    clip_para: list[int] = []               # paragraph index per surviving clip
    for sentence, pi, outfile, future in zip(sentences, sent_para,
                                             outfiles, futures):
        try:
            ok, err = future.result()
        except Exception as exc:  # noqa: BLE001
            log.warning("intake: render future raised: %s", exc)
            continue
        if not ok:
            log.warning("intake: render failed for sentence (%s): %s", engine, err)
            state.log_error("intake", f"render failed ({engine})",
                            extras={"err": err, "source": event.source.value})
            continue
        clip_path = outfile
        with _fallback_lock:
            has_fallback = bool(fallback_info)
        if has_fallback and clip_path.suffix == ".wav":
            renamed = clip_path.with_suffix(".mp3")
            try:
                clip_path.rename(renamed)
                clip_path = renamed
            except OSError:
                pass
        clip_data.append((sentence, clip_path))
        clip_para.append(pi)

    if not clip_data:
        return None

    # Compute per-clip offsets for a single spanning progress bar.
    durations = [_clip_duration(p) for _, p in clip_data]
    total_duration_s = sum(durations)

    # After mpv reports idle, Snapcast still has buffered audio to drain.
    # Delay the next highlight by this amount so it fires when the audio
    # actually starts at the listener's end rather than when mpv finishes
    # writing to the PipeWire sink.
    _highlight_delay_s = float(
        os.environ.get("MEDIA_SNAPCAST_LATENCY_MS", "500")) / 1000.0

    # Cumulative start offset of each clip on the response-wide timeline.
    offsets: list[float] = []
    _acc = 0.0
    for d in durations:
        offsets.append(_acc)
        _acc += d
    _clip_sentences = [s for s, _ in clip_data]

    # A muted pane skips playback entirely: the clips are already rendered
    # (above), so we fall straight through to the history write below — no
    # broker, no before/after_speech, no duck.
    if not muted:
        # Serialize playback across sessions: only one response feeds the shared
        # broker/Snapcast stream at a time (rendering above already ran in
        # parallel). Acquired before before_speech() so other media isn't paused
        # while we're still queued behind another speaker.
        playback_lock = _SpeechPlaybackLock()
        playback_lock.acquire(event.priority)
        played_any = False
        n = len(clip_data)
        highlighter = _HighlightScheduler(_highlight_delay_s, do_highlight)
        # Drop any stale jump request left by a previous response.
        _nav_flag_path(target).unlink(missing_ok=True)
        try:
            coordinator.before_speech()
            mute_watcher = _MuteDuckWatcher(sink, target, coordinator)
            i = 0
            nav_jump = False  # True when this clip was reached via a popup skip
            while 0 <= i < n:
                # Step aside between sentences if a higher-priority speaker (e.g. a
                # notification) is waiting; resume this same sentence once it's done.
                if playback_lock.should_yield():
                    playback_lock.yield_to_higher()
                sentence, clip_path = clip_data[i]
                # Update now_playing so cmd_status can show a response-wide bar,
                # `media current-sentence` can return the active sentence, and
                # `media skip` can read the sentence/paragraph map for live nav.
                state.set_now_playing(
                    "speech", uri=str(clip_path), started_at=started_at,
                    target=target.name,
                    extras={"text": text, "source": event.source.value,
                            "engine": engine, "voice": voice,
                            "clip_offset_s": offsets[i],
                            "total_duration_s": total_duration_s,
                            "source_pane": source_pane,
                            "source_session": source_session,
                            "source_tmux_session": source_tmux_session,
                            "current_sentence": sentence,
                            "current_sentence_idx": i,
                            "clip_paragraph_idx": clip_para,
                            "clip_sentences": _clip_sentences})
                try:
                    # Only the first sentence resets a lingering pause/mute;
                    # later sentences preserve a pause the user made mid-response.
                    sink.play(str(clip_path), target, reset_state=(i == 0))
                    played_any = True
                except Exception as e:  # noqa: BLE001
                    log.warning("intake: sink-speech.play failed: %s", e)
                    state.log_error("intake", "sink-speech play failed",
                                    extras={"detail": str(e),
                                            "source": event.source.value})
                    i += 1
                    nav_jump = False
                    continue
                # Highlight is deferred by the Snapcast buffer so it lands with the
                # audio (a manual jump forces it on immediately even if the reader
                # scrolled away; a natural advance respects the scroll position).
                highlighter.show(sentence, first=(i == 0), force=nav_jump)
                nav = _wait_for_clip(sink, target, on_poll=mute_watcher.poll)
                if nav is None:
                    i += 1
                    nav_jump = False
                else:
                    # Popup requested a sentence/paragraph jump. A target past the
                    # last clip means "skip to the end" → finish the response.
                    if nav >= n:
                        highlighter.cancel_pending()
                        break
                    i = max(0, nav)
                    nav_jump = True
        finally:
            highlighter.drain()
            coordinator.after_speech()
            state.clear_now_playing("speech")
            playback_lock.release()

        if not played_any:
            return None

    extras = {"engine": engine, "voice": voice,
              "priority": event.priority.value,
              "source_pane": source_pane,
              "source_session": source_session,
              "source_tmux_session": source_tmux_session,
              "clip_uris": [str(p) for _, p in clip_data],
          "clip_sentences": [s for s, _ in clip_data],
          "clip_durations_s": durations,
          "clip_paragraph_idx": clip_para,
              **(event.metadata or {})}
    if fallback_info:
        extras["fallback"] = fallback_info
    if muted:
        extras["muted"] = True   # rendered but never played (popup can replay)
    return state.add_history(
        sink="speech",
        uri=str(first_clip),
        started_at=started_at,
        ended_at=time.time(),
        target=target.name,
        source=event.source.value,
        text=text,
        extras=extras,
    )


def submit_stream(sentences,
                  event: Event,
                  *,
                  state: Optional[StateStore] = None,
                  coordinator: Optional[Coordinator] = None,
                  sink: Optional[SinkSpeech] = None) -> Optional[int]:
    """Streaming sibling of `submit_event`: speak sentences as they arrive.

    `sentences` is an iterable yielding cleaned sentence strings as a producer
    (e.g. a model's token stream) completes them. Each sentence is rendered the
    instant it arrives and played in order through the same long-running
    sink-speech broker, so audio for sentence 1 starts while the model is still
    generating the rest — the key win over `submit_event`, which needs the
    whole reply first.

    Remote players are paused/resumed once (not per sentence). Best-effort
    karaoke highlight + back/forward nav over already-spoken sentences; the
    response-wide progress bar grows as sentences arrive (total length isn't
    known up front). One history row is written at the end.

    Blocks until the producer is exhausted and all clips finish. Callers that
    need fire-and-forget should run this in a thread.
    """
    from ..types import Source as _Source

    state = state or StateStore()
    coordinator = coordinator or Coordinator(state=state)
    sink = sink or SinkSpeech()
    target = event.target or Target(
        name=os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET", "local"))

    engine = _resolve_engine(event)
    voice = _resolve_voice(event, engine)
    ext = _ext_for(engine)

    audio_dir = _audio_dir()
    # Per-submission unique: second-resolution time is NOT enough — two
    # concurrent sessions (both source "claude-code") finishing a reply in the
    # same second would render to identical clip paths and clobber each other's
    # audio, so one session could end up playing another's clips. pid + a short
    # random token guarantees uniqueness across processes and within one.
    stamp = (time.strftime("%Y%m%dT%H%M%S")
             + f"-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    started_at = time.time()

    source_pane = (event.metadata or {}).get("pane") or os.environ.get("TMUX_PANE", "")
    source_session = (event.metadata or {}).get("session") or ""
    source_tmux_session = _tmux_session_for_pane(source_pane)
    # Durable per-pane / per-session mute: render the stream into clips for
    # popup replay/history, but never play it or duck music. See submit_event.
    muted = state.resolve_mute(source_pane, source_tmux_session)
    do_highlight = event.source not in (_Source.CLI,)
    # Snapcast buffers audio after mpv starts writing, so the sound reaches the
    # listener a beat later than play() returns. Hold the highlight that long so
    # it lands with the speech rather than ahead of it.
    _highlight_delay_s = float(
        os.environ.get("MEDIA_SNAPCAST_LATENCY_MS", "500")) / 1000.0

    fallback_info: dict = {}
    _fallback_lock = threading.Lock()

    def _on_fallback(failed_engine: str, err: str) -> None:
        short = err.strip().splitlines()[0] if err else "no detail"
        kind = "render-fallback"
        if "insufficient_quota" in err:
            kind = "render-quota"
        log.warning("intake-stream: %s engine failed (%s); falling back to edge",
                    failed_engine, short)
        state.log_error("intake",
                        f"render {failed_engine} failed, fell back to edge",
                        extras={"kind": kind, "engine": failed_engine,
                                "detail": short[:300],
                                "source": event.source.value})
        with _fallback_lock:
            fallback_info.update({"from_engine": failed_engine,
                                  "fallback_engine": "edge", "kind": kind,
                                  "detail": short[:300]})
        if kind == "render-quota":
            title = f"agent-media: {failed_engine} quota exhausted"
            body = "Falling back to edge for now."
        else:
            title = f"agent-media: {failed_engine} render failed"
            body = f"Falling back to edge. {short[:120]}"
        notify(key=f"render-fallback-{failed_engine}", title=title, content=body)

    # Shared, lock-guarded clip table: the producer thread appends sentences and
    # kicks off their renders the instant they arrive; the play loop consumes in
    # order. Renders run a few ahead of playback via a bounded pool.
    workers = max(1, int(os.environ.get("MEDIA_STREAM_RENDER_WORKERS", "3") or 3))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    cond = threading.Condition()
    sents: list[str] = []
    futures: list = []
    paths: list[Path] = []
    producer_done = threading.Event()

    def _produce() -> None:
        try:
            for s in sentences:
                if not s or not s.strip():
                    continue
                outfile = audio_dir / f"{stamp}--{event.source.value}--{len(sents):03d}.{ext}"
                fut = pool.submit(render_text, s, outfile,
                                  engine=engine, voice=voice, on_fallback=_on_fallback)
                with cond:
                    sents.append(s)
                    futures.append(fut)
                    paths.append(outfile)
                    cond.notify_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("intake-stream: producer raised: %s", exc)
        finally:
            producer_done.set()
            with cond:
                cond.notify_all()

    def _get(i: int):
        """(sentence, future, path) for clip i, waiting until it's enqueued.
        Returns None when no clip i will ever exist (producer finished)."""
        with cond:
            while i >= len(sents) and not producer_done.is_set():
                cond.wait(timeout=0.5)
            if i >= len(sents):
                return None
            return sents[i], futures[i], paths[i]

    # Skipped when muted — nothing plays, so there's nothing to pause for.
    if not muted:
        coordinator.pre_pause_remote()
    _nav_flag_path(target).unlink(missing_ok=True)
    producer = threading.Thread(target=_produce, daemon=True)
    producer.start()

    durations: dict[int, float] = {}   # measured clip durations, by index
    played_any = False
    first_clip: Optional[Path] = None

    if muted:
        # Drain the producer into clips so the response is in history and the
        # popup can replay it — but never play it or touch the coordinator.
        i = 0
        try:
            while True:
                item = _get(i)
                if item is None:
                    break
                sentence, fut, clip_path = item
                try:
                    ok, err = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("intake-stream: render future raised: %s", exc)
                    i += 1
                    continue
                if not ok:
                    log.warning("intake-stream: render failed (%s): %s", engine, err)
                    state.log_error("intake", f"render failed ({engine})",
                                    extras={"err": err, "source": event.source.value})
                    i += 1
                    continue
                with _fallback_lock:
                    has_fallback = bool(fallback_info)
                if has_fallback and clip_path.suffix == ".wav":
                    renamed = clip_path.with_suffix(".mp3")
                    try:
                        clip_path.rename(renamed)
                        clip_path = renamed
                        paths[i] = renamed
                    except OSError:
                        pass
                durations[i] = _clip_duration(clip_path)
                if first_clip is None:
                    first_clip = clip_path
                played_any = True
                i += 1
        finally:
            producer_done.wait(timeout=2.0)
            pool.shutdown(wait=False)
        # Sidecar with the full text so the popup shows the whole response.
        if first_clip is not None:
            with cond:
                known = list(sents)
            try:
                first_clip.with_suffix(".txt").write_text(" ".join(known))
            except OSError:
                pass
    else:
        before_called = False
        mute_watcher = _MuteDuckWatcher(sink, target, coordinator)
        highlighter = _HighlightScheduler(_highlight_delay_s, do_highlight)
        # Serialize playback across sessions (rendering keeps streaming in
        # parallel via the producer thread while we wait our turn for the broker).
        playback_lock = _SpeechPlaybackLock()
        playback_lock.acquire(event.priority)
        i = 0
        nav_jump = False
        try:
            while True:
                # Step aside between sentences for a higher-priority speaker;
                # resume this clip once it's done. Only after the first clip has
                # played, so before_speech ran and there's something to resume.
                if before_called and playback_lock.should_yield():
                    playback_lock.yield_to_higher()
                item = _get(i)
                if item is None:
                    break
                sentence, fut, clip_path = item
                try:
                    ok, err = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("intake-stream: render future raised: %s", exc)
                    i += 1
                    nav_jump = False
                    continue
                if not ok:
                    log.warning("intake-stream: render failed (%s): %s", engine, err)
                    state.log_error("intake", f"render failed ({engine})",
                                    extras={"err": err, "source": event.source.value})
                    i += 1
                    nav_jump = False
                    continue
                with _fallback_lock:
                    has_fallback = bool(fallback_info)
                if has_fallback and clip_path.suffix == ".wav":
                    renamed = clip_path.with_suffix(".mp3")
                    try:
                        clip_path.rename(renamed)
                        clip_path = renamed
                        paths[i] = renamed
                    except OSError:
                        pass

                if not before_called:
                    coordinator.before_speech()
                    before_called = True

                durations[i] = _clip_duration(clip_path)
                with cond:
                    known = list(sents)
                if first_clip is None:
                    first_clip = clip_path
                # Keep the first clip's text sidecar updated with everything spoken
                # so far, so the popup can show the running response.
                try:
                    first_clip.with_suffix(".txt").write_text(" ".join(known))
                except OSError:
                    pass

                offset = sum(durations.get(k, 0.0) for k in range(i))
                total = sum(durations.values())  # grows as more clips render
                state.set_now_playing(
                    "speech", uri=str(clip_path), started_at=started_at,
                    target=target.name,
                    extras={"text": " ".join(known), "source": event.source.value,
                            "engine": engine, "voice": voice,
                            "clip_offset_s": offset,
                            "total_duration_s": total,
                            "source_pane": source_pane,
                            "source_session": source_session,
                            "source_tmux_session": source_tmux_session,
                            "current_sentence": sentence,
                            "current_sentence_idx": i,
                            "clip_sentences": known,
                            "streaming": True})
                try:
                    sink.play(str(clip_path), target, reset_state=(i == 0))
                    played_any = True
                except Exception as e:  # noqa: BLE001
                    log.warning("intake-stream: sink-speech.play failed: %s", e)
                    state.log_error("intake", "sink-speech play failed",
                                    extras={"detail": str(e),
                                            "source": event.source.value})
                    i += 1
                    nav_jump = False
                    continue
                # Deferred so the highlight lands with the Snapcast-buffered audio.
                highlighter.show(sentence, first=(i == 0), force=nav_jump)

                nav = _wait_for_clip(sink, target, on_poll=mute_watcher.poll)
                if nav is None:
                    i += 1
                    nav_jump = False
                else:
                    with cond:
                        count = len(sents)
                        done = producer_done.is_set()
                    if nav >= count:
                        # "Skip to the end": stop if the producer's finished,
                        # otherwise fall through to whatever arrives next.
                        if done:
                            highlighter.cancel_pending()
                            break
                        i = count
                        nav_jump = False
                    else:
                        i = max(0, nav)
                        nav_jump = True
        finally:
            highlighter.drain()
            coordinator.after_speech()
            state.clear_now_playing("speech")
            playback_lock.release()
            producer_done.wait(timeout=2.0)
            pool.shutdown(wait=False)

    if not played_any:
        return None

    with cond:
        all_sents = list(sents)
        all_paths = list(paths)
    full_text = " ".join(all_sents)
    extras = {"engine": engine, "voice": voice,
              "priority": event.priority.value,
              "source_pane": source_pane,
              "source_session": source_session,
              "source_tmux_session": source_tmux_session,
              "clip_uris": [str(p) for p in all_paths],
              "clip_sentences": all_sents,
              "clip_durations_s": [durations.get(k, 0.0) for k in range(len(all_paths))],
              "streaming": True,
              **(event.metadata or {})}
    if fallback_info:
        extras["fallback"] = fallback_info
    if muted:
        extras["muted"] = True   # rendered but never played (popup can replay)
    return state.add_history(
        sink="speech",
        uri=str(first_clip),
        started_at=started_at,
        ended_at=time.time(),
        target=target.name,
        source=event.source.value,
        text=full_text,
        extras=extras,
    )
