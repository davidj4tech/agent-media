"""`GET /dashboard` (server-contract.md §6.11): the home screen in one answer.

Over real HTTP with the contract rig. The pane sweep, the shelf, speech, the
machine (systemctl, tailscale) and the harness lookup are fakes; the activity
file and the reaper log are written under the throwaway state dir (conftest).
Key sets are pinned exactly, as test_contract does.
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from agent_media_server import asks, dashboard, panes, reap, sessions, speech

from test_contract import (AUTH, SID, SID2, call, keys, server, shelf,  # noqa: F401
                           signed_in, typed)

TOP = {"ok", "at", "needs_you", "working", "speech", "recent", "replies", "places", "agents", "hosts", "digests", "alerts", "signins"}
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


def test_open_alerts_are_on_home_worst_first(monkeypatch):
    from agent_media_server import alerts
    monkeypatch.setattr(alerts, "listing", lambda open_only=False: {"alerts": [
        {"id": "disk.red5.root", "level": "needs", "title": "red5 / full", "detail": "x" * 900,
         "fix": "free space", "host": "red5", "first_seen": 1.0, "changed_at": 2.0, "acked_at": None,
         "kind": "status", "open": True},
        {"id": "host.hpo", "level": "warn", "title": "hpo offline", "detail": "", "fix": "",
         "host": "red5", "first_seen": 1.0, "changed_at": 1.5, "acked_at": 1.6,
         "kind": "status", "open": True}]})
    rows = dashboard._alerts()
    assert [r["id"] for r in rows] == ["disk.red5.root", "host.hpo"]
    real = alerts.listing
    monkeypatch.setattr(alerts, "listing", lambda open_only=False: {"alerts": [
        {**real_row, "id": "shell.signin.red5"} for real_row in [{"level": "needs", "title": "t"}]]})
    assert dashboard._alerts() == [], "a sign-in request is its own card, not an alert"
    assert len(rows[0]["detail"]) == 600, "Home shows a line, not the whole detail"
    assert rows[1]["acked_at"] == 1.6
    monkeypatch.setattr(alerts, "listing", lambda open_only=False: (_ for _ in ()).throw(OSError("no db")))
    assert dashboard._alerts() == []


def _transcript(session: str, records: list[dict]) -> None:
    """A Claude Code transcript under the conftest's throwaway config dir
    (compact JSON: the reader looks for `"type":"assistant"` before parsing)."""
    d = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-w"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.jsonl").write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))


def _said(text: str, n: int) -> dict:
    return {"type": "assistant", "uuid": f"a{n}", "timestamp": "2026-10-03T01:00:00Z",
            "message": {"id": f"m{n}", "role": "assistant", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": text}]}}


def _asked(text: str, n: int) -> dict:
    return {"type": "user", "uuid": f"u{n}", "timestamp": "2026-10-03T00:59:00Z",
            "message": {"role": "user", "content": text}}


def test_replies_are_threads_that_end_on_a_reply(server, shelf, signed_in, machine, monkeypatch):
    # SID ends on a reply; SID2 (live) is working, so it is left out even
    # though its transcript ends on one too.
    _transcript(SID, [_asked("hi", 1), _said("All done.", 2)])
    _transcript(SID2, [_asked("go", 1), _said("Started.", 2)])
    res, obj = call(server, "GET", "/dashboard", headers=AUTH)
    assert res.status == 200, obj
    assert [r["session"] for r in obj["replies"]] == [SID]
    r = obj["replies"][0]
    assert keys(r) == {"session", "title", "at", "text", "live", "project", "cwd"}
    assert r["text"] == "All done." and r["title"] == "Sasonica music" and r["live"] is False
    # A new prompt: no longer a reply waiting to be read.
    _transcript(SID, [_asked("hi", 1), _said("All done.", 2), _asked("more", 3)])
    res, obj = call(server, "GET", "/dashboard", headers=AUTH)
    assert obj["replies"] == []


def test_replies_from_opencode_and_hermes(monkeypatch):
    """No transcript file: opencode's reply comes from its database (stubbed
    here), Hermes's from its newest spoken line — not an alert."""
    import time as _t

    from agent_media_core.state import store
    from agent_media_server import transcript

    now = _t.time()
    oc, hm = "ses_0123456789abcdefABCDEFGHIJ", "20261003_101010_abcdef"
    monkeypatch.setattr(transcript, "file_state", lambda s: (0, 4, now - 60) if s == oc else None)
    monkeypatch.setattr(transcript, "last_reply_opencode",
                        lambda s: {"at": now - 60, "text": "opencode says hi"})

    class Store:
        def recent_history(self, sink=None, limit=20):
            return [{"started_at": now - 5, "text": "Hermes is waiting",
                     "extras": {"source_session": hm, "kind": "notif"}},
                    {"started_at": now - 30, "text": "Hermes [[visual: x]] answered.",
                     "extras": {"source_session": hm}},
                    {"started_at": now - 90, "text": "older", "extras": {"source_session": hm}}]
    monkeypatch.setattr(store, "StateStore", Store)
    dashboard._reset_for_tests()
    rows = dashboard._replies([{"session": oc, "title": "oc"}, {"session": hm, "title": "hm"}], set())
    assert [(r["session"], r["text"]) for r in rows] == [(hm, "Hermes answered."),
                                                         (oc, "opencode says hi")]
    assert dashboard._replies([{"session": hm, "title": "hm"}], {hm}) == []
    dashboard._reset_for_tests()
