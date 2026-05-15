"""Claude Code intake adapter.

Reads Claude Code's hook JSON from stdin, extracts the speech text, and
hands it to `submit_event`. Replaces the legacy bash hook
(`packages/audio-relay/src/agent_audio_relay/shell/hooks/claude-code-tts-hook.sh`).

Settings.json wires it as:

    "hooks": {
      "Stop":         [{"hooks":[{"type":"command",
                                  "command":"media-hook-claude-code",
                                  "timeout":30}]}],
      "Notification": [{"hooks":[{"type":"command",
                                  "command":"media-hook-claude-code",
                                  "timeout":30}]}]
    }

Behaviours preserved from the bash version:
  - Sources `~/.config/agent-audio-relay.env` (or `RELAY_ENV_FILE`) so
    OPENAI_API_KEY etc. don't have to live in settings.json.
  - Notification suppression: skip if another notif fired within 120s,
    or a Stop played within 90s.
  - Stop: read the latest assistant text from the transcript JSONL.
    Skip tool-call-only turns (no text content).
  - Dedup key (text-hash) collapses duplicate Stop / Stop+notif races.

Long-text routing (the old `CLAUDE_TTS_LONG_THRESHOLD` split into
tts-stream) is gone — Phase 3 of the restructure locked in a single
stream-only render path. The realtime engine produces audio in chunks
fast enough that long replies are no longer a special case.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from ..state import StateStore
from ..types import Event, Priority, Source
from .submit import submit_event


log = logging.getLogger(__name__)


def _load_env_file() -> None:
    """Load secrets from the user's relay env file into os.environ.

    Same precedence as the legacy bash hook: RELAY_ENV_FILE > the
    default at ~/.config/agent-audio-relay.env. Missing file → no-op.
    Lines starting with `export ` are honored.
    """
    candidates = [
        os.environ.get("RELAY_ENV_FILE") or "",
        str(Path.home() / ".config" / "agent-audio-relay.env"),
    ]
    for path in candidates:
        if not path:
            continue
        try:
            with open(path, "r") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):]
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
            return
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("hook: failed to read %s: %s", path, e)
            return


def _stamp_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    d = state / "agent-media" / "claude-stamps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_stamp(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_stamp(path: Path, value: int) -> None:
    try:
        path.write_text(str(value))
    except OSError:
        pass


def _latest_assistant_text(transcript_path: Path) -> str:
    """Walk the JSONL transcript from the end, return the most recent
    assistant turn's joined text content. Empty string if the latest
    turn is tool-call-only or the file is unparseable.
    """
    try:
        lines = transcript_path.read_text().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        parts = []
        for c in msg.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text") or "")
        return "\n".join(p for p in parts if p)
    return ""


def _dedup_seen(state: StateStore, text: str, ttl_seconds: int = 300) -> bool:
    """Crude text-hash dedup over the recent history table.

    Returns True if this exact text has been spoken in the last
    `ttl_seconds` (so caller should skip emitting again).
    """
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    cutoff = time.time() - ttl_seconds
    for row in state.recent_history(sink="speech", limit=20):
        if (row.get("started_at") or 0) < cutoff:
            break
        extras = row.get("extras") or {}
        if isinstance(extras, str):
            try:
                extras = json.loads(extras)
            except json.JSONDecodeError:
                extras = {}
        if extras.get("dedup_key") == key:
            return True
        if (row.get("text") or "") == text:
            return True
    return False


def _handle_notification(payload: dict) -> int:
    """Notif path: prefer Claude's `message` field, dedup-skip if a
    Stop just played or a notif fired within the cooldown windows.
    """
    msg = (payload.get("message") or "").strip()
    if not msg:
        return 0

    # Annotate with the tmux session if we can see one.
    tmux_pane = os.environ.get("TMUX_PANE")
    if tmux_pane:
        try:
            import subprocess
            r = subprocess.run(
                ["tmux", "display-message", "-p", "-t", tmux_pane,
                 "#{session_name}"],
                capture_output=True, text=True, timeout=2,
            )
            sess = (r.stdout or "").strip()
            if sess:
                msg = f"Session {sess}: {msg}"
        except Exception:  # noqa: BLE001
            pass

    stamps = _stamp_dir()
    now = int(time.time())
    last_notif = _read_stamp(stamps / "notif-last")
    last_stop = _read_stamp(stamps / "stop-last")
    if last_notif and (now - last_notif) < 120:
        return 0
    if last_stop and (now - last_stop) < 90:
        return 0
    _write_stamp(stamps / "notif-last", now)

    submit_event(Event(text=msg, source=Source.CLAUDE_CODE,
                       priority=Priority.HIGH,
                       metadata={"kind": "notif"}))
    return 0


def _handle_stop(payload: dict) -> int:
    """Stop path: read the latest assistant text and submit it."""
    raw_path = (payload.get("transcript_path") or "").strip()
    if not raw_path:
        return 0
    tp = Path(raw_path)
    if not tp.is_file():
        return 0

    # Tight retry while the transcript flushes.
    for _ in range(5):
        try:
            ok = tp.stat().st_size > 0 and (time.time() - tp.stat().st_mtime) <= 5
        except OSError:
            ok = False
        if ok:
            break
        time.sleep(0.1)

    text = _latest_assistant_text(tp)
    if not text:
        return 0

    state = StateStore()
    if _dedup_seen(state, text):
        return 0

    dedup_key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    rid = submit_event(
        Event(text=text, source=Source.CLAUDE_CODE,
              priority=Priority.NORMAL,
              metadata={"kind": "stop", "dedup_key": dedup_key}),
        state=state,
    )
    if rid is not None:
        _write_stamp(_stamp_dir() / "stop-last", int(time.time()))
    return 0


def main() -> int:
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    if os.environ.get("CLAUDE_TTS_ENABLED", "1") == "0":
        return 0

    _load_env_file()

    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return 0
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    event_name = payload.get("hook_event_name")
    try:
        if event_name == "Notification":
            return _handle_notification(payload)
        if event_name == "Stop":
            return _handle_stop(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("hook: %s handler failed: %s", event_name, e)
        try:
            StateStore().log_error("hook-claude-code",
                                   f"{event_name} failed",
                                   extras={"detail": str(e)})
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
