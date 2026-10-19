"""Matrix rooms in `GET /search` (search.py's Matrix section, server-contract
§6.14, 17 Oct 2026): what a Room holds is indexed by event id, history is
paged in by `backfill`, and a hit opens in the room by the log's own id.

Every room is a `matrix.Room` over the canned homeserver of
test_matrix_rooms.py — never a real one — and every MATRIX_* variable is
scrubbed, so nothing here reads the host's own token or rooms.
"""

from __future__ import annotations

import os
import threading

import pytest
from agent_media_core import matrix as core_matrix
from agent_media_server import matrix, org, search, sessions, threads
from test_matrix_rooms import ME, MEL, ROOM, SAM, Homeserver, _ev


@pytest.fixture()
def rooms(monkeypatch):
    """No real Matrix config, no memory store, no live thread list; one
    room table this test fills (`add`)."""
    for k in list(os.environ):
        if k.startswith("MATRIX_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MATRIX_SAM_ID", SAM)
    monkeypatch.setattr(sessions, "sessions_index", list)
    monkeypatch.setattr(org, "_memory_call", lambda *a, **k: None)
    monkeypatch.setattr(org, "_memories", lambda q, n: [])
    monkeypatch.setattr(search, "memory_available", lambda: False)
    from agent_media_server import auth

    monkeypatch.setattr(auth, "gate", lambda bearer: ({"username": "david"}, None))
    table: dict = {}
    monkeypatch.setattr(matrix, "_ROOMS", table)

    def add(history, room_id=ROOM, load=True, **kw):
        cfg = core_matrix.Config(homeserver="https://hs.test", token="t", rooms={room_id})
        hs = Homeserver(history, **kw)
        r = matrix.Room(room_id, cfg, ME, get=hs)
        table[r.thread] = r
        if load:
            r.load()
        return r, hs

    return add


def ask(q, **kw):
    ok, out = search.search(q, "tok", memory=False, **kw)
    assert ok, out
    return out


def _story(n, words=None):
    words = words or {}
    return [_ev(i, ME if i % 2 else MEL, words.get(i, f"note {i}")) for i in range(1, n + 1)]


def test_a_rooms_messages_are_found_by_the_logs_id(rooms):
    r, _ = rooms([_ev(1, ME, "where is the walrus"), _ev(2, MEL, "the walrus is home")])
    out = ask("walrus")
    by = {h["message"]: h for h in out["messages"]}
    assert set(by) == {"$1", "$2"}
    mine, theirs = by["$1"], by["$2"]
    assert mine["session"] == r.thread and mine["role"] == "user" and mine["kind"] == "text"
    assert theirs["role"] == "assistant" and theirs["at"] == 1_790_000_002.0
    assert theirs["thread"] == {"title": "sam", "project": None, "harness": "matrix",
                                "live": False, "archived": False}
    # The id is the one the room's log gives the message.
    ok, env = threads.session_log(r.thread, limit=30)
    assert ok and "$2" in [m["id"] for m in env["messages"]]
    # The room's name is a thread hit, under the agent filter's harness.
    [t] = ask("sam")["threads"]
    assert t["session"] == r.thread and t["harness"] == "matrix" and t["title"] == "sam"


def test_sync_events_edits_and_redactions_follow(rooms):
    r, _ = rooms([_ev(1, MEL, "plain words")])
    assert ask("kayak")["messages"] == []
    r.on_event(_ev(2, MEL, "bring the kayak"))
    [hit] = ask("kayak")["messages"]
    assert hit["message"] == "$2"
    r.on_event({"type": "m.room.message", "event_id": "$e", "sender": MEL,
                "content": {"msgtype": "m.text", "body": "* bring the canoe",
                            "m.new_content": {"msgtype": "m.text", "body": "bring the canoe"},
                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$2"}}})
    assert ask("kayak")["messages"] == []
    assert [h["message"] for h in ask("canoe")["messages"]] == ["$2"]
    r.on_event({"type": "m.room.redaction", "event_id": "$r", "redacts": "$2"})
    assert ask("canoe")["messages"] == []
    assert search.refresh()["changed"] == 0            # nothing new: nothing read


def test_a_query_never_asks_the_homeserver(rooms):
    _r, hs = rooms(_story(120, {3: "an old heron"}))
    asked = len(hs.asked)
    assert ask("heron")["messages"] == []              # 3 is not held: not yet
    assert len(hs.asked) == asked


def test_backfill_pages_history_in_down_to_the_start(rooms):
    r, _hs = rooms(_story(120, {3: "an old heron", 100: "a new heron"}))
    assert [h["message"] for h in ask("heron")["messages"]] == ["$100"]
    assert search.backfill(pages=1) == 1               # bounded: one page, 21..70
    assert [h["message"] for h in ask("heron")["messages"]] == ["$100"]
    assert search.backfill() == 1                      # 1..20: the start
    hits = ask("heron")["messages"]
    assert [h["message"] for h in hits] == ["$100", "$3"]
    assert hits[1]["at"] == 1_790_000_003.0 and hits[1]["role"] == "user"
    assert search.backfill() == 0                      # the room is all in
    conn = search._connect()
    assert conn.execute("SELECT count(*) FROM docs WHERE session=?",
                        (r.thread,)).fetchone()[0] == 120
    # The held messages were not touched by it: still 50 of them.
    assert len(r.messages) == 50


def test_backfill_takes_an_edit_before_its_original(rooms):
    edit = {"type": "m.room.message", "event_id": "$e", "sender": MEL,
            "origin_server_ts": 1_790_000_005_000,
            "content": {"msgtype": "m.text", "body": "* fixed otter",
                        "m.new_content": {"msgtype": "m.text", "body": "fixed otter"},
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"}}}
    history = [_ev(1, MEL, "typo oter")] + [_ev(i) for i in range(2, 5)] + [edit] \
        + [_ev(i) for i in range(6, 60)]
    _r, _ = rooms(history)
    search.backfill()
    [hit] = ask("otter")["messages"]
    assert hit["message"] == "$1" and hit["at"] == 1_790_000_001.0
    assert ask("oter")["messages"] == []


def test_a_restart_fills_the_gap_below_what_is_held(rooms):
    history = _story(40, {10: "first lynx"})
    _r, _hs = rooms(history)
    search.backfill()                                  # all 40 held: the start
    assert [h["message"] for h in ask("lynx")["messages"]] == ["$10"]
    # The canvas is down while 120 more come; read again, it holds 111..160.
    history += _story(160, {130: "second lynx", 80: "middle lynx"})[40:]
    search._MATRIX_REV.clear()                         # a new process
    matrix._ROOMS.clear()
    r2, hs2 = rooms(history)
    assert [h["message"] for h in ask("lynx")["messages"]] == ["$130", "$10"]
    assert search._MATRIX_GAPS == {r2.thread: "110"}
    search.backfill()                                  # 61..110, then 11..60: met $11..
    assert [h["message"] for h in ask("lynx")["messages"]] == ["$130", "$80", "$10"]
    assert search._MATRIX_GAPS == {}
    pages = [p for p, q in hs2.asked if p.endswith("/messages")]
    assert len(pages) == 3                             # the newest page and the gap's two


def test_only_rooms_matrix_serves_are_indexed_or_answered(rooms):
    r, _ = rooms([_ev(1, MEL, "secret gecko")])
    other, _ = rooms([_ev(1, MEL, "another gecko")], room_id="!other:ryer.org")
    from agent_media_core import deleted

    deleted.mark([other.thread])
    assert [h["session"] for h in ask("gecko")["messages"]] == [r.thread]
    conn = search._connect()
    assert conn.execute("SELECT count(*) FROM docs WHERE session=?",
                        (other.thread,)).fetchone()[0] == 0
    # Taken off the list (or the sync off): its rows stay, unanswered.
    del matrix._ROOMS[r.thread]
    assert ask("gecko")["messages"] == [] and ask("sam")["threads"] == []
    # A room not loaded yet is not read.
    _late, _ = rooms([_ev(1, MEL, "late gecko")], room_id="!late:ryer.org", load=False)
    assert ask("gecko")["messages"] == []


def test_a_busy_room_is_left_for_the_next_pass(rooms):
    r, _ = rooms([_ev(1, MEL, "busy badger")])
    held, release = threading.Event(), threading.Event()

    def hold():
        with r.lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        out = ask("badger")
        assert out["messages"] == [] and out["indexing"] is True
    finally:
        release.set()
        t.join()
    assert [h["message"] for h in ask("badger")["messages"]] == ["$1"]


def test_a_hit_opens_around_its_message(rooms):
    r, _hs = rooms(_story(120))
    ok, env = threads.session_log(r.thread, limit=10, around="$30")   # not held: paged in
    assert ok and env["around"] == {"id": "$30", "found": True, "newer": False}
    ids = [m["id"] for m in env["messages"]]
    assert ids[0] == "$25" and ids[-1] == "$120" and env["older"] is True
    assert env["newer"] is False and env["messages"][1]["peer"] == {"name": "Mel"}
    ok, env = threads.session_log(r.thread, limit=10, around="$gone")
    assert env["around"] == {"id": "$gone", "found": False, "newer": False}
    assert [m["id"] for m in env["messages"]][-1] == "$120" and len(env["messages"]) == 10
    ok, env = threads.session_log(r.thread, limit=10)
    assert "around" not in env and "newer" not in env


@pytest.fixture()
def home_claude(tmp_path):
    """A Claude Code thread beside the rooms, under the conftest's throwaway
    CLAUDE_CONFIG_DIR."""
    from test_contract import SID
    from test_search import claude

    def write(words):
        s = claude(tmp_path, SID)
        s.prompt(words)
        s.end_turn()

    return write


def test_rooms_only_or_none(rooms, home_claude):
    r, _ = rooms([_ev(1, MEL, "the marmot is back")])
    home_claude("marmot in a coding thread")
    assert {h["thread"]["harness"] for h in ask("marmot")["messages"]} == {"matrix", "claude"}
    [only] = ask("marmot", rooms="only")["messages"]
    assert only["session"] == r.thread
    [none] = ask("marmot", rooms="none")["messages"]
    assert none["thread"]["harness"] == "claude"
    # Thread hits follow it too: the room's name is gone without rooms.
    assert [t["session"] for t in ask("sam", rooms="only")["threads"]] == [r.thread]
    assert ask("sam", rooms="none")["threads"] == []
