"""`POST /threads/{session}/agents/{id}/stop` and `…/message` — acting on
one background agent (server-contract.md §6.12a).

The agents strip (§6.12) was read-only; Claude Code's own agent viewer can
stop one subagent or send it a message, and the app now can too (David, 7
Oct 2026, roadmap item 17). How depends on who holds the session:

* **Stop, headless** → the `stop_task` control request (sessiond.py), the
  one Claude Code's TaskStop tool and its viewer use. The thread's turn goes
  on; the agent's `killed` notification turns its row `stopped`.
* **Message, and stop in a pane** → there is no way in but the main agent:
  a message in the thread asks it to use its own tool (`SendMessage`,
  `TaskStop`). Claude Code 2.1.289 refuses the `send_task_message` control
  request headless, and a pane has only keys. The message is the listener's
  turn like any other: it shows in the thread, and waits behind a turn that
  is running.

The agent must be one of the thread's rows (`agents.agents`), and for a
stop, running: `stop_task` answers success for an id it has never heard of,
so the check is here.
"""

from __future__ import annotations

from . import agents, auth, driver, sessions

#: How long a message to an agent may be.
TEXT_MAX = 4000


def _label(row: dict) -> str:
    desc = " ".join(str(row.get("description") or "").split())[:80]
    return f"{row['id']} (“{desc}”)" if desc else row["id"]


def stop_words(row: dict) -> str:
    return (f"Please stop your background agent {_label(row)} with TaskStop. "
            "I stopped it from the phone; nothing else needs doing.")


def message_words(row: dict, text: str) -> str:
    return (f"Please pass this to your background agent {_label(row)} with "
            f"SendMessage, word for word, then carry on: {text}")


def _row(session: str, agent_id: str, bearer: str) -> tuple[dict | None, dict]:
    if not sessions._SESSION.fullmatch(session or ""):
        return None, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return None, err
    if not agents.AGENT_ID.fullmatch(agent_id or ""):
        return None, {"error": "no such agent", "status": 404}
    row = next((r for r in agents.agents(session) or [] if r["id"] == agent_id), None)
    if row is None:
        return None, {"error": "no such agent", "status": 404}
    return row, {}


def _relay(drv, session: str, words: str) -> tuple[bool, dict]:
    """`words` to the main agent, as the listener's turn. Not into a session
    stopped on a question: the words would be typed into its dialog."""
    st = drv.state(session)
    if not st.get("live"):
        return False, {"error": "the thread is not running", "status": 409}
    if st.get("state") == "approval":
        return False, {"error": "the thread is waiting on a question; answer it first",
                       "status": 409}
    ok, r = drv.send(session, words, words)
    if not ok:
        return False, r
    return True, {"via": "message", "queued": bool(r.get("queued"))}


def agent_stop(session: str, agent_id: str, bearer: str) -> tuple[bool, dict]:
    row, err = _row(session, agent_id, bearer)
    if row is None:
        return False, err
    if row["status"] != "running":
        return False, {"error": f"that agent is not running ({row['status']})",
                       "status": 409, "agent": row}
    drv = driver.for_session(session)
    if drv.kind == driver.HEADLESS:
        ok, r = drv.stop_task(session, agent_id)
        if not ok:
            return False, r
        if not r.get("stopped"):
            return False, {"error": f"not stopped ({r.get('why') or 'no answer'})",
                           "status": 409, "agent": row}
        return True, {"session": session, "agent": row, "via": "stop_task"}
    ok, r = _relay(drv, session, stop_words(row))
    return ok, ({"session": session, "agent": row, **r} if ok else r)


def agent_message(session: str, agent_id: str, text: str, bearer: str) -> tuple[bool, dict]:
    text = " ".join((text or "").split())
    row, err = _row(session, agent_id, bearer)
    if row is None:
        return False, err
    if not text:
        return False, {"error": "say something", "status": 400}
    if len(text) > TEXT_MAX:
        return False, {"error": f"at most {TEXT_MAX} characters", "status": 400}
    # A finished agent is fine: SendMessage resumes it from its transcript.
    ok, r = _relay(driver.for_session(session), session, message_words(row, text))
    return ok, ({"session": session, "agent": row, **r} if ok else r)
