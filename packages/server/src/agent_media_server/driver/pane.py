"""The pane driver: today's path, behind the Driver seam, unchanged.

Every method calls the code that answered the route before the seam existed
(`send._deliver_pane`, `send._ask_pane`, `send._answer_pane`, …), through the
module so the tests' fakes still apply. The one new thing is `interrupt`, for
`/session/stop` (server-contract.md §12): Escape, and only while the pane is
working — Escape on a permission prompt means "no", and that decision belongs
to `/session/answer`.
"""

from __future__ import annotations

import time

from . import PANE, Caps

#: How long to watch the pane leave `working` after Escape (§12: "up to 3 s,
#: the way /session/answer verifies").
INTERRUPT_WATCH_S = 3.0
#: Harnesses whose interrupt is Escape. pi and Hermes: TBD (§12).
_ESCAPES = ("claude", "codex")


class PaneDriver:
    kind = PANE
    caps = Caps(interrupt=True)

    def start(self, *, agent, cwd, text, host="", flags=(), quote="", model="", mode=""):
        from .. import send

        flags = list(flags)
        if agent == "claude":
            # Flags, so a new window starts that way before its first word.
            if model:
                flags += ["--model", model]
            if mode == "plan":
                # `--dangerously-skip-permissions` would win over it (measured
                # 26 Sep 2026); the allow- form keeps bypass one shift+tab away.
                flags = ["--allow-dangerously-skip-permissions" if f == "--dangerously-skip-permissions"
                         else f for f in flags] + ["--permission-mode", "plan"]
        return send._ask_pane(text, agent=agent, cwd=cwd, host=host, flags=flags,
                              quote=quote)

    def configure(self, session, *, model=None, mode=None):
        """`/model <alias>` typed in, and shift+tab round to plan mode or off
        it (session_settings.py)."""
        from .. import session_settings

        return session_settings.configure_pane(session, model=model, mode=mode)

    def send(self, session, body, text, *, quote=""):
        from .. import send

        return send._deliver_pane(session, body, text)

    def resume(self, session):
        from .. import send

        return send._resume_pane(session)

    def answer(self, session, request):
        from .. import send

        if request.get("answers") is not None:
            # A question's structured answers: given key by key, checked on
            # the screen as they go (asks.drive). No request id: a pane's
            # question is fingerprinted by its `key`.
            return send._answer_pane(session, 0, str(request.get("key") or ""),
                                     answers=request["answers"])
        if "request_id" in request:
            # Allow/deny by request id is a headless session's; a pane's
            # permission prompt is read off a screen and answered by number.
            return False, {"error": "this session answers by number (choice and key)",
                           "status": 400, "approval": self.approval(session)}
        return send._answer_pane(session, int(request.get("choice") or 0),
                                 str(request.get("key") or ""))

    def close(self, session):
        from .. import send

        return send.close_pane(session)

    def state(self, session):
        from .. import sessions

        pane = sessions.live_sessions().get(session, "")
        if not pane:
            return {"state": "ended", "live": False, "pane": None}
        st = sessions.activity_of(session, pane).get("state") or "waiting"
        return {"state": st, "live": True, "pane": pane}

    def approval(self, session):
        from .. import sessions

        pane = sessions.live_sessions().get(session, "")
        return sessions.approval_for(pane, sessions._agent_of_pane(pane), session) if pane else None

    def interrupt(self, session, drop_queued=False, words=""):
        """Escape into a working pane, then watch it stop. Never on a dialog.

        `drop_queued` (`/session/retract`): Escape alone lets a message typed
        while the turn ran go in as the next turn (measured 27 Sep 2026,
        Claude Code 2.1.283). So first Up, which takes the queue back into
        the composer, then Ctrl-U until `words` (the message taken back) have
        left it — Claude Code only; Codex and herdr panes get the Escape."""
        from .. import panes, sessions

        pane = sessions.live_sessions().get(session, "")
        if not pane or not panes.alive(pane):
            return True, {"interrupted": False, "why": "not live", "state": "ended",
                          "pane": None}
        agent = sessions._agent_of_pane(pane)
        st = sessions.activity_of(session, pane).get("state") or "waiting"
        if st != "working":
            why = "waiting on a question" if st == "approval" else "not working"
            return True, {"interrupted": False, "why": why, "state": st, "pane": pane}
        if agent not in _ESCAPES:
            return True, {"interrupted": False, "why": f"not supported for {agent}",
                          "state": st, "pane": pane}
        if panes.is_herdr(pane):
            panes._run(["herdr", "pane", "send-keys", panes.herdr_pane(pane), "escape"])
        else:
            if drop_queued and agent == "claude":
                _drop_queue(pane, words)
            panes._tmux(["send-keys", "-t", pane, "Escape"])
        deadline = time.monotonic() + INTERRUPT_WATCH_S
        while time.monotonic() < deadline:
            time.sleep(0.3)
            now = sessions.activity_of(session, pane).get("state") or "waiting"
            if now != "working":
                return True, {"interrupted": True, "why": None, "state": now, "pane": pane}
        return False, {"error": "still working after Escape", "status": 504,
                       "interrupted": False, "state": "working", "pane": pane}


#: Claude Code's hint under a turn with messages queued behind it.
_QUEUED_HINT = "Press up to edit queued messages"
#: Ctrl-U deletes one line of the composer; a cap for a queue we cannot see.
_CLEAR_MAX = 40


def _composer(pane: str) -> str:
    """The flattened screen from the composer's ❯ on ("" when there is none)."""
    from .. import panes, sessions

    flat = " ".join(panes.strip_ansi(sessions._capture_pane(pane)).split())
    i = flat.rfind(sessions._PROMPT_GLYPH)
    return flat[i + 1:] if i >= 0 else ""


def _drop_queue(pane: str, words: str) -> None:
    """Take a working Claude pane's queued messages back and delete them.

    Only when the queue hint is on screen: Up in an empty composer with
    nothing queued brings back the last prompt instead. The words are known
    when the queue is the message taken back; each Ctrl-U is checked against
    its first and last words, so a queue of several lines is cleared and an
    empty box is not pressed forty times. Ctrl-U is Claude Code's kill, so
    anything cleared by mistake is one Ctrl-Y from coming back."""
    from .. import panes, sessions

    if _QUEUED_HINT not in " ".join(panes.strip_ansi(sessions._capture_pane(pane)).split()):
        return
    panes._tmux(["send-keys", "-t", pane, "Up"])
    w = " ".join((words or "").split())
    marks = [m for m in (w[:24], w[-24:]) if m.strip()]
    for _ in range(_CLEAR_MAX):
        time.sleep(0.15)
        box = _composer(pane)
        if marks and not any(m in box for m in marks) and "[Pasted text" not in box:
            return
        panes._tmux(["send-keys", "-t", pane, "C-u"])
        if not marks and not box.strip():
            return
