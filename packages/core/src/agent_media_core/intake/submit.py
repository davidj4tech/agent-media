"""Submit: render text → play through sink-speech.

The intake "happy path" for any event source. Bypasses the legacy
drop-dir + watcher chain — render is in-process, playback is dispatched
straight to sink-speech via the route Coordinator.

Adapters (`hook_claude_code`, `cli`, future `matrix` / `ha-stt`) all
land here. The shape is intentionally narrow: take a populated
`Event`, hand back a history row id.
"""

from __future__ import annotations

import logging
import os
import tempfile
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


def submit_event(event: Event,
                 *,
                 state: Optional[StateStore] = None,
                 coordinator: Optional[Coordinator] = None,
                 sink: Optional[SinkSpeech] = None) -> Optional[int]:
    """Render `event` and play it through sink-speech.

    Returns the history-row id, or None if nothing was rendered (empty
    text, render failure with no fallback, etc.).

    Blocks until playback finishes. Callers that need fire-and-forget
    should run this in a thread.
    """
    text = event.text.strip()
    if not text:
        return None

    state = state or StateStore()
    coordinator = coordinator or Coordinator(state=state)
    sink = sink or SinkSpeech()
    # Per-event target wins; otherwise the host's deployment default
    # (mel sets MEDIA_SPEECH_DEFAULT_TARGET=rooms to feed Snapcast),
    # falling back to local. Decision 1C.
    target = event.target or Target(
        name=os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET", "local"))

    engine = _resolve_engine(event)
    voice = _resolve_voice(event)
    ext = _ext_for(engine)

    audio_dir = _audio_dir()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    outfile = audio_dir / f"{stamp}--{event.source.value}.{ext}"

    fallback_info: dict = {}

    def _on_fallback(failed_engine: str, err: str) -> None:
        """Render engine failed; render_text will retry on edge. Record
        the failure visibly so silent degradation gets a trail.
        """
        short = err.strip().splitlines()[0] if err else "no detail"
        # Heuristic: collapse OpenAI quota errors to a clean label so
        # the notification body stays human-readable.
        kind = "render-fallback"
        if "insufficient_quota" in err:
            kind = "render-quota"
        fallback_info.update({
            "from_engine": failed_engine,
            "fallback_engine": "edge",
            "kind": kind,
            "detail": short[:300],
        })
        log.warning("intake: %s engine failed (%s); falling back to edge",
                    failed_engine, short)
        state.log_error("intake",
                        f"render {failed_engine} failed, fell back to edge",
                        extras={"kind": kind, "engine": failed_engine,
                                "detail": short[:300],
                                "source": event.source.value})
        # Throttled notification so the user sees it once per window.
        if kind == "render-quota":
            title = f"agent-media: {failed_engine} quota exhausted"
            body = "Falling back to edge for now."
        else:
            title = f"agent-media: {failed_engine} render failed"
            body = f"Falling back to edge. {short[:120]}"
        notify(key=f"render-fallback-{failed_engine}",
               title=title, content=body)

    started_at = time.time()
    ok, err = render_text(text, outfile, engine=engine, voice=voice,
                          on_fallback=_on_fallback)
    if not ok:
        log.warning("intake: render failed (%s): %s", engine, err)
        state.log_error("intake", f"render failed ({engine})",
                        extras={"err": err, "source": event.source.value})
        return None

    # Fallback path: realtime/qwen pre-picked a .wav name but edge wrote
    # MP3 bytes into it. Rename so the file name doesn't lie. mpv reads
    # either way, so playback isn't affected.
    if fallback_info and outfile.suffix == ".wav":
        renamed = outfile.with_suffix(".mp3")
        try:
            outfile.rename(renamed)
            outfile = renamed
        except OSError:
            pass

    # Spoken-text sidecar next to the clip + a live "now-speaking" record,
    # so the tmux popup / pane highlighter can show and highlight the text
    # being spoken. Reinstates the old tts.tmux `<stem>.txt` contract on the
    # core path (the forwarder/watcher used to carry this; media-mcp didn't).
    try:
        outfile.with_suffix(".txt").write_text(text)
    except OSError as e:  # noqa: BLE001
        log.warning("intake: text sidecar write failed: %s", e)
    state.set_now_playing(
        "speech", uri=str(outfile), started_at=started_at,
        target=target.name,
        extras={"text": text, "source": event.source.value,
                "engine": engine, "voice": voice})

    coordinator.before_speech()
    try:
        try:
            sink.play(str(outfile), target)
        except Exception as e:  # noqa: BLE001
            log.warning("intake: sink-speech.play failed: %s", e)
            state.log_error("intake", "sink-speech play failed",
                            extras={"detail": str(e),
                                    "source": event.source.value})
            return None
        # Wait for the broker to flip out of idle, then poll until done
        # — keeps the coordinator's before/after symmetry tight.
        for _ in range(20):
            if not sink.idle(target):
                break
            time.sleep(0.05)
        for _ in range(1200):
            if sink.idle(target):
                break
            time.sleep(0.1)
    finally:
        coordinator.after_speech()
        state.clear_now_playing("speech")

    extras = {"engine": engine, "voice": voice,
              "priority": event.priority.value,
              **(event.metadata or {})}
    if fallback_info:
        extras["fallback"] = fallback_info
    return state.add_history(
        sink="speech",
        uri=str(outfile),
        started_at=started_at,
        ended_at=time.time(),
        target=target.name,
        source=event.source.value,
        text=text,
        extras=extras,
    )
