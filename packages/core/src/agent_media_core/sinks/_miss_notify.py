"""Phone notification for LOST spoken replies — delivered when the phone wakes.

A reply is lost when play_playlist can't reach the player at all (doze'd
phone, dead bridge) — the fallback chain is exhausted and nothing sounded.
The cruel twist: the same doze that ate the reply also blocks an immediate
notification. So each loss is appended to a ledger and a singleton retrier
keeps offering a compact "N spoken replies didn't reach this phone" via
`ssh <host> termux-notification` until the phone answers (it wakes with the
user), then clears the ledger. `--id speech-miss` makes retries replace the
same notification instead of stacking the shade.

Wired into SpeechSink.play_playlist's failure path via record_miss().
Config: MEDIA_SPEECH_MISS_SSH overrides the ssh host (default: the same
relay the prefetch uses — MEDIA_SPEECH_CLIP_SSH_<TARGET> /
MEDIA_MUSIC_LOCAL_SSH / p8ar).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

RETRY_S = 60
GIVE_UP_S = 4 * 3600
_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


def _state_dir() -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME",
                            str(Path.home() / ".local" / "state")))
    d = d / "agent-media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ledger() -> Path:
    return _state_dir() / "speech-misses.log"


def _pidfile() -> Path:
    return _state_dir() / "speech-miss-notifier.pid"


def _notifier_running() -> bool:
    try:
        pid = int(_pidfile().read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def miss_host(target_name: str = "") -> str:
    key = f"MEDIA_SPEECH_CLIP_SSH_{target_name.upper()}" if target_name else ""
    return (os.environ.get("MEDIA_SPEECH_MISS_SSH")
            or (os.environ.get(key, "") if key else "")
            or os.environ.get("MEDIA_MUSIC_LOCAL_SSH", "p8ar"))


def record_miss(target_name: str = "") -> None:
    """Append this loss to the ledger and ensure a retrier is running.
    Never raises — this runs inside playback's failure path."""
    try:
        with _ledger().open("a") as fh:
            fh.write(f"{int(time.time())}\n")
        if not _notifier_running():
            subprocess.Popen(
                [sys.executable, "-m", "agent_media_core.sinks._miss_notify",
                 miss_host(target_name)],
                start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def _try_notify(host: str, count: int, latest: int) -> bool:
    import shlex
    when = time.strftime("%H:%M", time.localtime(latest))
    plural = "reply" if count == 1 else "replies"
    content = (f"{count} spoken {plural} didn't reach this phone "
               f"(latest {when}) — likely dozed. See the canvas/session "
               f"for what was said.")
    # ssh re-splits the remote argv on spaces — quote it as ONE command string
    # or the multi-word title/content shatter into stray arguments.
    remote = " ".join(shlex.quote(a) for a in
                      ["termux-notification", "--id", "speech-miss",
                       "--title", "agent-media: missed speech",
                       "--content", content])
    r = subprocess.run(["ssh", *_SSH_OPTS, host, remote],
                       capture_output=True, timeout=20, check=False)
    return r.returncode == 0


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else miss_host()
    _pidfile().write_text(str(os.getpid()))
    deadline = time.time() + GIVE_UP_S
    try:
        while time.time() < deadline:
            lines = []
            try:
                lines = [ln for ln in _ledger().read_text().splitlines() if ln]
            except OSError:
                pass
            if not lines:
                return 0
            try:
                if _try_notify(host, len(lines), int(lines[-1])):
                    _ledger().unlink(missing_ok=True)
                    return 0
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
            time.sleep(RETRY_S)
        return 1
    finally:
        _pidfile().unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
