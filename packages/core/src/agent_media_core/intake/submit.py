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
import hashlib
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
    """qwen / realtime / kokoro emit WAV, others MP3."""
    return "wav" if engine in ("qwen", "realtime", "kokoro") else "mp3"


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


# Backstop so a forgotten/stale force-highlight flag can't override the
# keystroke-skip indefinitely (a real press self-heals on the next keystroke).
_FORCE_MAX_AGE_S = 1800


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

    Active from the press until the user types again ("types again" = client
    activity strictly past the press epoch; pressing the force key is itself
    client input at the press second, so equal-second still counts as active).
    Expired flags are unlinked so they don't linger.

    Fails to *inactive* when we can't read client activity, and ignores a flag
    older than FORCE_MAX_AGE_S — otherwise a stale flag (e.g. left by a test, or
    a moment when no client is attached so activity reads None) would silently
    override the keystroke-skip forever. A genuine press self-heals on the next
    keystroke; the max-age is just a backstop."""
    p = _force_highlight_flag_path()
    try:
        pressed = int(p.read_text().strip())
    except (OSError, ValueError):
        return False
    if time.time() - pressed > _FORCE_MAX_AGE_S:
        p.unlink(missing_ok=True)
        return False
    last = _last_client_activity(pane)
    if last is None:
        return False                      # can't tell → don't override
    if last > pressed:
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


def _pane_alternate_on(pane: str) -> bool:
    """True if `pane` is on the alternate screen — a fullscreen TUI (Claude Code
    and friends). That also means no tmux scrollback, so copy-mode only ever
    sees the *visible* screen, and the app likely has its own scroll/transcript
    view (e.g. Claude's Ctrl+O) that a held copy-mode would block."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{alternate_on}"],
            capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "1"
    except Exception:  # noqa: BLE001
        return False


def _highlight_dump_enabled() -> bool:
    """Opt-in (MEDIA_HIGHLIGHT_DUMP=1): in Claude's fullscreen mode, drive its
    Ctrl+O → `[` transcript-print so the spoken sentence — usually scrolled off
    the alt-screen where copy-mode can't reach it — lands in real scrollback the
    highlight can follow. Off by default; see `_dump_transcript`."""
    return os.environ.get("MEDIA_HIGHLIGHT_DUMP") == "1"


# Pane we've driven into a transcript dump this response (fullscreen → normal
# screen), so the response-end restore knows to send Escape. One response plays
# per process, so a single in-process slot is enough.
_dumped_pane: Optional[str] = None


def _dump_transcript(pane: str) -> bool:
    """Print Claude Code's conversation into `pane`'s native scrollback via its
    Ctrl+O (transcript) → `[` (print) keys, turning a fullscreen (alt-screen)
    pane into a normal-screen one copy-mode can search — so the highlight can
    follow a sentence that scrolled off the fullscreen view.

    Best-effort: returns True only if the pane actually left the alt-screen
    (i.e. the dump took, so it was really Claude and the keys registered). On
    success records the pane in `_dumped_pane` for `_restore_fullscreen`.
    """
    global _dumped_pane
    # Time for Claude to enter transcript mode before we send `[`; too short and
    # `[` would land in the input box instead. Once-per-response, so a generous
    # default is imperceptible. Tunable via MEDIA_HIGHLIGHT_DUMP_SETTLE_MS.
    settle = int(os.environ.get("MEDIA_HIGHLIGHT_DUMP_SETTLE_MS", "300")) / 1000
    try:
        # Leave tmux copy-mode first: a prior sentence's scroll-and-hold may have
        # left us in it, and then the C-o below is eaten by copy-mode instead of
        # reaching Claude — so a *re-dump* would silently no-op. Harmless no-op
        # when not in copy-mode.
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "cancel"],
                       capture_output=True)
        # Fresh scrollback each dump so repeated dumps don't pile up copies.
        # Safe here: we only dump an alt-screen pane, whose scrollback is empty.
        subprocess.run(["tmux", "clear-history", "-t", pane], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-o"], capture_output=True)
        time.sleep(settle)
        subprocess.run(["tmux", "send-keys", "-t", pane, "["], capture_output=True)
        time.sleep(0.2)
    except Exception:  # noqa: BLE001
        return False
    if _pane_alternate_on(pane):
        return False  # not Claude, or the keys didn't take — nothing dumped
    _dumped_pane = pane
    return True


def _restore_fullscreen() -> None:
    """Undo `_dump_transcript`: leave copy-mode and send Escape so Claude
    re-enters its fullscreen renderer. No-op unless we dumped a pane."""
    global _dumped_pane
    pane = _dumped_pane
    if not pane:
        return
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "cancel"],
                       capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Escape"],
                       capture_output=True)
    except Exception:  # noqa: BLE001
        pass
    _dumped_pane = None


def _highlight_pidfile(pane: str) -> str:
    """Path to the per-pane clear-timer PID file. One file per pane so a new
    highlight can kill the previous sentence's pending clear-timer."""
    _pane_safe = re.sub(r"[^A-Za-z0-9_-]", "_", pane)
    return f"/tmp/media-highlight-clear-{_pane_safe}.pid"


def _kill_pending_clear(pane: str) -> None:
    """Kill any in-flight clear-timer for `pane` and drop its PID file.

    The clear-timer is a detached `sleep …; tmux send-keys -X …` process group
    (see `_tmux_highlight_text`). Without this, turning auto-highlight off — or
    ending playback — leaves the timer alive to fire `cancel`/`clear-selection`
    into the pane a beat later, yanking the view out from under the user."""
    import signal as _signal
    pidfile = _highlight_pidfile(pane)
    try:
        with open(pidfile) as _f:
            _pgid = int(_f.read().strip())
        try:
            os.killpg(_pgid, _signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    except (OSError, ValueError):
        pass
    try:
        os.unlink(pidfile)
    except OSError:
        pass


def _force_cancel_copy_mode(pane: str) -> None:
    """Cancel copy-mode on `pane` and verify it actually left the mode.

    A single `-X cancel` is a no-op if the pane isn't in copy-mode, and can be
    lost to a race with an entering highlight; re-check `#{pane_in_mode}` and
    retry once so we never strand the pane inside tmux copy-mode (which would
    eat the app's own scroll/transcript keys)."""
    for _ in range(2):
        in_mode, _pos = _pane_scroll_pos(pane)
        if not in_mode:
            return
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "cancel"],
                       capture_output=True)


def _tmux_highlight_text(text: str, *, first: bool = False,
                         force: bool = False) -> None:
    """Re-anchor copy-mode in the source pane onto the spoken text.

    Each call jumps to the bottom and searches backward for this sentence,
    so it tracks the right line regardless of prior position — including
    while the user has scrolled up in copy-mode. (We used to no-op when the
    user scrolled away from our last highlight; that rule is gone — the
    keystroke-recency skip in `_run` is the gentler way to stay out of the
    user's way, so highlighting now always follows the spoken text.)

    On an alternate-screen pane (Claude Code & other fullscreen TUIs) this is a
    *transient pulse* — flash then drop out of copy-mode so the app's own scroll
    keys (Claude's Ctrl+O) stay usable; on a normal-screen pane it parks the
    viewport on the sentence (scroll-and-hold). See `transient` below.

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

    # Optional transcript-dump follow-along (MEDIA_HIGHLIGHT_DUMP=1). Print
    # Claude's transcript into scrollback so the scroll-and-hold below can follow
    # every sentence — including ones off the fullscreen view.
    #
    # (Re)dump whenever the pane is currently on the alt-screen: the first
    # sentence needs the initial dump, and *later* sentences need a refresh
    # because Claude re-renders often and each redraw flips the pane back to
    # fullscreen, staling the dumped scrollback (the highlight then "doesn't
    # jump back in"). A pane still on the normal screen (alt=0) means our dump
    # holds — skip the re-dump so we don't churn Ctrl+O/[ on every line.
    #
    # First, though: if the user has started typing into the dumped pane, step
    # aside — restore fullscreen and skip, so their keys reach the input box
    # (the once-at-start keystroke skip in `_run` can't catch a mid-response
    # keystroke) and we don't re-dump on top of them.
    dump = _highlight_dump_enabled()
    if dump:
        _ks = float(os.environ.get("MEDIA_HIGHLIGHT_KEYSTROKE_S", "5"))
        _typing = (_ks > 0 and _pane_recent_keystrokes(pane, _ks)
                   and not _force_highlight_active(pane)
                   and not _popup_open_for(pane))
        if _dumped_pane == pane and not first and _typing:
            _restore_fullscreen()
            return
        if not _typing and _pane_alternate_on(pane):
            _dump_transcript(pane)

    # Transient pulse vs scroll-and-hold. On an alternate-screen pane (Claude
    # Code & other fullscreen TUIs) we flash the sentence then drop out of
    # copy-mode, so the pane returns to the app's live view and its own
    # scroll/transcript keys (Claude's Ctrl+O) aren't blocked — and there's no
    # scrollback to hold onto anyway. On a normal-screen pane we keep the
    # scroll-and-hold follow-along (real scrollback to read along, no fullscreen
    # view to step aside for). A dumped pane (above) is now normal-screen with a
    # transcript to hold, so force scroll-and-hold there. Override with
    # MEDIA_HIGHLIGHT_TRANSIENT=1/0.
    _t_env = os.environ.get("MEDIA_HIGHLIGHT_TRANSIENT")
    if _t_env in ("0", "1"):
        transient = (_t_env == "1")
    elif dump and _dumped_pane == pane:
        transient = False
    else:
        transient = _pane_alternate_on(pane)

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

    pidfile = _highlight_pidfile(pane)

    # Per-pane PID file so each new highlight can kill the previous
    # sentence's pending clear-timer before it races into our selection.
    _kill_pending_clear(pane)

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
            # After the flash window, end the highlight. Transient (alt-screen):
            # `cancel` drops out of copy-mode entirely so the app's own keys work
            # again between pulses. Otherwise `clear-selection` fades the mark but
            # stays in copy-mode, leaving the viewport parked on the sentence.
            #
            # The transient/hold choice is re-checked at *fire* time, not frozen
            # here: if the pane has since flipped to the alternate screen (the
            # user opened Claude's detailed-transcript / Ctrl+O view), we must
            # `cancel` rather than `clear-selection` — otherwise we'd leave the
            # pane parked in tmux copy-mode, eating the app's own scroll keys.
            # start_new_session makes this proc the session leader, so its PID is
            # its pgid; we record it so the next highlight can killpg it cleanly.
            _hold = "0" if transient else "1"
            proc = subprocess.Popen(
                ["sh", "-c",
                 f"sleep {flash_ms / 1000:.2f}; "
                 f'if [ "{_hold}" = "1" ] && '
                 f'[ "$(tmux display-message -p -t {pane} '
                 f"'#{{alternate_on}}' 2>/dev/null)\" != \"1\" ]; then "
                 f"tmux send-keys -t {pane} -X clear-selection 2>/dev/null; "
                 f"else tmux send-keys -t {pane} -X cancel 2>/dev/null; fi"],
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


def _speech_supersede_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "speech-supersede"


# Priority -> numeric rank. Higher rank preempts lower; equal ranks queue.
_PRIO_RANK = {
    Priority.LOW: 0,
    Priority.NORMAL: 10,
    Priority.HIGH: 20,
    Priority.URGENT: 30,
}


def _rank_of(priority: Priority) -> int:
    return _PRIO_RANK.get(priority, _PRIO_RANK[Priority.NORMAL])


def _order_session(source_pane: str, source_session: str) -> str:
    """The identity the playback lock orders a clip within (see
    `_SpeechPlaybackLock`): same identity -> canonical submission order,
    different identity -> priority preemption.

    Prefer the *pane* over the Claude session id. One pane is one conversation's
    worth of speech no matter which producer emitted it, but the producers don't
    agree on a session id: the Stop / PreToolUse hooks tag events with the hook
    payload's session id while the `say` MCP tool tags none at all. Keying on
    that id makes a spoken lead-in and the AskUserQuestion read-out that follows
    it look like two different sessions, so the HIGH-priority question preempts
    the prose it was meant to follow. Both producers live in (or were spawned
    from) the agent's pane and inherit its TMUX_PANE, so the pane is the id they
    do share. Off tmux there is no pane, so fall back to the session id and the
    old behaviour.
    """
    return source_pane or source_session


def _pending_ttl_s() -> float:
    """How long a *pending* (announced-but-not-yet-rendered) waiter entry keeps
    holding its place in its session's queue. A render that takes longer than
    this is assumed broken, and the entry stops blocking its siblings."""
    try:
        return float(os.environ.get("MEDIA_SPEECH_PENDING_TTL_S", "120"))
    except ValueError:
        return 120.0


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
    = NORMAL) decides what happens when *another session* wants the token:

      * HIGH / URGENT  -> preempt: the current speaker steps aside at its next
                          sentence boundary (`should_yield` -> `yield_to_higher`)
                          and resumes when the higher clip finishes.
      * NORMAL         -> queue: wait for the token, never interrupt.
      * LOW            -> skip: if the token is already held, give up rather
                          than queue (ambient announcements aren't worth a wait).

    Priority preemption is scoped to *cross-session* contention only. Within a
    single Claude session, speech does not preempt speech: a session's own clips
    play in submission (canonical) order regardless of priority, so a short
    HIGH notification can't cut ahead of that same session's longer NORMAL
    reply — it queues behind it. "Session" here is the *pane* wherever there is
    one (see `_order_session`), so everything one conversation says — hook
    read-outs and `say` MCP calls alike — is ordered together rather than
    preempting itself. (A clip with neither pane nor session id is treated as
    its own session, so it still preempts by priority as before.) Same-session
    ordering is enforced at admission via a per-clip submission timestamp; a
    clip already speaking otherwise finishes rather than being cut short.

    That timestamp is the caller's *submission* time (`acquire(seq=...)`), NOT
    the moment the token is asked for — rendering happens before acquire() and
    takes longer for a longer reply, so acquire-time ordering would hand the
    queue to whichever sibling was shortest. For the same reason a sibling that
    is still rendering announces itself as a *pending* waiter up front
    (`announce()`), so a later, faster-rendering sibling can see it and defer
    instead of speaking first. Pending entries are same-session-ordering only:
    they never preempt another session and never make a speaker yield, and they
    stop counting after MEDIA_SPEECH_PENDING_TTL_S so a wedged render can't
    mute its session forever.

    The one same-session exception is URGENT — a deliberate "stop and hear this"
    barge-in. An URGENT clip interrupts its session's in-progress clip at the
    next boundary and jumps ahead of its queued siblings. By default the
    interrupted clip resumes afterwards (nothing lost); if the URGENT clip is
    tagged `supersede`, the messages it interrupts/precedes are dropped instead
    (see `should_abort` and the `speech-supersede` marker).

    Waiters announce themselves in a lockless registry — one file per waiter
    under `speech-waiters/`, named `<pid>.<token>` and holding the waiter's
    rank, submission time, and session id — so a holder can tell whether anyone
    with precedence is waiting. Dead-pid
    entries are reaped on scan, so a crashed waiter never wedges anyone, and
    `flock` is released on fd close / process death, so a crashed holder frees
    the token. A genuinely *wedged* holder would hold it indefinitely, so
    non-LOW waiters give up after MEDIA_SPEECH_LOCK_TIMEOUT_S (default 600) and
    play unserialized rather than be lost — but a holder that's *deliberately*
    paused (popup Space) is exempted from that give-up, so a queued reply never
    overtakes it and clobbers its now_playing name. Set MEDIA_SPEECH_SERIALIZE=0
    to disable.

    Rendering is intentionally left outside the lock so sessions still render
    their clips in parallel; only the broker hand-off serializes.
    """

    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._rank: int = _PRIO_RANK[Priority.NORMAL]
        # The Claude session this speech belongs to. Priority preemption only
        # applies *across* sessions; within one session speech never preempts
        # speech and siblings play in submission (canonical) order. Empty means
        # "unknown" and is treated as distinct from every other waiter, so a
        # message with no session id still preempts by priority as before.
        self._session: str = ""
        # Submission time, used to order same-session siblings. Set once in
        # announce()/acquire() and preserved across yield_to_higher() re-takes.
        self._seq: float = 0.0
        # True between announce() and acquire(): we're still rendering, so we
        # hold our place in our session's queue but don't contend with anyone.
        self._pending: bool = False
        # Whether this is a supersede barge-in — one that drops the same-session
        # messages it interrupts/precedes instead of letting them resume.
        self._supersede: bool = False
        # Lazily-created read-only handle for polling holder progress while we
        # wait for the token (see _holder_progress_sig).
        self._progress_store: Optional[StateStore] = None
        # Unique per instance so two locks in one process (e.g. tests) don't
        # collide; the pid prefix lets the waiter scan reap dead entries.
        self._token = f"{os.getpid()}.{uuid.uuid4().hex}"

    # ---- waiter registry -------------------------------------------------

    def _register(self) -> None:
        try:
            d = _speech_wait_dir()
            d.mkdir(parents=True, exist_ok=True)
            # Four lines: rank, submission seq, session id, pending flag. The
            # session keeps its own line so an id containing odd characters
            # can't corrupt the numeric fields; the pending flag is appended
            # after it rather than inserted, so older three-line (and
            # single-line, rank-only) files still parse.
            (d / self._token).write_text(
                f"{self._rank}\n{self._seq!r}\n"
                f"{self._session}\n{1 if self._pending else 0}")
        except OSError:
            pass

    def _unregister(self) -> None:
        try:
            (_speech_wait_dir() / self._token).unlink()
        except OSError:
            pass

    def _same_session(self, other: str) -> bool:
        """True only when both sessions are known and identical. Unknown ("")
        sessions are treated as distinct, so priority preemption still applies
        when a message carries no session id."""
        return bool(self._session) and bool(other) and other == self._session

    def _other_waiters(self) -> "list[tuple[int, float, str, bool]]":
        """(rank, seq, session, pending) for every *other* live waiter. Reaps
        stale (dead-pid) entries as a side effect, and drops pending entries
        whose render has blown past MEDIA_SPEECH_PENDING_TTL_S."""
        out: "list[tuple[int, float, str, bool]]" = []
        pending_floor = time.time() - _pending_ttl_s()
        try:
            entries = list(_speech_wait_dir().iterdir())
        except OSError:
            return out
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
                lines = f.read_text().splitlines()
                rank = int(lines[0].strip())
            except (OSError, ValueError, IndexError):
                continue
            try:
                seq = float(lines[1].strip()) if len(lines) > 1 else 0.0
            except ValueError:
                seq = 0.0
            session = lines[2] if len(lines) > 2 else ""
            pending = len(lines) > 3 and lines[3].strip() == "1"
            if pending and seq < pending_floor:
                continue    # render wedged/abandoned; stop holding the queue
            out.append((rank, seq, session, pending))
        return out

    def _preempting_rank(self) -> int:
        """Highest rank among other live waiters from a *different* session;
        -1 if none. Same-session waiters are excluded — within a session,
        priority never preempts (canonical order wins). Pending (still
        rendering) waiters are excluded too: they hold a place in their own
        session's queue, they don't contend for the token."""
        best = -1
        for rank, _seq, session, pending in self._other_waiters():
            if pending or self._same_session(session):
                continue
            best = max(best, rank)
        return best

    def _earlier_sibling_waiting(self) -> bool:
        """True if a same-session waiter was submitted before me. It must speak
        first to preserve canonical order, regardless of either one's priority.

        Counts *pending* siblings — ones still rendering their clips. A long
        reply takes longer to render than a short one submitted after it, so
        without this a two-sentence follow-up sails past its own session's
        still-rendering predecessor and the pair is heard back to front."""
        for _rank, seq, session, _pending in self._other_waiters():
            if self._same_session(session) and seq < self._seq:
                return True
        return False

    def _is_urgent(self) -> bool:
        return self._rank >= _PRIO_RANK[Priority.URGENT]

    def _urgent_sibling_waiting(self) -> bool:
        """True if a same-session URGENT clip is waiting. URGENT is the one
        same-session case that DOES barge in: a deliberate "stop and hear this"
        that interrupts (and jumps ahead of) our own earlier message rather than
        queueing behind it in canonical order. Everything below URGENT still
        queues within a session. A pending (still rendering) URGENT sibling
        doesn't count — it barges in once it actually wants the token, and
        until then there's nothing to hand over to."""
        for rank, _seq, session, pending in self._other_waiters():
            if pending:
                continue
            if self._same_session(session) and rank >= _PRIO_RANK[Priority.URGENT]:
                return True
        return False

    # ---- token acquisition ----------------------------------------------

    @staticmethod
    def _disabled() -> bool:
        return os.environ.get("MEDIA_SPEECH_SERIALIZE", "1").lower() in ("0", "false", "no")

    def announce(self, priority: Priority = Priority.NORMAL, *,
                 session: str = "", seq: float = 0.0) -> None:
        """Claim a place in this session's queue *before* rendering starts.

        Rendering runs outside the lock (deliberately — sessions render in
        parallel), and a long reply takes longer to render than a short one
        submitted right after it. Without this the short one reaches acquire()
        first, finds nobody waiting, and speaks ahead of its own predecessor.
        Announcing publishes a pending waiter entry so the sibling defers.

        Idempotent-ish and best-effort: acquire() rewrites the same entry as a
        real waiter, release() removes it. Safe to skip calling — ordering then
        degrades to the pre-existing acquire-time behaviour.
        """
        if self._disabled():
            return
        self._rank = _rank_of(priority)
        self._session = session or ""
        self._seq = seq or time.time()
        self._pending = True
        self._register()

    def acquire(self, priority: Priority = Priority.NORMAL, *,
                session: str = "", supersede: bool = False,
                seq: float = 0.0) -> None:
        if self._disabled():
            return
        self._rank = _rank_of(priority)
        self._session = session or ""
        # Stamp submission order for same-session sibling ordering. Prefer the
        # caller's submission time (`seq`) over "now": now is post-render, and
        # ordering by it hands the queue to whichever sibling rendered fastest.
        # Set once — yield_to_higher() re-takes without restamping, so a yielded
        # reply keeps its original place among its session's clips.
        self._seq = seq or self._seq or time.time()
        # No longer merely pending: from here we actually contend for the token.
        self._pending = False
        # A supersede barge-in publishes a per-session marker at its own seq so
        # the same-session clips it interrupts/precedes (all with an earlier
        # seq) can see they've been dropped and abort. Only meaningful with a
        # real session and an URGENT rank (the only same-session barge-in).
        self._supersede = bool(supersede) and bool(self._session) \
            and self._rank >= _PRIO_RANK[Priority.URGENT]
        if self._supersede:
            self._mark_supersede()
        # LOW announcements skip rather than queue when anything's playing.
        self._take(skip_if_busy=self._rank <= _PRIO_RANK[Priority.LOW])

    def _holder_progress_sig(self) -> Optional[tuple]:
        """A cheap signature of the current speaker's progress: (clip uri,
        message start). The shared speech now_playing row is rewritten every
        sentence with the new clip uri, so this changes as long as someone is
        actively speaking — and stays put when the holder is wedged or gone.
        Returns None if it can't be read (treated as "no progress info").
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

    def _holder_paused(self) -> bool:
        """True when the current speech holder is *deliberately* paused (popup
        Space), as opposed to wedged. A paused clip stops advancing its
        now_playing uri, so without this the progress-aware give-up below can't
        tell it apart from a stalled holder and would overtake it — clobbering
        the shared speech now_playing row (and thus the popup/status name) with
        the overtaking session. Authoritative for both local and remote targets:
        reads the broker's live `pause` property from whatever target is
        actually playing. Best-effort — any read failure returns False so a
        genuinely wedged/unreadable holder still times out as before.
        """
        store = self._progress_store
        if store is None:
            try:
                store = self._progress_store = StateStore()
            except Exception:  # noqa: BLE001
                return False
        try:
            np = store.get_now_playing("speech")
        except Exception:  # noqa: BLE001
            return False
        if not np:
            return False
        # Prefer the live pause the playlist/remote loop mirrors into extras
        # (a local DB hit, no bridge round-trip); fall back to reading the
        # broker directly for the local per-sentence loop, which doesn't record
        # it. The uri-mirrored `live_pause` is only trustworthy while it exists.
        ex = np.get("extras") or {}
        if isinstance(ex, dict) and "live_pause" in ex:
            return bool(ex.get("live_pause"))
        try:
            from ..sinks.speech import _socket_for
            from ..sinks import _mpv_ipc as ipc
            sock = _socket_for(Target(name=np.get("target") or "local"))
            return bool(ipc.get_property(sock, "pause"))
        except Exception:  # noqa: BLE001
            return False

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
        # A deliberately-paused holder buys ONE extra grace window (so a queued
        # reply doesn't overtake a pane the user just paused), but not infinite
        # grace: renewing the deadline every poll would let a paused pane block
        # a waiter forever. After the single renewal the give-up fires as it
        # would for a wedged holder, so the waiter proceeds unserialized.
        paused_grace_used = False
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
                    elif self._holder_paused() and not paused_grace_used:
                        # Deliberately paused (popup Space), not wedged: a paused
                        # clip stops advancing its uri, so the sig check above
                        # can't see it's healthy. Grant one extra grace window so
                        # we don't overtake — and don't clobber the paused clip's
                        # now_playing name — the instant the user pauses. EXTEND
                        # the deadline (not reset it): the first poll fires at ~t0
                        # when the deadline is still ~now+timeout, so a reset would
                        # be a no-op — adding a window is what actually buys the
                        # extra grace. Bounded (once): a still-paused holder past
                        # that window is then treated like any stalled one.
                        paused_grace_used = True
                        deadline += timeout
                    if time.monotonic() >= deadline:
                        log.warning("speech lock: holder stalled >%ss; proceeding "
                                    "unserialized", timeout)
                        os.close(fd)
                        return
                    time.sleep(0.2)
                    continue
                # Got the token, but hand it back if someone else should go
                # first, then retry. Reasons to defer: a strictly-higher
                # *other-session* waiter (priority wins admission across
                # sessions, no matter who won the raw flock race), or — unless
                # we ourselves are URGENT — a same-session waiter that outranks
                # our place in the queue: an URGENT sibling (deliberate barge-in
                # jumps to the front) or an *earlier* sibling (siblings otherwise
                # play in submission order, so priority never lets a later clip
                # jump its session's queue). An URGENT clip defers to nobody in
                # its own session; the strictly-earliest non-URGENT sibling never
                # defers to a sibling either, so admission always progresses.
                if (self._preempting_rank() > self._rank
                        or (not self._is_urgent()
                            and (self._urgent_sibling_waiting()
                                 or self._earlier_sibling_waiting()))):
                    # Bound the deferral on the same progress-aware deadline as
                    # the wait above, and for the same reason: whoever we're
                    # standing aside for normally takes the token straight away
                    # (and every clip anyone plays pushes the deadline out), but
                    # if nobody ever does — a sibling wedged between announce()
                    # and acquire(), say — we'd spin here forever rather than
                    # merely speak out of order. Checked before handing the
                    # token back, so on give-up we keep the lock we hold.
                    sig = self._holder_progress_sig()
                    if sig is not None and sig != last_sig:
                        last_sig = sig
                        deadline = time.monotonic() + timeout
                    if time.monotonic() >= deadline:
                        log.warning("speech lock: deferred >%ss with nobody "
                                    "taking the token; proceeding", timeout)
                        self._fd = fd
                        return
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
        """True when we should step aside at the next clip boundary: for a
        strictly higher-priority speaker from a *different* session, or for a
        same-session URGENT barge-in. A same-session clip below URGENT never
        interrupts an in-progress clip — once we're speaking we finish, and
        canonical order is enforced at admission (an earlier sibling wins the
        token next), so an ordinary sibling never cuts a clip short."""
        if self._fd is None:
            return False
        return (self._preempting_rank() > self._rank
                or self._urgent_sibling_waiting())

    def _supersede_path(self) -> Path:
        key = hashlib.sha1(self._session.encode("utf-8")).hexdigest()
        return _speech_supersede_dir() / key

    def _mark_supersede(self) -> None:
        """Publish 'drop everything in this session older than my seq'. Keyed by
        session, so at most one marker per session; a later supersede only ever
        raises the bar (max), never lowers it."""
        try:
            d = _speech_supersede_dir()
            d.mkdir(parents=True, exist_ok=True)
            path = self._supersede_path()
            try:
                prev = float(path.read_text().strip())
            except (OSError, ValueError):
                prev = 0.0
            path.write_text(repr(max(prev, self._seq)))
        except OSError:
            pass

    def should_abort(self) -> bool:
        """True when a same-session supersede has dropped this clip — a later
        URGENT clip in our session was tagged `supersede`, so everything it
        interrupted or was queued ahead of (every clip with an earlier seq)
        should stop rather than play/resume. The superseding clip itself has
        seq == marker, so it never aborts itself; clips submitted *after* it
        (larger seq) are unaffected."""
        if self._disabled() or not self._session:
            return False
        try:
            marker = float(self._supersede_path().read_text().strip())
        except (OSError, ValueError):
            return False
        return marker > self._seq

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


def _tmux_window_for_pane(pane: str) -> str:
    """The conversation title for a pane — its tmux window name, captured now
    while the pane is alive.

    Persisted into the speech extras so the popup / status bar can name the
    speaker even when its pane can't be resolved at *display* time: a pane
    renumbered by a tmux-resurrect restore, closed since, or — for a rooms hub
    — living on a *different host* entirely. Mirrors the cli `_subject_label`
    preference: the window name (which tracks the stable Claude conversation
    title) over the transient, spinner-prefixed pane title; falls back to a
    spinner-stripped pane title only when the window has no usable name.
    """
    if not pane or "#{" in pane:
        return ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{window_name}\t#{pane_title}"],
            capture_output=True, text=True, timeout=2)
    except Exception:  # noqa: BLE001
        return ""
    if r.returncode != 0:
        return ""
    window_name, _, pane_title = r.stdout.strip().partition("\t")
    label = window_name.strip()
    if not label or label in {"zsh", "bash", "sh", "fish"}:
        label = re.sub(r"^[⠀-⣿]\s*", "", pane_title.strip())
    return label


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

    def poll(self, muted: Optional[bool] = None) -> None:
        if muted is None:
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


def _remote_playlist(target: Target) -> bool:
    """Use the autonomous gapless-playlist path for a *remote* speech target.

    A tcp:// speech socket means the player (the phone's mpv) is on another host
    reached over a bridge. Driving it sentence-by-sentence over that bridge is
    fragile (a dropped poll stalls or cuts a clip), so for remote targets we load
    the whole response as a gapless playlist and let the player run it itself,
    just following playlist-pos for the popup. Local/rooms (unix-socket) targets
    keep the per-sentence loop, which gives finer control on a reliable socket.
    """
    from ..sinks.speech import _socket_for
    return str(_socket_for(target)).startswith("tcp://")


def _wait_and_claim_broker(sink: "SinkSpeech", target: Target) -> None:
    """Cross-host serialization for a *shared remote* (tcp://) broker.

    The playback flock only serializes this host; the phone broker is driven by
    every host. Before we stop+clear it with our playlist, claim an owner token
    that lives on the broker itself (mpv user-data) and wait out any other host
    that actively holds it. This mirrors the flock's progress-aware give-up: a
    healthy remote holder keeps refreshing its claim's deadline, so we keep
    waiting; a crashed/stalled one's claim expires, so we take over. No-op for
    local/rooms targets (the flock already covers them) and best-effort — the
    token machinery must never wedge a reply, so any trouble just proceeds.
    """
    if not _remote_playlist(target):
        return
    claim = getattr(sink, "claim_broker", None)
    if claim is None:
        return
    timeout = float(os.environ.get("MEDIA_SPEECH_LOCK_TIMEOUT_S", "600"))
    deadline = time.monotonic() + timeout
    last_seen: Optional[float] = None
    while True:
        try:
            info = sink.active_other_owner(target)
        except Exception:  # noqa: BLE001
            info = None
        if info is None:
            try:
                if claim(target):
                    return
            except Exception:  # noqa: BLE001
                return
        else:
            # Another host holds it; while its claim's deadline keeps advancing
            # it's alive, so reset our give-up and keep waiting rather than
            # clobber a healthy long reply on the other machine.
            try:
                od = float(info.get("deadline", 0))
            except (TypeError, ValueError):
                od = 0.0
            if od != last_seen:
                last_seen = od
                deadline = time.monotonic() + timeout
        if time.monotonic() >= deadline:
            who = info.get("owner") if isinstance(info, dict) else "?"
            log.warning("intake: remote broker held by %s and not advancing "
                        ">%ss; proceeding", who, timeout)
            return
        time.sleep(0.3)


def _submit_remote_say(text: str, cmd: str, coordinator: Coordinator,
                       state: StateStore, event: Event) -> Optional[int]:
    """Render a reply on a remote low-latency hub instead of locally.

    Used when ``MEDIA_REMOTE_SAY_CMD`` is set (e.g. red5, whose rooms listen to
    snap-mel in Melbourne). The whole reply text is piped to the remote renderer
    over **stdin** — so no shell on the far side reinterprets quotes/`$`/etc. —
    and the call blocks until the remote finishes, so ``before_speech`` /
    ``after_speech`` bracket the audio and music ducks for its full duration.

    Serialized by the same cross-process speech lock as local playback, so two
    sessions can't render into the hub's fifo at once. Best-effort: a remote
    hiccup is logged, never raised, and the duck is always restored.
    """
    import subprocess

    timeout = float(os.environ.get("MEDIA_REMOTE_SAY_TIMEOUT", "180"))
    lock = _SpeechPlaybackLock()
    lock.acquire(event.priority,
                 session=(event.metadata or {}).get("session") or "",
                 supersede=bool((event.metadata or {}).get("supersede")))
    # Superseded before we started: skip this whole-reply remote render. (The
    # remote say is one blocking pipe, so this is the only place it can drop —
    # once handed off it can't be cut mid-utterance like the clip loops.)
    if lock.should_abort():
        lock.release()
        return None
    try:
        coordinator.before_speech()
        try:
            subprocess.run(cmd, shell=True, input=text.encode(),
                           timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001 — remote render must never crash the hook
            log.warning("intake: remote-say failed: %s", e)
            try:
                state.log_error("intake", "remote-say failed",
                                extras={"detail": str(e),
                                        "source": event.source.value})
            except Exception:  # noqa: BLE001
                pass
        finally:
            coordinator.after_speech()
    finally:
        lock.release()
    return None


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

    # Remote-say bridge: on a headless feeder host (e.g. red5) whose rooms now
    # listen to a remote low-latency hub (snap-mel, in Melbourne), render the
    # reply *there* instead of locally — the hub renders the text to its own
    # Snapcast fifo. The coordinator still ducks from here (it drives the rooms
    # snapserver over the tailnet via MEDIA_SNAP_JSONRPC_HOST), so music dips
    # under speech as before. Env-gated: unset elsewhere ⇒ the local render+play
    # path below is unchanged.
    remote_say = os.environ.get("MEDIA_REMOTE_SAY_CMD")
    if remote_say:
        return _submit_remote_say(text, remote_say, coordinator, state, event)

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
    order_session = _order_session(source_pane, source_session)
    # Claim this reply's place in its session's speech queue *now*, before the
    # renders below: a shorter sibling submitted a moment later would otherwise
    # finish rendering first, find the queue empty, and speak ahead of us.
    # Created here, acquired for real once the clips exist (and released on
    # every path out, including muted / render-failed).
    playback_lock = _SpeechPlaybackLock()
    playback_lock.announce(event.priority, session=order_session,
                           seq=started_at)
    # The tmux session that owns the source pane, captured now while the pane
    # is guaranteed alive. Persisted so the popup's < / > can scope history
    # traversal to "this tmux session's clips" without resolving a (possibly
    # since-closed) pane id back to its session at browse time.
    source_tmux_session = _tmux_session_for_pane(source_pane)
    # The conversation title (window name) of the source pane, captured now so
    # the popup / status bar can name the speaker even when source_pane can't be
    # resolved at display time (renumbered by a restore, closed, or remote host).
    source_window = _tmux_window_for_pane(source_pane)

    # Durable per-pane / per-session mute (popup `M` / `media mute-pane`): a
    # muted pane still renders its clips and records a replayable history row,
    # but is never played through the broker and never ducks music. Decided
    # once, up front, so we also skip the remote pre-pause below.
    muted = state.resolve_mute(source_pane, source_tmux_session)
    if muted:
        # Nothing will be played, so give the queue slot announced above back
        # immediately rather than making this session's next reply wait on a
        # render it will never hear.
        playback_lock.release()

    fallback_info: dict = {}
    _fallback_lock = threading.Lock()

    def _on_fallback(failed_engine: str, err: str) -> None:
        short = err.strip().splitlines()[0] if err else "no detail"
        fb = os.environ.get("MEDIA_RENDER_FALLBACK_ENGINE") or "edge"
        kind = "render-fallback"
        if "insufficient_quota" in err:
            kind = "render-quota"
        log.warning("intake: %s engine failed (%s); falling back to %s",
                    failed_engine, short, fb)
        state.log_error("intake",
                        f"render {failed_engine} failed, fell back to {fb}",
                        extras={"kind": kind, "engine": failed_engine,
                                "fallback_engine": fb, "detail": short[:300],
                                "source": event.source.value})
        with _fallback_lock:
            fallback_info.update({
                "from_engine": failed_engine,
                "fallback_engine": fb,
                "kind": kind,
                "detail": short[:300],
            })
        if kind == "render-quota":
            title = f"agent-media: {failed_engine} quota exhausted"
            body = f"Falling back to {fb} for now."
        else:
            title = f"agent-media: {failed_engine} render failed"
            body = f"Falling back to {fb}. {short[:120]}"
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
        playback_lock.release()   # nothing to say; stop holding our queue slot
        return None

    # Compute per-clip offsets for a single spanning progress bar. ffprobe is a
    # subprocess per clip; probe them in parallel so a multi-sentence reply
    # doesn't add ~0.2s × N to time-to-first-audio.
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(clip_data))) as _dpool:
        durations = list(_dpool.map(_clip_duration, [p for _, p in clip_data]))
    total_duration_s = sum(durations)

    # Delay the highlight so it fires when the audio is actually *heard*, not
    # when mpv reports idle. For Snapcast rooms that's the buffer drain
    # (MEDIA_SNAPCAST_LATENCY_MS). For a remote-played target (Grade B: phone
    # mpv over the bridge, clips pre-fetched local) it's just the bridge
    # loadfile + mpv start — much smaller — so a per-target override wins:
    # MEDIA_SPEECH_PLAYOUT_MS_<TARGET>.
    _playout_key = f"MEDIA_SPEECH_PLAYOUT_MS_{target.name.upper().replace('-', '_')}"
    _highlight_delay_s = float(
        os.environ.get(_playout_key)
        or os.environ.get("MEDIA_SNAPCAST_LATENCY_MS", "500")) / 1000.0

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
        # while we're still queued behind another speaker. `seq=started_at` is
        # what keeps same-session order canonical — see announce() above.
        playback_lock.acquire(
            event.priority, session=order_session,
            supersede=bool((event.metadata or {}).get("supersede")),
            seq=started_at)
        # Superseded before we started (a later URGENT in this session dropped
        # us): hand the token straight back and skip playback entirely — no
        # broker claim, no music duck, no history row.
        if playback_lock.should_abort():
            playback_lock.release()
            return None
        # Cross-host: also claim the shared remote broker so another machine's
        # reply can't stop+clear our still-playing playlist. Waits out a healthy
        # remote holder, takes over an expired one. No-op for local/rooms.
        _wait_and_claim_broker(sink, target)
        # Grade B: push all clips to the remote player's local dir up front
        # (no-op for local/rooms), so each play below is a local loadfile —
        # no per-sentence network fetch to stall a long reply.
        # Defensive getattr: prefetch is a newer Sink method; a minimal sink
        # (or test double) without it just skips the pre-fetch (no-op anyway for
        # non-remote targets).
        getattr(sink, "prefetch", lambda *a, **k: None)(
            [p for _, p in clip_data], target)
        played_any = False
        n = len(clip_data)
        highlighter = _HighlightScheduler(_highlight_delay_s, do_highlight)
        # Drop any stale jump request left by a previous response.
        _nav_flag_path(target).unlink(missing_ok=True)
        try:
            coordinator.before_speech()
            mute_watcher = _MuteDuckWatcher(sink, target, coordinator)
            # Shared per-clip marker — drives the popup (status bar, current
            # sentence, skip map). Identical for both playback paths below.
            def _mark(idx: int, live: Optional[dict] = None) -> None:
                sentence_i, clip_i = clip_data[idx]
                extras = {"text": text, "source": event.source.value,
                          "engine": engine, "voice": voice,
                          "clip_offset_s": offsets[idx],
                          "total_duration_s": total_duration_s,
                          "source_pane": source_pane,
                          "source_session": source_session,
                          "source_tmux_session": source_tmux_session,
                          "source_window": source_window,
                          "current_sentence": sentence_i,
                          "current_sentence_idx": idx,
                          "clip_paragraph_idx": clip_para,
                          "clip_sentences": _clip_sentences,
                          "writer_pid": os.getpid()}
                # Figure-bearing message ([[visual:]]/[[reveal:]]): surfaces as
                # the ▣ indicator in the status bar / popup / canvas badge.
                if event.metadata.get("visual"):
                    extras["visual"] = event.metadata["visual"]
                if live is not None:
                    # Mirror the *remote* player's live state into now_playing so a
                    # status read (popup redraw) is a local DB hit, not a ~600ms
                    # bridge round-trip to the phone. Timeline position = the start
                    # of this clip plus how far mpv is into it.
                    tp = live.get("time-pos")
                    extras["live_pos_s"] = (offsets[idx] + tp
                                            if tp is not None else offsets[idx])
                    extras["live_pause"] = bool(live.get("pause"))
                    extras["live_speed"] = live.get("speed") or 1.0
                    extras["live_mute"] = bool(live.get("mute"))
                state.set_now_playing(
                    "speech", uri=str(clip_i), started_at=started_at,
                    target=target.name, extras=extras)

            if _remote_playlist(target):
                # Autonomous gapless playlist: load every clip and let the remote
                # player advance through them itself — no per-sentence drive to
                # stall, and gapless (no inter-sentence gap). We only *follow*
                # playlist-pos to move the popup/highlight; a dropped poll lags
                # the follow-along, it never cuts the audio.
                try:
                    sink.play_playlist([p for _, p in clip_data], target)
                    played_any = True
                except Exception as e:  # noqa: BLE001
                    log.warning("intake: play_playlist failed: %s", e)
                    state.log_error("intake", "play_playlist failed",
                                    extras={"detail": str(e),
                                            "source": event.source.value})
                # Seed now_playing immediately so a status read (popup) shows the
                # response as playing right away, before the first bridge snapshot
                # lands (~0.6s) to fill in the live position.
                if played_any:
                    _mark(0)
                i = -1
                nav_jump = False
                misses = 0
                last_ms = -1
                stall = 0
                # Hold the playback token until the reply's audio is really done,
                # not merely until we can still *see* the player. Losing the
                # follow-along (a flaky bridge trips the misses/stall guards below)
                # must NOT release the token early: a queued equal-priority reply
                # would then grab it and play_playlist stop+clears our still-
                # playing audio — the "long reply gets cut off and never comes
                # back" bug. We know the whole reply's duration, so on a blind
                # bail we keep the token until that's plausibly elapsed (the
                # blind-hold tail after this loop). `finished` is set only when we
                # positively observe the end (idle, or a skip past the last clip).
                finished = False
                hard_deadline = time.monotonic() + (total_duration_s or 0.0) + 5.0
                last_broker_refresh = time.monotonic()
                while played_any:
                    # Superseded by a later URGENT in this session — drop the
                    # rest of the playlist instead of yielding-and-resuming.
                    if playback_lock.should_abort():
                        highlighter.cancel_pending()
                        sink.stop(target)
                        finished = True
                        break
                    # Step aside for a higher-priority speaker (e.g. a
                    # notification) waiting on the token, then resume this reply —
                    # the remote counterpart of the per-clip path's yield. The
                    # phone plays the playlist autonomously, so to hand the broker
                    # over cleanly we stop it, drop our broker claim (else a
                    # same-host higher speaker would wait out our claim's TTL),
                    # yield the flock until the higher clip finishes, then reload
                    # the full playlist and jump back to where we were — reloading
                    # the whole list (not just the tail) keeps playlist-pos mapping
                    # 1:1 to the sentence index for the popup/highlight.
                    if playback_lock.should_yield():
                        resume_i = i if 0 <= i < n else 0
                        highlighter.cancel_pending()
                        sink.stop(target)
                        getattr(sink, "release_broker",
                                lambda *a, **k: None)(target)
                        playback_lock.yield_to_higher()
                        _wait_and_claim_broker(sink, target)
                        try:
                            sink.play_playlist([p for _, p in clip_data], target)
                            if resume_i > 0:
                                sink.set_playlist_pos(resume_i, target)
                        except Exception as e:  # noqa: BLE001
                            log.warning("intake: resume after yield failed: %s", e)
                        # Re-arm follow state; recompute the blind-hold deadline for
                        # only the audio that's left (wall-clock advanced while the
                        # higher-priority reply played).
                        i = -1
                        nav_jump = True
                        misses = 0
                        last_ms = -1
                        stall = 0
                        remaining = max(0.0, total_duration_s - offsets[resume_i])
                        hard_deadline = time.monotonic() + remaining + 5.0
                        last_broker_refresh = time.monotonic()
                        _mark(resume_i)
                        continue
                    # Keep our cross-host broker claim alive while we play so
                    # another machine's reply doesn't take it for a stalled one.
                    if time.monotonic() - last_broker_refresh > 5.0:
                        getattr(sink, "refresh_broker",
                                lambda *a, **k: None)(target)
                        last_broker_refresh = time.monotonic()
                    # One batched snapshot per tick (pos/idle/pause/time) instead
                    # of four separate ~600ms bridge hops — keeps the follow-along
                    # tight rather than lagging the audio by seconds.
                    snap = sink.snapshot(target)
                    if not snap:
                        misses += 1
                        if misses > 50:        # ~5s fully unreadable → bail
                            break
                        time.sleep(0.1)
                        continue
                    misses = 0
                    mute_watcher.poll(snap.get("mute"))  # from the same snapshot
                    nav = _read_nav_request(target)
                    if nav is not None:
                        if nav >= n:
                            highlighter.cancel_pending()
                            sink.stop(target)
                            finished = True   # skip past last clip = intentional end
                            break
                        sink.set_playlist_pos(max(0, nav), target)
                        nav_jump = True
                        stall = 0
                    if snap.get("pause"):
                        if 0 <= i < n:
                            _mark(i, live=snap)  # reflect the pause in now_playing
                        stall = 0
                        time.sleep(0.1)
                        continue
                    if snap.get("idle-active"):
                        finished = True
                        break  # playlist finished
                    pos = snap.get("playlist-pos")
                    if pos is None or pos < 0:
                        time.sleep(0.1)   # loaded but not on an entry yet
                        continue
                    if pos != i and 0 <= pos < n:
                        i = pos
                        highlighter.show(clip_data[i][0],
                                         first=(i == 0), force=nav_jump)
                        nav_jump = False
                        stall = 0
                    if 0 <= i < n:
                        # Every tick, not just on sentence change: keep the mirrored
                        # live position/pause/speed/mute fresh so the popup's redraw
                        # reads a current local snapshot.
                        _mark(i, live=snap)
                    # Stall guard: if playback time isn't advancing while we're
                    # not paused (a wedged clip, or another process clobbering the
                    # shared broker), bail so a response can never hang. A gapless
                    # clip boundary resets time-pos, which counts as progress.
                    ms = snap.get("time-pos")
                    if ms is not None and ms != last_ms:
                        last_ms = ms
                        stall = 0
                    else:
                        stall += 1
                        if stall > 80:         # ~8s with no progress → give up
                            log.warning("intake: playlist stalled; ending follow")
                            break
                    time.sleep(0.1)
                # Blind-hold: the follow loop stopped but we never positively saw
                # the playlist end (the bridge went unreadable / stalled). The
                # phone is most likely still playing our clips, so keep the token
                # until we can confirm it's idle again or the reply's own duration
                # has elapsed — otherwise a queued reply clobbers the remaining
                # audio. Trust only a *readable* idle: snapshot() returns None on a
                # dead bridge, and sink.idle() reports idle on IPC error, so either
                # alone would release us straight back into the clobber.
                if played_any and not finished:
                    log.info("intake: lost follow-along; holding speech token "
                             "until audio completes")
                    while time.monotonic() < hard_deadline:
                        snap = sink.snapshot(target)
                        if snap and snap.get("idle-active"):
                            break
                        time.sleep(0.5)
            else:
                i = 0
                nav_jump = False  # True when this clip was reached via a popup skip
                while 0 <= i < n:
                    # Superseded by a later URGENT in this session — drop the
                    # remaining sentences instead of yielding-and-resuming.
                    if playback_lock.should_abort():
                        break
                    # Step aside between sentences if a higher-priority speaker
                    # (e.g. a notification) is waiting; resume it once that's done.
                    if playback_lock.should_yield():
                        playback_lock.yield_to_higher()
                    sentence, clip_path = clip_data[i]
                    _mark(i)
                    try:
                        # Only the first sentence resets a lingering pause/mute;
                        # later sentences preserve a pause made mid-response.
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
                    # Highlight is deferred by the Snapcast buffer so it lands with
                    # the audio (a manual jump forces it on immediately).
                    highlighter.show(sentence, first=(i == 0), force=nav_jump)
                    nav = _wait_for_clip(sink, target, on_poll=mute_watcher.poll)
                    if nav is None:
                        i += 1
                        nav_jump = False
                    else:
                        # Popup sentence/paragraph jump; past the last clip = end.
                        if nav >= n:
                            highlighter.cancel_pending()
                            break
                        i = max(0, nav)
                        nav_jump = True
        finally:
            highlighter.drain()
            _restore_fullscreen()   # no-op unless MEDIA_HIGHLIGHT_DUMP dumped
            coordinator.after_speech()
            state.clear_now_playing("speech")
            # Drop the cross-host broker claim before the flock so the next host
            # (and the next local waiter) can take over immediately. No-op local.
            getattr(sink, "release_broker", lambda *a, **k: None)(target)
            playback_lock.release()

        if not played_any:
            return None

    extras = {"engine": engine, "voice": voice,
              "priority": event.priority.value,
              "source_pane": source_pane,
              "source_session": source_session,
              "source_tmux_session": source_tmux_session,
              "source_window": source_window,
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

    # Remote-say bridge: on a headless feeder host (e.g. red5) whose rooms now
    # listen to a remote low-latency hub (snap-mel, in Melbourne), render the
    # reply *there* instead of locally — the hub renders the text to its own
    # Snapcast fifo. The coordinator still ducks from here (it drives the rooms
    # snapserver over the tailnet via MEDIA_SNAP_JSONRPC_HOST), so music dips
    # under speech as before. Env-gated: unset elsewhere ⇒ the local render+play
    # path below is unchanged.
    remote_say = os.environ.get("MEDIA_REMOTE_SAY_CMD")
    if remote_say:
        return _submit_remote_say(" ".join(sentences), remote_say, coordinator, state, event)

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
    order_session = _order_session(source_pane, source_session)
    source_tmux_session = _tmux_session_for_pane(source_pane)
    source_window = _tmux_window_for_pane(source_pane)
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
        fb = os.environ.get("MEDIA_RENDER_FALLBACK_ENGINE") or "edge"
        kind = "render-fallback"
        if "insufficient_quota" in err:
            kind = "render-quota"
        log.warning("intake-stream: %s engine failed (%s); falling back to %s",
                    failed_engine, short, fb)
        state.log_error("intake",
                        f"render {failed_engine} failed, fell back to {fb}",
                        extras={"kind": kind, "engine": failed_engine,
                                "fallback_engine": fb, "detail": short[:300],
                                "source": event.source.value})
        with _fallback_lock:
            fallback_info.update({"from_engine": failed_engine,
                                  "fallback_engine": fb, "kind": kind,
                                  "detail": short[:300]})
        if kind == "render-quota":
            title = f"agent-media: {failed_engine} quota exhausted"
            body = f"Falling back to {fb} for now."
        else:
            title = f"agent-media: {failed_engine} render failed"
            body = f"Falling back to {fb}. {short[:120]}"
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
        playback_lock.acquire(
            event.priority, session=order_session,
            supersede=bool((event.metadata or {}).get("supersede")),
            seq=started_at)
        i = 0
        nav_jump = False
        try:
            while True:
                # Superseded by a later URGENT in this session — drop the rest
                # (whether or not we've started) instead of resuming.
                if playback_lock.should_abort():
                    break
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
                            "source_window": source_window,
                            "current_sentence": sentence,
                            "current_sentence_idx": i,
                            "clip_sentences": known,
                            "streaming": True,
                            "writer_pid": os.getpid()})
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
            _restore_fullscreen()   # no-op unless MEDIA_HIGHLIGHT_DUMP dumped
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
              "source_window": source_window,
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
