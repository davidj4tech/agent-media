"""media-sink-speech: the speech player (mpv) on its IPC endpoint, kept running.

What `packages/core/services/sink-speech/run` does under runit or systemd, for
a host with neither — today the Windows logon task `Sasonica\\speech`, which
`sasonica install --local-speech` adds. Linux and the phone keep their run
script; this is not wired into either.

The endpoint is the one the speech sink talks to: the `local` target's socket
path, or on Windows the named pipe `_mpv_ipc.endpoint_name()` makes of it, so
the picker's "is a player running" and this agree by construction. mpv's
options are the run script's, read from the same env keys (MEDIA_SPEECH_AO,
_VOLUME, _VOLUME_MAX, _LOUDNORM, _SAMPLERATE, _FORMAT, _CHANNELS,
_PITCH_CORRECTION); the AO defaults to the audio backend's (wasapi on
Windows). mpv is the user's own: MEDIA_MPV_BIN, else `mpv` on PATH.

It restarts mpv when mpv exits (2 s, backing off to a minute when it keeps
failing at once), the way runit would. Killing this process does not take
mpv with it on Windows; a new one then finds a player already on the pipe and
watches it instead of starting a second, and `sasonica install --local-speech
off` asks the player to quit over the pipe.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

#: What to tell someone with no mpv. The shinchiro build is mpv's own
#: recommended Windows build, and the one winget carries.
WINGET_HINT = "winget install --id shinchiro.mpv -e"


def socket_path() -> Path:
    """The `local` speech target's socket path (never a tcp:// bridge)."""
    from ..sinks.speech import _socket_for
    from ..types import Target

    ep = _socket_for(Target(name="local"))
    if str(ep).startswith("tcp://"):
        state = Path(os.environ.get("XDG_STATE_HOME",
                                    str(Path.home() / ".local" / "state")))
        return state / "agent-media" / "sink-speech.sock"
    return Path(ep)


def find_mpv() -> Optional[str]:
    return shutil.which(os.environ.get("MEDIA_MPV_BIN") or "mpv")


def mpv_argv(mpv: str, endpoint: str) -> list[str]:
    """The run script's mpv command line, from the same env keys."""
    from .. import audio_backend

    env = os.environ
    ao = env.get("MEDIA_SPEECH_AO") or audio_backend.backend().default_ao() or "auto"
    loudnorm = env.get("MEDIA_SPEECH_LOUDNORM",
                       "dynaudnorm=targetrms=0.95:maxgain=30:gausssize=15")
    af = "scaletempo2" if ao in ("openal", "opensles") else ""
    if loudnorm:
        af = f"{af},{loudnorm}" if af else loudnorm
    return [mpv, "--idle=yes", f"--ao={ao}", "--force-window=no",
            "--no-terminal",
            f"--input-ipc-server={endpoint}",
            f"--volume-max={env.get('MEDIA_SPEECH_VOLUME_MAX') or '200'}",
            f"--volume={env.get('MEDIA_SPEECH_VOLUME') or '130'}",
            "--keep-open=no",
            "--gapless-audio=no", "--audio-stream-silence=no", "--ytdl=no",
            "--network-timeout=60",
            f"--af={af}",
            f"--audio-pitch-correction={env.get('MEDIA_SPEECH_PITCH_CORRECTION') or 'yes'}",
            f"--audio-samplerate={env.get('MEDIA_SPEECH_SAMPLERATE') or '48000'}",
            f"--audio-format={env.get('MEDIA_SPEECH_FORMAT') or 's16'}",
            f"--audio-channels={env.get('MEDIA_SPEECH_CHANNELS') or 'stereo'}"]


def _say(msg: str) -> None:
    print(f"media-sink-speech: {msg}", flush=True)


def run(once: bool = False, sleep=time.sleep, call=subprocess.call) -> int:
    from ..sinks import _mpv_ipc as ipc

    mpv = find_mpv()
    if not mpv:
        _say("mpv is not on PATH, and local speech needs it. Install it with "
             f"`{WINGET_HINT}` (or set MEDIA_MPV_BIN to an mpv.exe), then "
             "start this again.")
        return 2
    path = socket_path()
    endpoint = ipc.endpoint_name(path)
    pipe = endpoint != str(path)
    if not pipe:
        path.parent.mkdir(parents=True, exist_ok=True)
    argv = mpv_argv(mpv, endpoint)
    backoff = 2.0
    while True:
        if pipe and ipc.endpoint_exists(endpoint):
            # A player is already on the pipe (one this replaced left behind):
            # watch it rather than start a second beside it.
            _say(f"a player is already on {endpoint}; watching it")
            while ipc.endpoint_exists(endpoint):
                sleep(5)
            if once:
                return 0
            continue
        if not pipe:
            try:
                path.unlink()        # a dead broker's socket blocks the bind
            except FileNotFoundError:
                pass
        _say(f"mpv on {endpoint}")
        t0 = time.monotonic()
        rc = call(argv)
        if once:
            return rc
        ran = time.monotonic() - t0
        backoff = 2.0 if ran > 30 else min(backoff * 2, 60.0)
        _say(f"mpv exited ({rc}) after {ran:.0f}s; again in {backoff:.0f}s")
        sleep(backoff)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="media-sink-speech",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", action="store_true",
                    help="run mpv once and exit with its status (no restarts)")
    ap.add_argument("--print", action="store_true",
                    help="print the endpoint and mpv's command line, start nothing")
    a = ap.parse_args(argv)
    from ..intake._env import load_env_file
    load_env_file("sink-speech")
    if a.print:
        from ..sinks import _mpv_ipc as ipc
        endpoint = ipc.endpoint_name(socket_path())
        print(endpoint)
        print(" ".join(mpv_argv(find_mpv() or "mpv", endpoint)))
        return 0
    return run(once=a.once)


if __name__ == "__main__":
    sys.exit(main())
