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
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

from agent_media_core.route import Coordinator
from agent_media_core.sinks.music import SinkMusic
from agent_media_core.sinks.speech import SinkSpeech
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Priority, Source, Target


log = logging.getLogger(__name__)

DEFAULT_SYNC_TIMEOUT_MS = 30000
DEFAULT_HOMESERVER = "https://matrix.example.org"

_running = True


def _shutdown(*_: object) -> None:
    global _running
    _running = False


def _state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME",
                               str(Path.home() / ".local" / "state")))
    d = base / "agent-media" / "matrix"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_state(path: Path, state: dict) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(path)
    except OSError as e:
        log.warning("matrix: state save failed: %s", e)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"next_batch": None, "seen": []}


def _mxc_to_http(homeserver: str, mxc: Optional[str]) -> Optional[str]:
    if not mxc or not mxc.startswith("mxc://"):
        return None
    rest = mxc[len("mxc://"):]
    if "/" not in rest:
        return None
    server, media_id = rest.split("/", 1)
    return f"{homeserver}/_matrix/client/v1/media/download/{server}/{media_id}"


def _download(url: str, token: str, dest: Path, timeout: float = 60.0) -> bool:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
        return dest.exists() and dest.stat().st_size > 0
    except (urllib.error.URLError, OSError) as e:
        log.warning("matrix: download failed (%s): %s", url, e)
        return False


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
    url = _mxc_to_http(homeserver, mxc)
    if not url:
        return
    dest = _audio_cache_dir() / f"matrix-{int(time.time() * 1000)}.ogg"
    if not _download(url, token, dest):
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


def main() -> int:
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0

    token = os.environ.get("MATRIX_ACCESS_TOKEN")
    if not token:
        print("matrix: MATRIX_ACCESS_TOKEN not set", file=sys.stderr)
        return 2

    homeserver = os.environ.get("MATRIX_HOMESERVER", DEFAULT_HOMESERVER).rstrip("/")
    sam_id = os.environ.get("MATRIX_SAM_ID", "@agent:example.org")
    control_ids = set(filter(None, (
        os.environ.get("MATRIX_CONTROL_IDS")
        or f"@owner:example.org,{sam_id}"
    ).split(",")))
    room_allow = set(filter(None, (
        os.environ.get("MATRIX_ROOM_ALLOW", "").split(",")
    )))
    if not room_allow:
        print("matrix: MATRIX_ROOM_ALLOW must list at least one room id",
              file=sys.stderr)
        return 2

    state_path = _state_dir() / "sync.json"
    sync_state = _load_state(state_path)
    seen = list(sync_state.get("seen") or [])

    target = Target(name="local")
    state = StateStore()
    sink = SinkSpeech()
    music = SinkMusic()
    coordinator = Coordinator(state=state, music=music)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("matrix: starting (homeserver=%s, rooms=%s)",
             homeserver, ",".join(sorted(room_allow)))

    timeout_ms = int(os.environ.get("MATRIX_SYNC_TIMEOUT_MS",
                                    DEFAULT_SYNC_TIMEOUT_MS))
    backoff = 1.0

    while _running:
        params = {"timeout": str(timeout_ms)}
        if sync_state.get("next_batch"):
            params["since"] = sync_state["next_batch"]
        url = (f"{homeserver}/_matrix/client/v3/sync?"
               + urllib.parse.urlencode(params))
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000 + 30) as resp:
                data = json.loads(resp.read())
            backoff = 1.0
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            log.warning("matrix: sync failed: %s; retry in %.1fs", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("matrix: unexpected sync error: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue

        sync_state["next_batch"] = data.get("next_batch")
        rooms = (data.get("rooms") or {}).get("join") or {}
        for room_id, room in rooms.items():
            if room_id not in room_allow:
                continue
            events = ((room.get("timeline") or {}).get("events") or [])
            for ev in events:
                ev_id = ev.get("event_id")
                if ev_id and ev_id in seen:
                    continue
                try:
                    handled = _process_event(
                        ev,
                        room_id=room_id, sam_id=sam_id,
                        control_ids=control_ids,
                        homeserver=homeserver, token=token,
                        sink=sink, music=music,
                        coordinator=coordinator, state=state,
                        target=target,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("matrix: event handler failed: %s", e)
                    handled = False
                if ev_id:
                    seen.append(ev_id)
                    if len(seen) > 100:
                        seen = seen[-100:]
        sync_state["seen"] = seen
        _save_state(state_path, sync_state)

    log.info("matrix: shutting down")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
