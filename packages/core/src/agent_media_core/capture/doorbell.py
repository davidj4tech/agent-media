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

The second announcement goes to Cece's relay mailbox, and it is hers by
request: the notification needs David to be near his phone and the spoken
question needs him in the room, but the mailbox is the one path that survives
her not being active — she finds it on her next check whether or not anyone
mentioned it. It is one-way, so unlike the notification there is nothing to
take back down; instead the message states its own deadline, which makes it
self-invalidating rather than needing a second row to retract it.

Off with MEDIA_CONVERSE_NOTIFY=0 (phone) and MEDIA_CONVERSE_MAILBOX="" (relay).
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


def _relay_msg_cmd() -> list[str] | None:
    """How to invoke relay-msg, or None if it isn't installed here.

    PATH first (`~/.local/bin/relay-msg` on red5), then the checkout, because
    converse can run from a systemd unit whose PATH is minimal — the failure
    mode this avoids is a doorbell that works interactively and silently does
    nothing as a service.
    """
    found = shutil.which("relay-msg")
    if found:
        return [found]
    fallback = Path.home() / "projects" / "tmux-relay" / "relay-msg.sh"
    return [str(fallback)] if fallback.is_file() else None


def post(question: str, timeout_s: float) -> None:
    """Drop the question in the answerer's relay mailbox. Fire and forget.

    `--from` is passed explicitly. relay-msg otherwise takes the sender from
    the box configured on the host, which is us either way — but stating it
    keeps the reply threading to the right box when the answer comes back.
    """
    box = os.environ.get("MEDIA_CONVERSE_MAILBOX", "cece").strip()
    if not box or not question.strip():
        return
    cmd = _relay_msg_cmd()
    if cmd is None:
        log.info("converse doorbell: relay-msg not installed — no mailbox drop")
        return
    body = (
        f"Sam is waiting on an answer, asked just now:\n\n"
        f"{question.strip()}\n\n"
        f"Answer with: media converse-reply \"<your answer>\"\n"
        f"It expires {timeout_s:.0f}s after it was asked — if that has passed, "
        f"the rendezvous is gone and converse-reply will exit 3. Check with "
        f"media converse-reply --pending before answering a stale one."
    )
    sender = os.environ.get("MEDIA_CONVERSE_MAILBOX_FROM", "sam")
    argv = [*cmd, "--from", sender, "--to", box, body]
    threading.Thread(
        target=lambda: _run(argv), daemon=True).start()


def _run(argv: list[str]) -> bool:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=_TIMEOUT_S,
                           check=False)
        if r.returncode != 0:
            log.warning("converse doorbell: relay-msg exit %d: %s",
                        r.returncode, r.stderr.decode(errors="replace")[:200])
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("converse doorbell: relay-msg failed: %s", e)
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
