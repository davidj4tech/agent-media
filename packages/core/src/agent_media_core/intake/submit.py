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
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .._notify import notify
from ..render import render_text
from ..route import Coordinator
from ..sinks.speech import SinkSpeech
from ..state import StateStore
from ..types import Event, Target


log = logging.getLogger(__name__)


_DEFAULT_ENGINE = "edge"
_DEFAULT_VOICE: Optional[str] = None


def _resolve_engine(event: Event) -> str:
    return (event.engine
            or os.environ.get("MEDIA_RENDER_ENGINE")
            or os.environ.get("CLAUDE_TTS_ENGINE")  # legacy
            or _DEFAULT_ENGINE)


def _resolve_voice(event: Event) -> Optional[str]:
    return (event.voice
            or os.environ.get("MEDIA_RENDER_VOICE")
            or os.environ.get("CLAUDE_TTS_VOICE")  # legacy
            or _DEFAULT_VOICE)


def _ext_for(engine: str) -> str:
    """qwen / realtime emit WAV, others MP3."""
    return "wav" if engine in ("qwen", "realtime") else "mp3"


def _split_sentences(text: str) -> list[str]:
    """Split text into sentence-level chunks for progressive TTS + highlight.

    Splits on paragraph breaks first, then on sentence-ending punctuation
    within each paragraph. Common abbreviations are masked so they don't
    produce spurious splits.
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
    raw: list[str] = []
    for para in paragraphs:
        raw.extend(_sentences_in(para))

    # Merge very short fragments (< 20 chars) into the preceding sentence
    # so standalone words like "Yes." or "OK." don't become solo clips.
    result: list[str] = []
    for part in raw:
        if len(part) < 20 and result:
            result[-1] += ' ' + part
        else:
            result.append(part)
    return result or [text.strip()]


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


def _tmux_highlight_text(text: str, *, first: bool = False,
                         force: bool = False) -> None:
    """Re-anchor copy-mode in the source pane onto the spoken text.

    Each call jumps to the bottom and searches backward for this sentence,
    so it tracks the right line regardless of prior position. But it leaves
    the user's scroll alone: if the pane is in copy-mode and the viewport
    has moved since our last highlight (the user scrolled up to read), this
    no-ops — until the user returns to that position or exits copy-mode, at
    which point following resumes. `force=True` (the popup's `v` toggle)
    always repositions, since the user just asked for it.

    Off by default — opt-in via the popup's `v` toggle (which writes to
    `$XDG_STATE_HOME/agent-media/auto-highlight`). `MEDIA_AUTO_HIGHLIGHT=1`
    in env can override on a per-host basis. `first` is accepted for call-site
    compatibility but no longer changes anchoring (every call re-anchors).
    """
    if not os.environ.get("TMUX"):
        return
    if not _is_auto_highlight_enabled():
        return
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return

    # Trim to last word boundary within 50 chars so the snippet fits on
    # a single visual line — tmux search won't match across line wraps.
    def _trim_to_word(s: str, limit: int = 50) -> str:
        if len(s) <= limit:
            return s
        cut = s[:limit].rfind(" ")
        return s[:cut] if cut > 15 else s[:limit]

    snippet = ""
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 20:
            snippet = _trim_to_word(line)
            break
    if not snippet:
        snippet = _trim_to_word(text.replace("\n", " ").strip())
    if not snippet:
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
    # Tracks the scroll_position our last highlight left the pane at, so we
    # can tell whether the user has since scrolled away.
    posfile = f"/tmp/media-highlight-pos-{_pane_safe}"

    # Respect a manual scroll: if the pane is in copy-mode at a position other
    # than where we last left it, the user scrolled up to read — leave their
    # view untouched. When they return to that position (or drop out of
    # copy-mode, putting them back at the live bottom), following resumes.
    # The first sentence of a response (and an explicit `v` toggle) always
    # re-anchors, so we never get permanently stuck skipping.
    if not force and not first:
        in_mode, pos = _pane_scroll_pos(pane)
        if in_mode:
            try:
                with open(posfile) as _f:
                    saved = _f.read().strip()
            except OSError:
                saved = None
            if pos != saved:
                return

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
        # If the search matched nothing, the cursor stays put at the bottom.
        # Don't paint a stray selection there (the "yellow block at bottom
        # left"); leave copy-mode so the next sentence retries cleanly.
        if _cursor_sig(pane) == _before:
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
        # Record where we landed so the next sentence can tell whether the
        # user has scrolled away from it.
        _, _new_pos = _pane_scroll_pos(pane)
        try:
            with open(posfile, "w") as _f:
                _f.write(_new_pos)
        except OSError:
            pass
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


def _wait_for_clip(sink: SinkSpeech, target: Target) -> None:
    """Wait for sink-speech to start then finish the current clip.

    Requires two consecutive idle readings before declaring done — a
    single True from sink.idle() can be a transient IPC error mid-play
    (the method returns True on MpvIpcError), which would otherwise cause
    the next clip to cut the current one short.
    """
    for _ in range(20):
        if not sink.idle(target):
            break
        time.sleep(0.05)
    idle_streak = 0
    elapsed = 0
    while elapsed < 1200:
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
    voice = _resolve_voice(event)
    ext = _ext_for(engine)

    audio_dir = _audio_dir()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    started_at = time.time()

    # The tmux pane that produced this speech (the Claude Code TTS hook runs
    # inside the agent's pane, so TMUX_PANE points at it). Persisted into
    # now_playing/history so the popup can show *which* pane is currently
    # talking, rather than the pane that happens to be active.
    source_pane = (event.metadata or {}).get("pane") or os.environ.get("TMUX_PANE", "")

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
    # (~4.8s) overlaps with sentence rendering below.
    coordinator.pre_pause_remote()

    sentences = _split_sentences(text)

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

    # Phase 1: resolve all render futures and collect clip durations.
    # Parallel renders are mostly done by now; future.result() is instant
    # for finished ones and waits briefly for the last stragglers.
    clip_data: list[tuple[str, Path]] = []  # (sentence, clip_path)
    for sentence, outfile, future in zip(sentences, outfiles, futures):
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

    coordinator.before_speech()
    played_any = False
    try:
        offset_s = 0.0
        for i, ((sentence, clip_path), dur) in enumerate(zip(clip_data, durations)):
            # Update now_playing so cmd_status can show a response-wide bar
            # and `media current-sentence` can return the active sentence.
            state.set_now_playing(
                "speech", uri=str(clip_path), started_at=started_at,
                target=target.name,
                extras={"text": text, "source": event.source.value,
                        "engine": engine, "voice": voice,
                        "clip_offset_s": offset_s,
                        "total_duration_s": total_duration_s,
                        "source_pane": source_pane,
                        "current_sentence": sentence,
                        "current_sentence_idx": i})
            if do_highlight:
                _tmux_highlight_text(sentence, first=(i == 0))
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
                offset_s += dur
                continue
            _wait_for_clip(sink, target)
            offset_s += dur
            # Let Snapcast drain before firing the next highlight, so the
            # visual matches what the listener is hearing.
            if _highlight_delay_s > 0:
                time.sleep(_highlight_delay_s)
    finally:
        coordinator.after_speech()
        state.clear_now_playing("speech")

    if not played_any:
        return None

    extras = {"engine": engine, "voice": voice,
              "priority": event.priority.value,
              "source_pane": source_pane,
              "clip_uris": [str(p) for _, p in clip_data],
          "clip_sentences": [s for s, _ in clip_data],
          "clip_durations_s": durations,
              **(event.metadata or {})}
    if fallback_info:
        extras["fallback"] = fallback_info
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
