"""Announcing a `converse` question to answerers who cannot hear it.

`converse` speaks its question, which is enough when David is listening. It is
nothing at all when he is in another room — and the answerer may not be him:
Cece (Claude live in the Android app) cannot be reached by any push, so the
only route to her runs through him. The question therefore has to survive not
being heard.

So: arm the rendezvous, then put the question in the notification shade, and
take it back down the moment the question is answered or expires. A stale "Sam
is asking" is worse than none — it invites an answer to a question nobody is
waiting for any more.

Termux-only in practice (termux-notification lives on the phone, converse runs
on red5), so this ssh's the way `_miss_notify` does, and inherits its host
resolution — one source of truth for "the phone". Best-effort throughout: a
doorbell that fails must never cost the conversation it was announcing.

The second announcement goes to Cece, and it is hers by request: the
notification needs David to be near his phone and the spoken question needs
him in the room. It used to be a row in tmux-relay's mailbox; since the relay
was retired (2026-09-18) it is `agent-mail-deliver --to cece`, which types it into the
Claude app when the app is in front and otherwise does nothing (--no-notify:
the notification above already carries it). It is one-way, so unlike the
notification there is nothing to take back down; instead the message states
its own deadline, which makes it self-invalidating.

Off with MEDIA_CONVERSE_NOTIFY=0 (phone) and MEDIA_CONVERSE_MAILBOX="" (Cece).
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

from ..sinks._miss_notify import _SSH_OPTS, miss_host


log = logging.getLogger(__name__)

NOTIFY_ID = "converse-question"
_TIMEOUT_S = 20
_DROP_TIMEOUT_S = 90   # agent-mail-deliver --to cece is an adb round trip or two


def _enabled() -> bool:
    return os.environ.get("MEDIA_CONVERSE_NOTIFY", "1") != "0"


def _ssh(remote_argv: list[str], timeout_s: float = _TIMEOUT_S) -> bool:
    # ssh re-splits the remote argv on spaces — quote it as ONE command string
    # or the multi-word title/content shatter into stray arguments.
    remote = " ".join(shlex.quote(a) for a in remote_argv)
    try:
        r = subprocess.run(["ssh", *_SSH_OPTS, miss_host(), remote],
                           capture_output=True, timeout=timeout_s, check=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("converse doorbell: %s", e)
        return False


def _mail_deliver_cmd() -> list[str] | None:
    """How to invoke agent-mail-deliver, or None if it isn't installed here.

    PATH first (`~/.local/bin/agent-mail-deliver` on red5), then the checkout, because
    converse can run from a systemd unit whose PATH is minimal — the failure
    mode this avoids is a doorbell that works interactively and silently does
    nothing as a service.
    """
    found = shutil.which("agent-mail-deliver")
    if found:
        return [found]
    fallback = Path.home() / "projects" / "agent-mail" / "bin" / "agent-mail-deliver"
    return [str(fallback)] if fallback.is_file() else None


def post(question: str, timeout_s: float) -> None:
    """Put the question in front of the answerer (agent-mail-deliver). Fire and forget.

    `--from` is required by agent-mail-deliver, which has no default sender: a default
    is how one assistant's messages came to be labelled as another's. So with
    no MEDIA_CONVERSE_MAILBOX_FROM this does nothing rather than guess.
    """
    # Unset means "nobody to ring", which the guard below already handles.
    # A default box name here would be one household's assistant.
    box = os.environ.get("MEDIA_CONVERSE_MAILBOX", "").strip()
    if not box or not question.strip():
        return
    sender = os.environ.get("MEDIA_CONVERSE_MAILBOX_FROM", "").strip()
    if not sender:
        log.info("converse doorbell: MEDIA_CONVERSE_MAILBOX_FROM unset — no drop")
        return
    cmd = _mail_deliver_cmd()
    if cmd is None:
        log.info("converse doorbell: agent-mail-deliver not installed — no drop")
        return
    body = (
        f"Sam is waiting on an answer, asked just now:\n\n"
        f"{question.strip()}\n\n"
        f"Answer with: media converse-reply \"<your answer>\"\n"
        f"It expires {timeout_s:.0f}s after it was asked — if that has passed, "
        f"the rendezvous is gone and converse-reply will exit 3. Check with "
        f"media converse-reply --pending before answering a stale one."
    )
    argv = [*cmd, "--no-notify", "--from", sender, "--to", box, body]
    # An adb push takes tens of seconds; the thread keeps it off converse.
    threading.Thread(
        target=lambda: _run(argv, timeout_s=_DROP_TIMEOUT_S), daemon=True).start()


def _run(argv: list[str], timeout_s: float = _TIMEOUT_S) -> bool:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout_s,
                           check=False)
        if r.returncode != 0:
            log.warning("converse doorbell: %s exit %d: %s",
                        Path(argv[0]).name, r.returncode,
                        r.stderr.decode(errors="replace")[:200])
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("converse doorbell: %s failed: %s", Path(argv[0]).name, e)
        return False


def ring(question: str, timeout_s: float) -> None:
    """Announce an armed question. Returns immediately; ssh runs in a thread.

    Never blocks converse: the human's answer beats our announcement of the
    question, and a dozing phone can hold an ssh open for the full timeout.
    """
    if not _enabled() or not question.strip():
        return
    content = (f"{question.strip()} — answer within {timeout_s:.0f}s, "
               f"or ask Cece to run: media converse-reply --pending")
    t = threading.Thread(
        target=_ssh,
        args=(["termux-notification", "--id", NOTIFY_ID,
               "--title", "Sam is asking", "--content", content,
               "--priority", "high"],),
        daemon=True)
    t.start()


def clear() -> None:
    """Take the question back down. Synchronous, on a short leash.

    Not a daemon thread like `ring`: converse returning is exactly when a stale
    doorbell becomes misleading, and a caller that exits first would leave it in
    the shade. The wait is bounded well under the ssh default because the call
    it tails has already spent a minute or more waiting — a few seconds more is
    cheap, twenty is not.
    """
    if not _enabled():
        return
    _ssh(["termux-notification-remove", NOTIFY_ID], timeout_s=8)
