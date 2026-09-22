"""A thread's background agents (agents.py, server-contract.md §6.12).

Every transcript here is a small synthetic file written by the test, in the
shapes Claude Code writes: the thread's `<session>.jsonl` with its Agent
calls, results and `<task-notification>`s, and beside it
`<session>/subagents/agent-<id>.jsonl` (sidechain records) plus
`agent-<id>.meta.json`. None is read from a real transcript: the conftest
points CLAUDE_CONFIG_DIR at a throwaway dir.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from agent_media_server import agents as A
from agent_media_server import sessions, thread_events

from test_contract import AUTH, call, server, shelf, signed_in, typed  # noqa: F401
from test_transcript import SID, Script

T0 = 1_790_000_000.0


class Side(Script):
    """A subagent's own transcript: every record a sidechain one."""

    def __init__(self, path, agent_id):
        self.agent_id = agent_id
        super().__init__(path)

    def base(self, kind, **kw):
        rec = super().base(kind, **kw)
        rec["isSidechain"] = True
        rec["agentId"] = self.agent_id
        return rec


def note(task_id, tool_id, status, name="x"):
    return (f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            f"<tool-use-id>{tool_id}</tool-use-id>\n<output-file>/tmp/{task_id}.output</output-file>\n"
            f"<status>{status}</status>\n<summary>Agent \"{name}\" finished</summary>\n"
            f"<result>Report for {name}. <status>failed</status> is only words here.</result>\n"
            f"</task-notification>")


LAUNCHED = [{"type": "text", "text": "Async agent launched successfully.\nagentId: x"}]


class Thread:
    """A thread with its subagents directory."""

    def __init__(self, root):
        self.dir = root / "claude" / "projects" / "-w"
        self.main = Script(self.dir / f"{SID}.jsonl")
        self.subs = self.dir / SID / "subagents"
        self.main.t = T0

    def spawn(self, agent_id, tid, desc, *, background=True, parent=None, fork=False,
              where=None, kind="general-purpose"):
        """The Agent call (in `where`: the thread, or a parent agent's file),
        its launch result, the meta file and the agent's own first record."""
        s = where or self.main
        s.tool("Agent", {"description": desc, "prompt": "Do it.", "subagent_type": kind}, tid,
               msgid=f"call-{tid}")
        if background:
            s.result(tid, LAUNCHED, tur={"isAsync": True, "status": "async_launched",
                                         "agentId": agent_id})
        meta = {"agentType": "fork" if fork else kind, "description": desc, "toolUseId": tid,
                "spawnDepth": 2 if parent else 1, "requestShape": "background"}
        if fork:
            meta["isFork"] = True
        if parent:
            meta["parentAgentId"] = parent
        self.subs.mkdir(parents=True, exist_ok=True)
        (self.subs / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        side = Side(self.subs / f"agent-{agent_id}.jsonl", agent_id)
        side.t = s.t
        return side


def rows(th, live=True):
    got = A.agents(SID, live=live)
    assert got is not None
    return {r["id"]: r for r in got}


@pytest.fixture()
def th(tmp_path):
    return Thread(tmp_path)


# --- status ---------------------------------------------------------------------------


def test_a_finished_background_agent_is_done_on_its_notification(th):
    th.main.prompt("Map the clients")
    a = th.spawn("a1b2c3d4e5", "toolu_A", "Map the clients")
    started = th.main.t
    a.prompt("Do it.")
    a.tool("Bash", {"command": "ls", "description": "List the work dir"}, "s1")
    a.result("s1", "x")
    a.tool("Read", {"file_path": "/w/app.py"}, "s2")
    a.result("s2", "a\nb")
    a.text("Mapped.", stop="end_turn")
    th.main.t = a.t + 1
    th.main.prompt(note("a1b2c3d4e5", "toolu_A", "completed", "Map the clients"))
    r = rows(th)["a1b2c3d4e5"]
    assert r == {"id": "a1b2c3d4e5", "description": "Map the clients",
                 "agent_type": "general-purpose", "is_fork": False, "parent_id": None,
                 "depth": 1, "started_at": pytest.approx(started - 1),
                 "ended_at": pytest.approx(th.main.t), "status": "done", "current_step": None,
                 "steps": 2, "last_at": pytest.approx(a.t)}


def test_running_while_the_file_grows_and_the_session_is_live(th):
    a = th.spawn("a0000000001", "toolu_A", "Build it")
    a.prompt("Do it.")
    a.tool("Bash", {"command": "make", "description": "Build the app"}, "s1")
    r = rows(th)["a0000000001"]
    assert r["status"] == "running" and r["ended_at"] is None
    assert r["current_step"] == "Build the app" and r["steps"] == 1
    # The session ended: a background agent dies with it.
    r = rows(th, live=False)["a0000000001"]
    assert r["status"] == "stopped" and r["current_step"] is None
    assert r["ended_at"] == pytest.approx(a.t)


def test_quiet_past_the_stale_window_is_stopped(th, monkeypatch):
    a = th.spawn("a0000000002", "toolu_A", "Wait forever")
    a.prompt("Do it.")
    old = time.time() - A.STALE_S - 60
    os.utime(a.path, (old, old))
    assert rows(th)["a0000000002"]["status"] == "stopped"
    monkeypatch.setenv("MEDIA_AGENTS_STALE_S", str(A.STALE_S * 10))
    assert rows(th)["a0000000002"]["status"] == "running"


@pytest.mark.parametrize("status,want", [("failed", "failed"), ("killed", "stopped"),
                                         ("completed", "done")])
def test_notification_statuses(th, status, want):
    a = th.spawn("a0000000003", "toolu_A", "Try")
    a.prompt("Do it.")
    th.main.t = a.t + 1
    th.main.prompt(note("a0000000003", "toolu_A", status))
    assert rows(th)["a0000000003"]["status"] == want


def test_a_foreground_agent_ends_with_its_own_result(th):
    a = th.spawn("a0000000004", "toolu_A", "Look", background=False)
    a.prompt("Do it.")
    a.text("Found it.", stop="end_turn")
    th.main.t = a.t + 1
    assert rows(th)["a0000000004"]["status"] == "running"
    th.main.result("toolu_A", [{"type": "text", "text": "Found it."}])
    r = rows(th)["a0000000004"]
    assert r["status"] == "done" and r["ended_at"] == pytest.approx(th.main.t)
    b = th.spawn("a0000000005", "toolu_B", "Break", background=False)
    b.prompt("Do it.")
    th.main.t = b.t + 1
    th.main.result("toolu_B", "Agent failed: boom", is_error=True)
    assert rows(th)["a0000000005"]["status"] == "failed"
    c = th.spawn("a0000000006", "toolu_C", "Cut", background=False)
    c.prompt("Do it.")
    th.main.t = c.t + 1
    th.main.result("toolu_C", "[Request interrupted by user for tool use]", is_error=True)
    assert rows(th)["a0000000006"]["status"] == "stopped"


def test_resumed_after_its_notification_is_running_again(th):
    a = th.spawn("a0000000007", "toolu_A", "Again")
    a.prompt("Do it.")
    th.main.t = a.t + 1
    th.main.prompt(note("a0000000007", "toolu_A", "completed"))
    assert rows(th)["a0000000007"]["status"] == "done"
    a.t = th.main.t + A.GRACE_S + 10         # sent another message later
    a.prompt("One more thing.")
    a.tool("Bash", {"command": "make", "description": "Rebuild"}, "s9")
    r = rows(th)["a0000000007"]
    assert r["status"] == "running" and r["current_step"] == "Rebuild"
    # … and when it went quiet without a second notification, the last word
    # stands once the session has gone.
    assert rows(th, live=False)["a0000000007"]["status"] == "done"


def test_a_quoted_notification_is_not_one(th):
    a = th.spawn("a0000000008", "toolu_A", "Quoted")
    a.prompt("Do it.")
    th.main.t = a.t + 1
    th.main.prompt("Look at this: " + note("a0000000008", "toolu_A", "completed"))
    assert rows(th)["a0000000008"]["status"] == "running"


def test_a_notification_as_a_queue_operation_or_queued_command(th):
    a = th.spawn("a0000000009", "toolu_A", "Queued")
    a.prompt("Do it.")
    th.main.t = a.t + 1
    th.main.write({"type": "queue-operation", "operation": "enqueue",
                   "timestamp": "2026-09-21T22:12:07.566Z", "sessionId": SID,
                   "content": note("a0000000009", "toolu_A", "failed")})
    assert rows(th)["a0000000009"]["status"] == "failed"


def test_a_fork_nests_under_its_parent_and_its_copied_turns_are_not_its_own(th):
    th.main.prompt("Split the work")
    p = th.spawn("a00000000p1", "toolu_P", "Parent")
    p.prompt("Do it.")
    p.tool("Bash", {"command": "ls", "description": "Look around"}, "p1")
    p.result("p1", "x")
    child = th.spawn("a00000000c1", "toolu_C", "Child half", parent="a00000000p1", fork=True,
                     where=p)
    called = p.t - 1          # the call record, before the launch result
    # A fork's file opens with a copy of its parent's turns, at their times.
    child.t = T0
    child.prompt("Split the work")
    child.tool("Bash", {"command": "ls", "description": "Look around"}, "p1")
    child.result("p1", "x")
    child.t = p.t + 5
    child.prompt("You are the APP half.")
    child.tool("Edit", {"file_path": "/w/a.ts", "old_string": "a", "new_string": "b"}, "c1")
    child.result("c1", "ok")
    child.text("Done.", stop="end_turn")
    # The child's notification lands in its parent's file, as a queued command.
    p.t = child.t + 1
    p.write(p.base("attachment", attachment={
        "type": "queued_command", "prompt": note("a00000000c1", "toolu_C", "completed")}))
    got = rows(th)
    c = got["a00000000c1"]
    assert (c["parent_id"], c["depth"], c["is_fork"], c["agent_type"]) == \
        ("a00000000p1", 2, True, "fork")
    assert c["started_at"] == pytest.approx(called)
    assert c["steps"] == 1 and c["status"] == "done"
    assert got["a00000000p1"]["status"] == "running"
    ok, log = A.agent_log(SID, "a00000000c1", limit=60)
    assert ok
    assert [m["role"] for m in log["messages"]] == ["user", "assistant"]
    assert log["messages"][0]["parts"][0]["text"] == "You are the APP half."
    assert log["older"] is False


def test_no_subagents_is_an_empty_list_and_no_transcript_is_none(th):
    th.main.prompt("Hello")
    assert A.agents(SID, live=True) == []
    assert A.agents("0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0", live=True) is None
    assert A.counts([]) == {"running": 0, "total": 0}


def test_growth_is_read_incrementally(th):
    a = th.spawn("a0000000010", "toolu_A", "Grow")
    a.prompt("Do it.")
    assert rows(th)["a0000000010"]["steps"] == 0
    scan = A._SCANS[str(a.path)]
    a.tool("Bash", {"command": "one", "description": "Step one"}, "s1")
    a.result("s1", "x")
    a.tool("Bash", {"command": "two", "description": "Step two"}, "s2")
    r = rows(th)["a0000000010"]
    assert A._SCANS[str(a.path)] is scan
    assert r["steps"] == 2 and r["current_step"] == "Step two"
    # Rewritten in place: read again from scratch.
    a.path.write_bytes(b"")
    a.prompt("Do it.")
    assert rows(th)["a0000000010"]["steps"] == 0


# --- the log --------------------------------------------------------------------------


def test_the_log_is_the_agents_turns_read_only(th, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    a = th.spawn("a0000000011", "toolu_A", "Log me")
    first = a.prompt("Find the route table.")
    a.thinking("Reading app.py next.")
    a.tool("Read", {"file_path": "/w/app.py"}, "s1")
    a.result("s1", "line\n" * 40)
    a.text("It is in app.py. [[visual: a table]]", stop="end_turn")
    before = open(a.path, "rb").read()
    ok, log = A.agent_log(SID, "a0000000011", limit=60)
    assert ok and set(log) == {"session", "agent", "messages", "older"}
    assert log["agent"]["id"] == "a0000000011" and log["agent"]["status"] == "running"
    you, agent = log["messages"]
    assert you["id"] == first["uuid"] and you["spoken"] is None
    assert [p["type"] for p in agent["parts"]] == ["reasoning", "tool", "text"]
    assert agent["parts"][1]["result_summary"] == "40 lines"
    assert agent["parts"][2]["text"] == "It is in app.py."
    assert open(a.path, "rb").read() == before          # read-only
    # Ended: nothing in it is still running.
    th.main.t = a.t + 1
    th.main.prompt(note("a0000000011", "toolu_A", "completed"))
    ok, log = A.agent_log(SID, "a0000000011", limit=60)
    assert log["agent"]["status"] == "done"
    assert not any(m["turn"]["running"] for m in log["messages"])


def test_the_log_pages_back(th):
    a = th.spawn("a0000000012", "toolu_A", "Pages")
    for i in range(5):
        a.prompt(f"Step {i}")
        a.text(f"Did {i}.", stop="end_turn")
    ok, log = A.agent_log(SID, "a0000000012", limit=4)
    assert ok and log["older"] is True
    assert [m["parts"][0]["text"] for m in log["messages"]] == ["Step 3", "Did 3.", "Step 4",
                                                                "Did 4."]
    ok, back = A.agent_log(SID, "a0000000012", limit=4, before=log["messages"][0]["id"])
    assert [m["parts"][0]["text"] for m in back["messages"]] == ["Step 1", "Did 1.", "Step 2",
                                                                 "Did 2."]


def test_the_log_of_an_unknown_or_bad_agent_is_404(th):
    th.main.prompt("Hello")
    assert A.agent_log(SID, "a0000000099", limit=10) == \
        (False, {"error": "no such agent", "status": 404})
    assert A.agent_log(SID, "../../etc", limit=10)[0] is False


# --- routes ---------------------------------------------------------------------------


ROW_KEYS = {"id", "description", "agent_type", "is_fork", "parent_id", "depth", "started_at",
            "ended_at", "status", "current_step", "steps", "last_at"}


def test_agents_route_shape(server, shelf, signed_in, th, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    a = th.spawn("a0000000020", "toolu_A", "Route")
    a.prompt("Do it.")
    a.tool("Bash", {"command": "make", "description": "Build"}, "s1")
    b = th.spawn("a0000000021", "toolu_B", "Done one")
    b.prompt("Do it.")
    th.main.t = b.t + 1
    th.main.prompt(note("a0000000021", "toolu_B", "completed"))
    res, obj = call(server, "GET", f"/threads/{SID}/agents", headers=AUTH)
    assert res.status == 200, obj
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    assert set(obj) == {"ok", "session", "counts", "agents"}
    assert obj["counts"] == {"running": 1, "total": 2}
    assert [r["id"] for r in obj["agents"]] == ["a0000000020", "a0000000021"]
    assert all(set(r) == ROW_KEYS for r in obj["agents"])

    res, obj = call(server, "GET", f"/threads/{SID}/agents/a0000000020/log?limit=1",
                    headers=AUTH)
    assert res.status == 200, obj
    assert set(obj) == {"ok", "session", "agent", "messages", "older"}
    assert len(obj["messages"]) == 1 and obj["older"] is True
    assert set(obj["agent"]) == ROW_KEYS


def test_the_log_route_is_gated(server, th, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    a = th.spawn("a0000000040", "toolu_A", "Gated")
    a.prompt("Do it.")
    res, obj = call(server, "GET", f"/threads/{SID}/agents/a0000000040/log")
    assert res.status == 401 and obj["ok"] is False


def test_agents_route_refusals(server, shelf, signed_in, th, monkeypatch):
    res, obj = call(server, "GET", "/threads/not-a-session/agents", headers=AUTH)
    assert res.status == 400 and obj["error"] == "not a session id"
    th.main.prompt("Hello")
    res, obj = call(server, "GET", f"/threads/{SID}/agents/a0000000099/log", headers=AUTH)
    assert res.status == 404 and obj == {"ok": False, "error": "no such agent"}
    monkeypatch.setattr(sessions, "session_exists", lambda s: False)
    res, obj = call(server, "GET", "/threads/0f1e2d3c-4b5a-4968-8776-000000000000/agents",
                    headers=AUTH)
    assert res.status == 404 and obj["error"] == "no such session"


# --- the stream -----------------------------------------------------------------------


def test_the_snapshot_counts_agents_and_an_event_says_when_they_change(
        server, shelf, signed_in, th, monkeypatch):
    from test_thread_events import Stream

    for k, v in {"POLL_S": 0.02, "DEBOUNCE_S": 0.03, "DEBOUNCE_MAX_S": 0.2, "FAST_S": 0.1,
                 "SLOW_S": 0.1, "PING_S": 0.3}.items():
        monkeypatch.setattr(thread_events, k, v)
    from agent_media_core import activity, book_tracks

    monkeypatch.setattr(book_tracks, "conversation_log",
                        lambda s, f, target=None, positions=True: [])
    monkeypatch.setattr(activity, "attach", lambda s, ls: None)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    th.main.prompt("Hello")
    th.main.text("Hi.", msgid="m0")
    th.main.end_turn()
    st = Stream(server, f"/threads/{SID}/events", AUTH)
    ev, snap = st.event()
    assert ev == "snapshot"
    assert snap["agents"] == {"running": 0, "total": 0}
    # Where the thread is (§6.1): the transcript's cwd, and the series its
    # shelf folder is filed under.
    assert snap["cwd"] == "/w" and snap["project"] == "p-agent-media"
    a = th.spawn("a0000000030", "toolu_A", "Streamed")
    a.prompt("Do it.")
    assert st.next("agents") == {"running": 1, "total": 1}
    th.main.t = a.t + 1
    th.main.prompt(note("a0000000030", "toolu_A", "completed"))
    assert st.next("agents") == {"running": 0, "total": 1}
    st.close()


# --- the project line (§6.1) ----------------------------------------------------------


def test_project_of_follows_the_layout(monkeypatch):
    home = os.path.expanduser("~/projects")
    assert sessions.project_of(f"{home}/agent-media") == "p-agent-media"
    assert sessions.project_of(f"{home}/agent-media/.claude/worktrees/x") == "p-agent-media"
    assert sessions.project_of("/tmp/x", "/lib/Conversations/p-music/A title") == "p-music"
    assert sessions.project_of("/tmp/x") is None
    assert sessions.project_of("") is None
    monkeypatch.setenv("MEDIA_LAYOUT", "default")
    assert sessions.project_of(f"{home}/agent-media") == "agent-media"
    assert sessions.project_of("/srv/site") == "site"
    assert sessions.project_of("") is None
