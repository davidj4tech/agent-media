#!/usr/bin/env python3
"""A speech-dispatcher output module that speaks through agent-media.

The inversion: instead of agent-media growing an adapter for every program
that might want to talk, it becomes something speech-dispatcher can call — and
then everything that already speaks SSIP inherits it. Orca, Emacspeak,
speechd-el, BRLTTY, any Qt app using QTextToSpeech, `spd-say` from a shell
script. None of them need to know agent-media exists.

What they inherit is not only the render engines. It is the arbitration none
of them have ever had: music ducked while speaking, urgent replies barging in,
per-source ordering — and, uniquely, a *target*. speech-dispatcher has no
notion of where to speak; it is always the local device. Here the synthesis
voice selects one, so Emacspeak running on a server in Germany can speak into
a room in Australia.

Deliberately NOT handled: CHAR and KEY. Those are what make a screen reader a
screen reader, and they need tens of milliseconds; this path renders a clip
and hands it to a player on the other side of the world. Declining them
honestly is better than half-doing them and leaving someone to wonder why
their cursor speech lags by a second.

Protocol per speech-dispatcher's module interface (see Sacha Chua's speechd-ai
for the reference implementation this follows):

    INIT                  -> 299 OK LOADED SUCCESSFULLY
    SPEAK                 -> 202 OK SEND DATA, body terminated by "."
                             then 200 OK SPEAKING, 701 BEGIN, 702 END
    SET / AUDIO / LOGLEVEL-> 203 OK ...
    STOP / PAUSE / CANCEL -> 703 EVENT ...
    QUIT                  -> 210 OK QUIT
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time

LOG = os.environ.get("AM_SPEECHD_LOG", "")

# Targets, offered as synthesis voices. `SET SYNTHESIS_VOICE phone` is how a
# speechd client picks a room — the only vocabulary the protocol gives us that
# means anything here.
TARGETS = ("default", "local", "rooms", "phone")

# A speechd client cannot see that "speak" means shipping audio across the
# world, and speech-dispatcher arbitrates priority server-side, so the module
# never learns that a message was only a progress update. Without a bound, one
# chatty client saturates a slow link and holds the speech lock. These are the
# only guards available at this layer.
MAX_CHARS = int(os.environ.get("AM_SPEECHD_MAX_CHARS", "2000"))
MIN_GAP_S = float(os.environ.get("AM_SPEECHD_MIN_GAP_S", "0.4"))


def log(msg: str) -> None:
    if not LOG:
        return
    try:
        with open(LOG, "a") as fh:
            fh.write(f"{time.time():.3f} {msg}\n")
    except OSError:
        pass


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def media_bin() -> str:
    """Absolute `media`. speech-dispatcher is started by a systemd user socket,
    so its PATH is minimal and a bare name would not resolve."""
    found = shutil.which("media")
    if found:
        return found
    for cand in (os.path.expanduser("~/.local/bin/media"),
                 os.path.expanduser("~/projects/agent-media/.venv/bin/media")):
        if os.access(cand, os.X_OK):
            return cand
    return "media"


class Speaker:
    """One utterance at a time, with the events speechd expects around it."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.last_end = 0.0

    def speak(self, text: str, target: str) -> None:
        text = text.strip()
        if not text:
            out("701 BEGIN")
            out("702 END")
            return
        if len(text) > MAX_CHARS:
            # Truncate rather than refuse: a caller that sent a whole buffer
            # still wants to hear the start of it, and a silent refusal is the
            # failure mode this project keeps having to fix.
            text = text[:MAX_CHARS] + " … truncated."
        gap = MIN_GAP_S - (time.time() - self.last_end)
        if gap > 0:
            time.sleep(gap)
        argv = [media_bin(), "say"]
        if target and target not in ("default", ""):
            argv += ["--target", target]
        argv.append(text)
        log(f"speak target={target} chars={len(text)}")
        out("200 OK SPEAKING")
        out("701 BEGIN")
        try:
            with self.lock:
                self.proc = subprocess.Popen(
                    argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    start_new_session=True)
            _o, err = self.proc.communicate()
            rc = self.proc.returncode
            if rc:
                log(f"media say rc={rc} err={err[:200]!r}")
        except OSError as e:
            log(f"spawn failed: {e}")
        finally:
            with self.lock:
                self.proc = None
            self.last_end = time.time()
            out("702 END")

    def stop(self) -> None:
        """Kill the utterance and tell agent-media to stop what is playing.

        Both halves are needed: killing the renderer leaves audio already
        handed to the player still playing, and stopping the player alone
        leaves the renderer about to start more."""
        with self.lock:
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), 15)
            except OSError:
                pass
        try:
            subprocess.run([media_bin(), "stop"], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def main() -> int:
    speaker = Speaker()
    target = os.environ.get("AM_SPEECHD_TARGET", "default")
    worker: threading.Thread | None = None

    for raw in sys.stdin:
        line = raw.rstrip("\n").rstrip("\r")
        log(f"< {line}")

        if line.startswith("INIT"):
            out("299-agent-media module ready")
            out("299 OK LOADED SUCCESSFULLY")

        elif line.startswith(("SPEAK", "CHAR", "KEY", "SOUND_ICON")):
            kind = line.split()[0]
            out("202 OK SEND DATA")
            body: list[str] = []
            for data in sys.stdin:                     # dot-terminated body
                d = data.rstrip("\n").rstrip("\r")
                if d == ".":
                    break
                body.append(d[1:] if d.startswith("..") else d)
            text = "\n".join(body)
            if kind in ("CHAR", "KEY"):
                # See the module docstring: cursor-granularity speech needs a
                # latency this path cannot offer. Answer the protocol properly
                # and say nothing, rather than lag a second behind the cursor.
                out("200 OK SPEAKING")
                out("701 BEGIN")
                out("702 END")
                continue
            worker = threading.Thread(target=speaker.speak,
                                      args=(text, target), daemon=True)
            worker.start()

        elif line.startswith("STOP"):
            speaker.stop()
            out("703 EVENT STOP")

        elif line.startswith("PAUSE"):
            subprocess.run([media_bin(), "pause"], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out("703 EVENT PAUSE")

        elif line.startswith("CANCEL"):
            speaker.stop()
            out("703 EVENT CANCEL")

        elif line.startswith("LIST VOICES"):
            # The protocol's voice list is the only place a client can be
            # offered a choice, so the targets are published as voices.
            for name in TARGETS:
                out(f"200-{name} none none")
            out("200 OK VOICE LIST SENT")

        elif line.startswith("SET"):
            m = re.match(r"SET\s+\S+\s+(\S+)\s+(.*)", line)
            if m:
                key, val = m.group(1).upper(), m.group(2).strip()
                if key in ("SYNTHESIS_VOICE", "VOICE") and val.lower() in TARGETS:
                    target = val.lower()
                    log(f"target -> {target}")
            out("203 OK RECEIVING SETTINGS")
            out("203 OK SETTINGS RECEIVED")

        elif line.startswith("AUDIO"):
            out("207 OK RECEIVING AUDIO SETTINGS")
            out("203 OK AUDIO INITIALIZED")

        elif line.startswith("LOGLEVEL"):
            out("203 OK RECEIVING LOGLEVEL SETTINGS")
            out("203 OK LOGLEVEL SET")

        elif line.startswith("QUIT"):
            speaker.stop()
            out("210 OK QUIT")
            return 0

        else:
            out("300 ERR UNKNOWN COMMAND")
    return 0


if __name__ == "__main__":
    sys.exit(main())
