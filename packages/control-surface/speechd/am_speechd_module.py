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

SET, AUDIO and LOGLEVEL are *block* commands: the bare keyword, then
`key=value` lines, then a lone ".". The body must be consumed before replying
with the confirmation, or every one of those lines is read as the next command
and the stream desynchronises for good. Driving the module by hand with
single-line commands will not show this — speech-dispatcher never sends that
form.
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
#
# How far this reaches depends on the host's config, so it is worth being
# precise. The chosen target always lands on the history row and its
# now-playing uri, which is what the popup reads to decide which player its
# controls talk to. Whether it also changes where the audio comes out depends
# on MEDIA_REMOTE_SAY_CMD_<TARGET> (see submit._remote_say_cmd): a target with
# its own lane uses it, and a target set to `-` renders and plays locally. A
# host that only sets the global MEDIA_REMOTE_SAY_CMD sends every target down
# that one lane, so `-y rooms` is labelled rooms and heard wherever the global
# lane points — correct, but not what the voice name suggests.
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
        self.aborted = ""      # "STOP"/"CANCEL" if this utterance was cut short

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
        argv = [media_bin(), "say", text]
        # The target is chosen through the environment, not a flag: `media say`
        # has no --target, and passing one made every targeted utterance exit 2
        # with a usage error that nothing surfaced. "default" means leave the
        # ambient setting alone.
        env = os.environ.copy()
        if target and target not in ("default", ""):
            env["MEDIA_SPEECH_DEFAULT_TARGET"] = target
        log(f"speak target={target} chars={len(text)}")
        self.aborted = ""
        out("200 OK SPEAKING")
        out("701 BEGIN")
        try:
            with self.lock:
                self.proc = subprocess.Popen(
                    argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    start_new_session=True, env=env)
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
            # The end-of-utterance event is the speaking thread's to send, and
            # which one it is depends on how the utterance ended. STOP and
            # CANCEL produce no reply of their own (sd_dummy sends nothing at
            # all for a STOP with nothing playing) — the event arrives here.
            out(f"703 EVENT {self.aborted}" if self.aborted else "702 END")

    def active(self) -> bool:
        """Whether an utterance of ours is in flight right now."""
        with self.lock:
            proc = self.proc
        return bool(proc and proc.poll() is None)

    def stop(self, event: str = "STOP") -> bool:
        """Kill our utterance and stop the audio it already handed over.

        Both halves are needed: killing the renderer leaves audio already
        given to the player still playing, and stopping the player alone
        leaves the renderer about to start more.

        `media stop` is only reached when an utterance of ours was actually in
        flight. The channel is shared — music, books, other sessions — so
        stopping it when we have nothing playing would silence someone else's
        audio, which is what a bare STOP or a speechd shutdown would otherwise
        do every time.

        Returns whether there was anything of ours to stop."""
        if not self.active():
            return False
        with self.lock:
            proc = self.proc
        if not (proc and proc.poll() is None):
            return False
        self.aborted = event
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except OSError:
            pass
        try:
            subprocess.run([media_bin(), "stop"], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
        return True


def strip_ssml(text: str) -> str:
    """speechd wraps every utterance in SSML (`<speak>…</speak>`) whether or not
    the module asked for it. agent-media's renderers take plain text, so the
    tags would otherwise be handed to the synthesiser as words to say."""
    text = re.sub(r"<[^>]*>", "", text)
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                         ("&apos;", "'"), ("&amp;", "&")):   # &amp; last
        text = text.replace(entity, char)
    return text.strip()


def read_block() -> list[str]:
    """Consume a dot-terminated body, undoing the protocol's dot-stuffing."""
    body: list[str] = []
    for data in sys.stdin:
        d = data.rstrip("\n").rstrip("\r")
        if d == ".":
            break
        body.append(d[1:] if d.startswith("..") else d)
    return body


def parse_settings(body: list[str]) -> dict[str, str]:
    settings = {}
    for line in body:
        if "=" in line:
            key, _, val = line.partition("=")
            settings[key.strip().lower()] = val.strip()
    return settings


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
            text = strip_ssml("\n".join(read_block()))
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

        # STOP, PAUSE and CANCEL take no reply of their own: the event comes
        # from the speaking thread as it unwinds. Answering here as well put an
        # extra line on the pipe, which speechd then read as the answer to the
        # *next* command — at shutdown that is QUIT, and the daemon waited for
        # a "210 OK QUIT" it had already thrown away, hanging mid-teardown and
        # keeping the socket so the next client hung too.
        elif line.startswith("STOP"):
            speaker.stop("STOP")

        elif line.startswith("PAUSE"):
            # Pause, not abort: the render keeps going and the player holds.
            # Guarded by active() for the same reason as stop() — an unguarded
            # `media pause` pauses whatever the shared channel happens to be
            # playing, which may be someone else's music.
            if speaker.active():
                subprocess.run([media_bin(), "pause"], timeout=10,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)

        elif line.startswith("CANCEL"):
            speaker.stop("CANCEL")

        elif line.startswith("LIST VOICES"):
            # The protocol's voice list is the only place a client can be
            # offered a choice, so the targets are published as voices. The
            # three fields are TAB-separated: with spaces speechd rejects the
            # whole list ("Can't get a list of voices") and drops the module,
            # having already accepted every other part of the handshake.
            for name in TARGETS:
                out(f"200-{name}\ten\tnone")
            out("200 OK VOICE LIST SENT")

        elif line.startswith("SET"):
            out("203 OK RECEIVING SETTINGS")
            m = re.match(r"SET\s+\S+\s+(\S+)\s+(.*)", line)
            if m:                                      # single-line form, by hand
                settings = {m.group(1).lower(): m.group(2).strip()}
            else:
                settings = parse_settings(read_block())
            for key in ("synthesis_voice", "voice"):
                val = settings.get(key, "").lower()
                if val in TARGETS:
                    target = val
                    log(f"target -> {target}")
            out("203 OK SETTINGS RECEIVED")

        elif line.startswith("AUDIO"):
            out("207 OK RECEIVING AUDIO SETTINGS")
            settings = parse_settings(read_block())
            method = settings.get("audio_output_method", "")
            if method == "server":
                # speechd offers to own the device and expects samples back.
                # This module has none to give: it renders through agent-media,
                # which picks the target and plays there. Declining is honest —
                # and speechd answers it by re-offering module-side audio, which
                # is the arrangement we want, so the refusal is how we get it.
                log("audio method=server (declined)")
                out("300-server audio is not supported; agent-media plays its own")
                out("300 MODULE ERROR")
            else:
                # Any other method means "play it yourself". We already do; the
                # device names are agent-media's business, not speechd's.
                log(f"audio method={method or '?'} (module-side, accepted)")
                out("203 OK AUDIO INITIALIZED")

        elif line.startswith("LOGLEVEL"):
            out("207 OK RECEIVING LOGLEVEL SETTINGS")   # 207, as sd_dummy sends
            read_block()
            out("203 OK LOGLEVEL SET")

        elif line.startswith("QUIT"):
            # Answer first: speechd blocks its whole shutdown waiting for this
            # line, and a daemon stuck mid-shutdown still owns the socket, so
            # the next client hangs instead of autospawning a fresh one.
            out("210 OK QUIT")
            speaker.stop()
            # Exit promptly. speechd waits for the module *process* to end
            # before it finishes closing it, so lingering here — even to drain
            # stdin — hangs the daemon's shutdown outright.
            return 0

        else:
            out("300 ERR UNKNOWN COMMAND")
    return 0


if __name__ == "__main__":
    sys.exit(main())
