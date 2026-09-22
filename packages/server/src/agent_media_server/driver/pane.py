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

    def start(self, *, agent, cwd, text, host="", flags=(), quote=""):
        from .. import send

        return send._ask_pane(text, agent=agent, cwd=cwd, host=host, flags=list(flags),
                              quote=quote)

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

    def interrupt(self, session):
        """Escape into a working pane, then watch it stop. Never on a dialog."""
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
            panes._tmux(["send-keys", "-t", pane, "Escape"])
        deadline = time.monotonic() + INTERRUPT_WATCH_S
        while time.monotonic() < deadline:
            time.sleep(0.3)
            now = sessions.activity_of(session, pane).get("state") or "waiting"
            if now != "working":
                return True, {"interrupted": True, "why": None, "state": now, "pane": pane}
        return False, {"error": "still working after Escape", "status": 504,
                       "interrupted": False, "state": "working", "pane": pane}
