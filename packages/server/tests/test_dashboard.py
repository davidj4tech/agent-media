"""`GET /dashboard` (server-contract.md §6.11): the home screen in one answer.

Over real HTTP with the contract rig. The pane sweep, the shelf, speech, the
machine (systemctl, tailscale) and the harness lookup are fakes; the activity
file and the reaper log are written under the throwaway state dir (conftest).
Key sets are pinned exactly, as test_contract does.
"""

from __future__ import annotations

import json
import types

import pytest

from agent_media_server import asks, dashboard, panes, reap, sessions, speech

from test_contract import (AUTH, SID, SID2, call, keys, server, shelf,  # noqa: F401
                           signed_in, typed)

TOP = {"ok", "at", "needs_you", "working", "speech", "recent", "places", "agents", "hosts", "digests"}
HOST = {"name", "role", "local", "online", "last_seen", "sessions", "mem_used_mb",
        "mem_total_mb", "mem_available_mb", "sessions_mem_mb", "tight", "reaper",
        "shell", "sessiond"}
TAILSCALE = {"Self": {"HostName": "red5"},
             "Peer": {"k1": {"HostName": "hpo", "DNSName": "hpo.example.ts.net.",
                             "Online": False, "LastSeen": "2026-09-21T14:20:00.1Z"},
                      "k2": {"HostName": "red4", "Online": True,
                             "LastSeen": "0001-01-01T00:00:00Z"}}}
DIALOG = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"


@pytest.fixture()
def machine(monkeypatch, tmp_path):
    """systemctl says sasonica-shell is up and sessiond is down; tailscale
    knows hpo (offline); every harness but hermes is installed; /proc/meminfo
    is tight."""
    calls: list = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[0] == "systemctl":
            word = "active" if argv[-1] == "sasonica-shell" else "inactive"
            return types.SimpleNamespace(stdout=word + "\n", returncode=0)
        if argv[0] == "tailscale":
            return types.SimpleNamespace(stdout=json.dumps(TAILSCALE), returncode=0)
        raise AssertionError(f"unexpected command {argv}")

    monkeypatch.setattr(dashboard, "_run", run)
    monkeypatch.setattr(dashboard, "_which", lambda name: f"/usr/bin/{name}")
    from agent_media_core import harnesses
    monkeypatch.setattr(harnesses, "program", lambda n: "" if n == "hermes" else f"/bin/{n}")
    proc = tmp_path / "fake-proc"
    proc.mkdir(exist_ok=True)
    (proc / "meminfo").write_text("MemTotal:  8000000 kB\nMemAvailable: 1000000 kB\n")
    monkeypatch.setattr(speech, "current_state", lambda: {
        "speaking": False, "paused": False,
        "queued": [{"session": SID, "urgent": False, "at": 1790000000.5}]})
    dashboard._reset_for_tests()
    yield calls
    dashboard._reset_for_tests()


def _activity(session: str, rows: list[dict]) -> None:
    from agent_media_core import activity

    d = activity.activity_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def _reap_log(text: str) -> None:
    p = reap.log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_shape_while_working(server, shelf, signed_in, machine):
    _activity(SID2, [{"at": 1790000000.0, "kind": "turn"},
                     {"at": 1790000001.0, "kind": "step", "text": "Read canvas.py"},
                     {"at": 1790000002.0, "kind": "step", "text": "Run the tests"}])
    _reap_log(
        '2026-09-22T14:45:00+10:00 apply closed aaaaaaaa "old" idle=13.0h/12h\n'
        '2026-09-22T15:00:06+10:00 apply kept 01a0c709 "x" idle=2.0h/12h reason=working\n'
        '2026-09-22T15:00:06+10:00 apply closed 5f8ca313 "y" idle=13.0h/12h why=idle\n'
        '2026-09-22T15:00:06+10:00 apply closed cb51c3c5 "z" idle=14.0h/12h why=idle\n')
    res, obj = call(server, "GET", "/dashboard", headers=AUTH)
    assert res.status == 200, obj
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    assert keys(obj) == TOP
    assert obj["needs_you"] == []
    assert obj["working"] == [{"session": SID2, "title": "Sasonica web",
                               "current": "Run the tests", "since": 1790000000.0, "count": 2,
                               # The small project line (§6.1): unknown here.
                               "project": None, "cwd": None}]
    assert keys(obj["speech"]) == {"now", "queued"}
    assert keys(obj["speech"]["now"]) == {"live", "speaking", "paused", "session", "title",
                                          "sentence", "target", "replay"}
    assert obj["speech"]["now"]["live"] is False
    assert obj["speech"]["queued"] == [{"session": SID, "title": "Sasonica music",
                                        "urgent": False, "at": 1790000000.5}]
    recent = {r["session"]: r for r in obj["recent"]}
    assert set(recent) == {SID, SID2}
    for r in obj["recent"]:
        assert keys(r) == {"session", "title", "recap", "at", "live", "rested", "project", "cwd"}
    assert recent[SID2]["live"] is True and recent[SID]["live"] is False
    # The shelved one is filed under a series: that is its project.
    assert recent[SID]["project"] == "p-agent-media" and recent[SID2]["project"] is None
    assert [keys(p) for p in obj["places"]] == [{"name", "path", "at"}]
    assert obj["agents"] == [{"name": "claude", "present": True},
                             {"name": "codex", "present": True},
                             {"name": "pi", "present": True},
                             {"name": "hermes", "present": False},
                             {"name": "opencode", "present": True}]
    red5, hpo = obj["hosts"]
    assert keys(red5) == HOST and keys(hpo) == HOST
    assert red5["local"] is True and red5["online"] is True and red5["sessions"] == 1
    assert (red5["mem_total_mb"], red5["mem_available_mb"], red5["mem_used_mb"]) == (7812, 977, 6835)
    assert red5["tight"] is True
    assert red5["reaper"] == {"mode": "apply", "closed_last_run": 2,
                              "last_run_at": 1790053206.0}
    assert red5["shell"] == {"service": "sasonica-shell", "active": True}
    assert red5["sessiond"] == {"service": "agent-media-sessiond", "active": False}
    assert hpo == {"name": "hpo", "role": "peer", "local": False, "online": False,
                   "last_seen": 1790000400.1, "sessions": None, "mem_used_mb": None,
                   "mem_total_mb": None, "mem_available_mb": None, "sessions_mem_mb": None,
                   "tight": None, "reaper": None, "shell": None, "sessiond": None}


def test_a_permission_prompt_needs_you_with_its_approval(server, shelf, signed_in, machine,
                                                          monkeypatch):
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "approval")
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: DIALOG)
    monkeypatch.setattr(asks, "approval", lambda cap, session, agent: None)
    _, obj = call(server, "GET", "/dashboard", headers=AUTH)
    assert obj["working"] == []
    [row] = obj["needs_you"]
    assert keys(row) == {"session", "title", "kind", "approval", "project", "cwd"}
    assert (row["session"], row["title"], row["kind"]) == (SID2, "Sasonica web", "approval")
    ap = row["approval"]
    assert ap["question"].endswith("Do you want to proceed?")
    assert [o["label"] for o in ap["options"]] == ["Yes", "No"]
    assert ap["key"] and ap["agent"] == "claude"


def test_a_question_is_kind_question(server, shelf, signed_in, machine, monkeypatch):
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "approval")
    q = {"question": "Which pets?", "partial": False, "options": [], "key": "k1",
         "agent": "claude", "kind": "question", "multiSelect": True, "free_text": True,
         "questions": [{"question": "Which pets?", "header": "Pets", "multiSelect": True,
                        "free_text": True, "options": []}],
         "current": 0, "review": False, "tool_use_id": "toolu_1", "source": "hook"}
    monkeypatch.setattr(asks, "approval", lambda cap, session, agent: q)
    _, obj = call(server, "GET", "/dashboard", headers=AUTH)
    [row] = obj["needs_you"]
    assert row["kind"] == "question" and row["approval"] == q


def test_a_dialog_gone_by_the_time_it_is_read_is_not_listed(server, shelf, signed_in, machine,
                                                            monkeypatch):
    # The sweep said approval, the capture no longer shows a list.
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "approval")
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: "")
    _, obj = call(server, "GET", "/dashboard", headers=AUTH)
    assert obj["needs_you"] == [] and obj["working"] == []


def test_archived_threads_leave_recent(server, shelf, signed_in, machine):
    call(server, "POST", "/session/archive", {"session": SID, "archived": True}, AUTH)
    dashboard._reset_for_tests()
    _, obj = call(server, "GET", "/dashboard", headers=AUTH)
    assert [r["session"] for r in obj["recent"]] == [SID2]


def test_machine_part_is_cached(server, shelf, signed_in, machine):
    call(server, "GET", "/dashboard", headers=AUTH)
    n = len(machine)
    assert n == 3                   # two is-active, one tailscale status
    call(server, "GET", "/dashboard", headers=AUTH)
    assert len(machine) == n        # inside the TTL: nothing run again


def test_no_tailscale_and_no_systemctl_is_null_not_an_error(server, shelf, signed_in, machine,
                                                            monkeypatch):
    monkeypatch.setattr(dashboard, "_which", lambda name: None)
    monkeypatch.setenv("MEDIA_DASHBOARD_PEERS", "hpo, pn")
    _, obj = call(server, "GET", "/dashboard", headers=AUTH)
    red5, hpo, pn = obj["hosts"]
    assert red5["shell"]["active"] is None and red5["sessiond"]["active"] is None
    assert red5["reaper"] == {"mode": None, "last_run_at": None, "closed_last_run": 0}
    assert (hpo["name"], hpo["online"], pn["name"], pn["online"]) == ("hpo", None, "pn", None)


def test_peers_can_be_none(server, shelf, signed_in, machine, monkeypatch):
    monkeypatch.setenv("MEDIA_DASHBOARD_PEERS", "")
    _, obj = call(server, "GET", "/dashboard", headers=AUTH)
    assert [h["name"] for h in obj["hosts"]] == [obj["hosts"][0]["name"]]


# The refusals (401 with no bearer, 503 with ABS down, 403) are test_contract's
# GATED_GETS, which lists /dashboard.


def test_preflight_is_open(server):
    res, _ = call(server, "OPTIONS", "/dashboard")
    assert res.status == 204
    assert res.getheader("Access-Control-Allow-Origin") == "*"


def test_speech_snapshot_is_served_stale_while_it_is_read_again(monkeypatch):
    # The canvas's snapshot is a `media` subprocess: a poll must not wait on it.
    import threading
    import time as _time

    dashboard._reset_for_tests()
    reads: list = []
    gate = threading.Event()

    def state():
        reads.append(1)
        if len(reads) > 1:
            gate.wait(5)
        return {"speaking": len(reads) > 1}

    monkeypatch.setattr(speech, "current_state", state)
    assert dashboard._speech_state() == {"speaking": False}     # nothing yet: waits
    assert dashboard._speech_state() == {"speaking": False}     # fresh: no read
    assert len(reads) == 1
    at, st = dashboard._SPEECH
    monkeypatch.setattr(dashboard, "_SPEECH", (at - dashboard.SPEECH_FRESH_S - 1, st))
    assert dashboard._speech_state() == {"speaking": False}     # stale, served now
    assert dashboard._speech_state() == {"speaking": False}     # one read in flight
    gate.set()
    deadline = _time.monotonic() + 5
    while dashboard._SPEECH[1] == st and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert len(reads) == 2
    assert dashboard._speech_state() == {"speaking": True}
    dashboard._reset_for_tests()
