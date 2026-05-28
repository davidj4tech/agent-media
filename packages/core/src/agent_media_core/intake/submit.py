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


def _tmux_highlight_text(text: str, *, first: bool = False) -> None:  # noqa: ARG001 (first unused)
    """Enter copy-mode in the source pane and jump to the spoken text.

    Called once per sentence. Each call is independent — cancel + re-enter
    + history-bottom + search-backward every time so that any overshoot in
    the previous selection doesn't cascade into the next sentence's search.

    Enabled when TMUX_PANE is set and MEDIA_AUTO_HIGHLIGHT != "0".
    """
    if not os.environ.get("TMUX"):
        return
    if os.environ.get("MEDIA_AUTO_HIGHLIGHT", "1") == "0":
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

    snippet = re.sub(r'([][(){}^$.*+?|\\])', r'\\\1', snippet)
    select_len = max(0, len(text.strip()) + 3)

    # Copycat trick: after jumping to the match, move cursor down by padding
    # lines then back up — viewport follows, centering the match on screen.
    # Cap at min(pane_height//2, 10) so we never overshoot into old history.
    try:
        _ph = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_height}"],
            capture_output=True, text=True).stdout.strip()
        _padding = max(1, min(int(_ph) // 2, 10)) if _ph.isdigit() else 5
    except Exception:  # noqa: BLE001
        _padding = 5

    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "cancel"],
                       capture_output=True)
        subprocess.run(["tmux", "copy-mode", "-t", pane],
                       capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "history-bottom"],
                       capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X",
                        "search-backward", snippet],
                       capture_output=True)
        # Push viewport down so the match isn't pinned to the top edge.
        subprocess.run(["tmux", "send-keys", "-t", pane,
                        "-X", "-N", str(_padding), "cursor-down"],
                       capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", pane,
                        "-X", "-N", str(_padding), "cursor-up"],
                       capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X",
                        "begin-selection"],
                       capture_output=True)
        if select_len > 0:
            subprocess.run(["tmux", "send-keys", "-t", pane,
                            "-X", "-N", str(select_len), "cursor-right"],
                           capture_output=True)
            # cursor-right dragged the viewport to the end of the selection;
            # cursor-left brings it back to the start while keeping the selection.
            subprocess.run(["tmux", "send-keys", "-t", pane,
                            "-X", "-N", str(select_len), "cursor-left"],
                           capture_output=True)
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
    for _ in range(1200):
        if sink.idle(target):
            idle_streak += 1
            if idle_streak >= 3:
                break
        else:
            idle_streak = 0
        time.sleep(0.1)


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
            # Update now_playing so cmd_status can show a response-wide bar.
            state.set_now_playing(
                "speech", uri=str(clip_path), started_at=started_at,
                target=target.name,
                extras={"text": text, "source": event.source.value,
                        "engine": engine, "voice": voice,
                        "clip_offset_s": offset_s,
                        "total_duration_s": total_duration_s})
            if do_highlight:
                _tmux_highlight_text(sentence)
            try:
                sink.play(str(clip_path), target)
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
