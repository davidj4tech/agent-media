"""Matrix intake daemon.

Long-poll subscribes to a single Matrix room, plays incoming voice
messages and audio attachments through sink-speech, and handles a
small set of text commands (`!pause`, `!resume`, `!skip`, `!replay`).

Replaces `/data/data/com.termux/files/home/.local/bin/sam-listener.py`.
Key differences from that script:

  - Access token comes from `MATRIX_ACCESS_TOKEN` (or a sops-managed
    env file) — no more hardcoded secret in the source.
  - Playback goes through sink-speech, not `termux-media-player`.
    Sam's voice notes thus share the one openal broker, ducking and
    history with everything else.
  - Control commands (`!pause` etc.) call the unified Coordinator /
    Sink interface. Same surface as the in-flight MCP commands that
    Phase 6 will expose.
  - Recording / sending back to the room is dropped here. That belongs
    in capture/ (Phase 5).

The `/sync` loop itself is agent_media_core.matrix, shared: on a host
running the canvas it lives there and this package is one of its readers
(:func:`consumer`); `media-intake-matrix` runs the loop standalone. Never
both on one token — two loops lose each other's events.

Voice notes are played only for rooms in `MATRIX_SPEECH_ROOMS` (default:
every allowed room). Leave a bridged room out of it, or its texts are read
aloud in the house.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

from agent_media_core import matrix

from agent_media_core.route import Coordinator
from agent_media_core.sinks.music import SinkMusic
from agent_media_core.sinks.speech import SinkSpeech
from agent_media_core.state import StateStore
from agent_media_core.types import Source, Target


log = logging.getLogger(__name__)


def _audio_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME",
                               str(Path.home() / ".cache")))
    d = base / "agent-media" / "matrix-audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _handle_voice_message(*, mxc: str, homeserver: str, token: str,
                          sink: SinkSpeech, coordinator: Coordinator,
                          state: StateStore, target: Target,
                          sender: str) -> None:
    url = matrix.mxc_to_http(homeserver, mxc)
    if not url:
        return
    dest = _audio_cache_dir() / f"matrix-{int(time.time() * 1000)}.ogg"
    if not matrix.download(url, token, dest):
        return

    started_at = time.time()
    coordinator.before_speech()
    try:
        try:
            sink.play(str(dest), target)
        except Exception as e:  # noqa: BLE001
            log.warning("matrix: sink-speech.play failed: %s", e)
            return
        for _ in range(20):
            if not sink.idle(target):
                break
            time.sleep(0.05)
        for _ in range(1800):
            if sink.idle(target):
                break
            time.sleep(0.1)
    finally:
        coordinator.after_speech()
        state.add_history(
            sink="speech", uri=str(dest),
            started_at=started_at, ended_at=time.time(),
            target=target.name, source=Source.MATRIX.value,
            extras={"kind": "voice-message", "sender": sender,
                    "mxc": mxc},
        )


def _handle_text_command(body: str, *, music: SinkMusic, sink: SinkSpeech,
                         state: StateStore, target: Target) -> bool:
    """Map !commands onto sink primitives. Returns True if the event
    was handled (caller should mark it `seen`).
    """
    lower = body.lower().strip()
    if lower in ("!pause", "pause"):
        try:
            sink.pause(target)
        except Exception:
            try:
                music.pause(target)
            except Exception:
                pass
        return True
    if lower in ("!resume", "resume", "!play", "play"):
        try:
            sink.resume(target)
        except Exception:
            try:
                music.resume(target)
            except Exception:
                pass
        return True
    if lower in ("!skip", "skip", "!stop", "stop"):
        try:
            sink.stop(target)
        except Exception:
            pass
        return True
    if lower in ("!replay", "replay"):
        rows = state.recent_history(sink="speech", limit=1)
        if rows:
            try:
                sink.play(rows[0]["uri"], target)
            except Exception:
                pass
        return True
    return False


def _process_event(ev: dict, *, room_id: str, sam_id: str,
                   control_ids: Iterable[str], homeserver: str, token: str,
                   sink: SinkSpeech, music: SinkMusic,
                   coordinator: Coordinator, state: StateStore,
                   target: Target) -> bool:
    """Returns True if the event was handled (so caller marks it seen)."""
    sender = ev.get("sender") or ""
    content = ev.get("content") or {}
    msgtype = content.get("msgtype")

    if sender in control_ids and msgtype == "m.text":
        body = (content.get("body") or "").strip()
        return _handle_text_command(body, music=music, sink=sink,
                                    state=state, target=target)

    if sender == sam_id and msgtype in ("m.audio", "m.voice"):
        mxc = content.get("url")
        if not mxc:
            return False
        _handle_voice_message(
            mxc=mxc, homeserver=homeserver, token=token,
            sink=sink, coordinator=coordinator,
            state=state, target=target, sender=sender,
        )
        return True
    return False


def _csv(value: str | None) -> set[str]:
    return {v.strip() for v in (value or "").split(",") if v.strip()}


def consumer(config: matrix.Config, env: dict | None = None):
    """The speech intake as a reader of a shared sync loop.

    Returns `(on_event, stop)`: `on_event(room_id, event)` only queues, so
    the sync thread is never held up by a voice note playing; one worker
    plays them in order. Sinks are made lazily, on the worker."""
    env = os.environ if env is None else env
    sam_id = env.get("MATRIX_SAM_ID", "@agent:example.org")
    control_ids = _csv(env.get("MATRIX_CONTROL_IDS")
                       or f"@owner:example.org,{sam_id}")
    speech_rooms = (_csv(env.get("MATRIX_SPEECH_ROOMS"))
                    if env.get("MATRIX_SPEECH_ROOMS") is not None
                    else set(config.rooms))
    q: "queue.Queue[tuple[str, dict] | None]" = queue.Queue()

    def on_event(room_id: str, ev: dict) -> None:
        if room_id in speech_rooms:
            q.put((room_id, ev))

    def work() -> None:
        target = Target(name="local")
        state = StateStore()
        sink = SinkSpeech()
        music = SinkMusic()
        coordinator = Coordinator(state=state, music=music)
        while True:
            item = q.get()
            if item is None:
                return
            room_id, ev = item
            try:
                _process_event(
                    ev, room_id=room_id, sam_id=sam_id,
                    control_ids=control_ids,
                    homeserver=config.homeserver, token=config.token,
                    sink=sink, music=music, coordinator=coordinator,
                    state=state, target=target,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("matrix: event handler failed: %s", e)

    threading.Thread(target=work, name="matrix-speech", daemon=True).start()
    return on_event, (lambda: q.put(None))


_running = True


def _shutdown(*_: object) -> None:
    global _running
    _running = False


def main() -> int:
    """The loop standalone, for a host with no canvas to run it."""
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    config = matrix.Config.from_env()
    if config is None:
        print("matrix: MATRIX_ACCESS_TOKEN and MATRIX_ROOM_ALLOW must be set",
              file=sys.stderr)
        return 2
    sync = matrix.Sync(config)
    on_event, stop = consumer(config)
    sync.subscribe(on_event)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    matrix.wait_forever(sync, lambda: _running)
    stop()
    log.info("matrix: shutting down")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
