"""Phone notification for a `converse` question awaiting an answer.

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

Off with MEDIA_CONVERSE_NOTIFY=0.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading

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
