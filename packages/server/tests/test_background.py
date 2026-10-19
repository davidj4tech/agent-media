"""Background work in a thread whose turn is over (background.py, §6.1).

`/sessions/state` rows and the thread's `state` carry `background:
{"agents", "commands"}` while a `waiting` thread still has work running:
a pane thread's running subagents (its transcript), a headless thread's
sessiond tasks. `state` itself stays `waiting`. No real session is read: the
transcripts are synthetic (test_agents.Thread, under the conftest's
throwaway CLAUDE_CONFIG_DIR) and sessiond is a fake driver's rows.
"""

from __future__ import annotations

import pytest

from agent_media_server import background, driver, sessions, thread_events

from test_agents import Thread, note
from test_transcript import SID


@pytest.fixture()
def th(tmp_path):
    return Thread(tmp_path)


@pytest.fixture()
def pane(monkeypatch):
    """SID live in a pane, its screen saying `state["now"]`."""
    state = {"now": "waiting"}
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    monkeypatch.setattr(sessions, "_pane_titles", lambda: {"%7": "Pane thread"})
    monkeypatch.setattr(sessions, "activity_of", lambda s, p: {"state": state["now"]})
    monkeypatch.delenv("MEDIA_HEADLESS", raising=False)
    return state


def _row(sid=SID):
    return next(r for r in sessions._live_states() if r["session"] == sid)


def test_tasks_are_agents_or_commands_by_their_kind():
    assert background.of_tasks([]) is None and background.of_tasks(None) is None
    assert background.of_tasks([{"id": "a", "type": "local_agent"},
                                {"id": "b", "type": "local_bash"},
                                {"id": "c", "type": "local_agent"},
                                {"id": "d", "type": ""}]) == {"agents": 3, "commands": 1}
    assert background.of_tasks([{"id": "b", "type": "local_bash"}]) == {"agents": 0, "commands": 1}


def test_a_pane_thread_waiting_on_a_running_subagent_says_so(th, pane):
    th.main.prompt("Map the clients")
    a = th.spawn("a0000000101", "toolu_A", "Map the clients")
    a.prompt("Do it.")
    a.tool("Bash", {"command": "ls", "description": "List"}, "s1")
    th.main.text("Started an agent; it will report back.", msgid="m1")
    th.main.end_turn()
    row = _row()
    # The turn is over: still `waiting`, so a reply notice fires as ever.
    assert row["state"] == "waiting"
    assert row["background"] == {"agents": 1, "commands": 0}
    st = thread_events.session_state(SID)
    assert st["state"] == "waiting" and st["background"] == {"agents": 1, "commands": 0}
    # While a turn runs the flag is not there: working says it all.
    pane["now"] = "working"
    assert "background" not in _row()
    assert thread_events.session_state(SID)["background"] is None
    # The agent reports back: nothing runs, the thread is plainly waiting.
    pane["now"] = "waiting"
    th.main.t = a.t + 1
    th.main.prompt(note("a0000000101", "toolu_A", "completed"))
    assert "background" not in _row()
    assert thread_events.session_state(SID)["background"] is None


def test_a_pane_thread_with_no_subagents_has_no_flag(th, pane):
    th.main.prompt("Hello")
    th.main.text("Hi.", msgid="m0")
    th.main.end_turn()
    assert "background" not in _row()


class FakeHeadless:
    """sessiond as the driver sees it: its `list` rows and one view."""

    def __init__(self, sid, tasks, state="waiting"):
        self.v = {"session": sid, "live": True, "state": state, "pid": None, "agent": "claude",
                  "first_text": "Run it", "pending": [], "queued": [],
                  "tasks": len(tasks), "task_list": tasks}

    def rows(self):
        return [self.v]

    def view(self, session):
        from agent_media_server.driver.headless import contract_state

        return {"state": contract_state(self.v), "live": True, "pane": None, "approval": None,
                "pid": None, "raw": self.v["state"], "tasks": self.v["tasks"],
                "task_list": self.v["task_list"], "queued": False}


def test_a_headless_thread_counts_sessiond_tasks(monkeypatch):
    sid = "11111111-2222-3333-4444-555555555555"
    fake = FakeHeadless(sid, [{"id": "t1", "type": "local_agent", "description": "Docs"},
                              {"id": "t2", "type": "local_bash", "description": "npm test"},
                              {"id": "t3", "type": "local_agent", "description": "Tests"}])
    monkeypatch.setenv("MEDIA_HEADLESS", "1")
    monkeypatch.setattr(driver, "headless_driver", lambda: fake)
    monkeypatch.setattr(driver, "owned_headless", lambda s: s == sid)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    row = _row(sid)
    assert row["state"] == "waiting" and row["driver"] == "headless"
    assert row["background"] == {"agents": 2, "commands": 1}
    st = thread_events.session_state(sid)
    assert st["state"] == "waiting" and st["background"] == {"agents": 2, "commands": 1}
    # A turn under way: no flag.
    fake.v["state"] = "working"
    assert "background" not in _row(sid)
    assert thread_events.session_state(sid)["background"] is None
    # The tasks over: plainly waiting.
    fake.v.update(state="waiting", tasks=0, task_list=[])
    assert "background" not in _row(sid)
    assert thread_events.session_state(sid)["background"] is None
