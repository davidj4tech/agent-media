"""Matrix rooms as threads (matrix.py, proposal step 1): the room cache
against a canned homeserver, and the routes that serve it. No network."""

import pytest

from agent_media_core import matrix as core_matrix
from agent_media_server import matrix, sessions, threads

ROOM = "!sam:ryer.org"
ME, SAM, MEL = "@david:ryer.org", "@sam:ryer.org", "@mel:ryer.org"


def _ev(i, sender=MEL, body=None, **content):
    return {"type": "m.room.message", "event_id": f"${i}", "sender": sender,
            "origin_server_ts": 1_790_000_000_000 + i * 1000,
            "content": {"msgtype": "m.text", "body": body or f"m{i}", **content}}


class Homeserver:
    """`/rooms/{id}/messages` over a fixed history, newest first, `PAGE` a go."""

    def __init__(self, history, name="sam"):
        self.history = history            # oldest first
        self.name = name
        self.asked = []

    def __call__(self, config, path, params=None, timeout=10.0):
        self.asked.append((path, params))
        if path.endswith("/joined_members"):
            return {"joined": {ME: {"display_name": "David"},
                               SAM: {"display_name": "Sam"},
                               MEL: {"display_name": "Mel"}}}
        if path.endswith("/state"):
            return [{"type": "m.bridge", "content": {"protocol": {
                "id": "gmessages-sms", "displayname": "Google Messages (SMS)"}}}]
        if path.endswith("/state/m.room.name"):
            if not self.name:
                raise OSError("404")
            return {"name": self.name}
        end = int((params or {}).get("from") or len(self.history))
        start = max(0, end - matrix.PAGE)
        chunk = list(reversed(self.history[start:end]))
        return {"chunk": chunk, **({"end": str(start)} if start else {})}


def _room(history, **kw):
    cfg = core_matrix.Config(homeserver="https://hs.test", token="t", rooms={ROOM})
    hs = Homeserver(history, **kw)
    return matrix.Room(ROOM, cfg, ME, get=hs), hs


@pytest.fixture
def one_room(monkeypatch):
    r, _ = _room([_ev(1, ME, "hi"), _ev(2, SAM, "hello")])
    monkeypatch.setattr(matrix, "_ROOMS", {r.thread: r})
    monkeypatch.setenv("MATRIX_SAM_ID", SAM)
    return r


def test_a_room_id_is_a_session_id():
    t = matrix.thread_of(ROOM)
    assert sessions._SESSION.fullmatch(t) and t == matrix.thread_of(ROOM)
    assert matrix.thread_of("!other:ryer.org") != t


def test_messages_have_a_role_and_a_sender():
    r, _ = _room([_ev(1, ME, "hi"), _ev(2, SAM, "hello")])
    r.load()
    msgs, older = r.page(30)
    assert [(m["role"], m["sender"]["name"], m["parts"][0]["text"]) for m in msgs] == \
        [("user", "David", "hi"), ("assistant", "Sam", "hello")]
    assert older is False and msgs[0]["at"] == 1_790_000_001.0
    assert msgs[0]["turn"] == {"running": False} and msgs[0]["spoken"] is None
    assert "peer" not in msgs[0] and msgs[1]["peer"] == {"name": "Sam"}


def test_paging_back_reaches_the_homeserver_past_what_is_held():
    r, hs = _room([_ev(i) for i in range(1, 121)])
    r.load()
    newest, older = r.page(30)
    assert [m["id"] for m in newest] == [f"${i}" for i in range(91, 121)] and older
    back, older = r.page(30, before="$71")    # held: 71..120
    assert [m["id"] for m in back] == [f"${i}" for i in range(41, 71)] and older
    back, older = r.page(30, before="$41")    # 41 is the oldest held: one more page
    assert [m["id"] for m in back] == [f"${i}" for i in range(11, 41)] and older
    back, older = r.page(30, before="$11")
    assert [m["id"] for m in back] == [f"${i}" for i in range(1, 11)] and not older


def test_sync_events_append_edit_and_redact():
    r, _ = _room([_ev(1)])
    r.on_event(_ev(2, ME, "early"))           # before the first read: kept
    r.load()
    r.on_event(_ev(2, ME, "early"))           # again: kept once
    r.on_event({"type": "m.room.message", "event_id": "$e", "sender": MEL,
                "content": {"msgtype": "m.text", "body": "* m1!",
                            "m.new_content": {"msgtype": "m.text", "body": "m1!"},
                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"}}})
    r.on_event(_ev(3, MEL, msgtype="m.image", body="cat.jpg"))
    r.on_event({"type": "m.room.redaction", "event_id": "$r", "redacts": "$2"})
    msgs, _ = r.page(30)
    assert [m["id"] for m in msgs] == ["$1", "$3"]
    assert msgs[0]["parts"][0]["text"] == "m1!" and msgs[0]["edited"]
    assert msgs[1]["parts"][0]["text"] == "_sent a photo_"


def test_an_unnamed_room_is_named_by_who_else_is_in_it(monkeypatch):
    monkeypatch.setenv("MATRIX_SAM_ID", SAM)
    r, _ = _room([_ev(1)], name="")
    r.load()
    assert r.title() == "Mel"


def test_targets_row(one_room):
    (row,) = matrix.rows({one_room.thread})
    assert row["session"] == one_room.thread and row["source"] == "matrix"
    assert row["title"] == "sam" and row["archived"] and not row["live"]
    assert row["recap"]["text"] == "Sam: hello" and row["drivable"] is False
    assert row["network"] == {"id": "gmessages", "name": "Google Messages"}


def test_the_log_and_the_stream_snapshot_serve_the_room(one_room):
    ok, env = threads.session_log(one_room.thread, limit=30)
    assert ok and [m["id"] for m in env["messages"]] == ["$1", "$2"]
    assert env["room"]["id"] == ROOM and env["lines"] == [] and not env["pending"]
    from agent_media_server import thread_events

    ok, snap = thread_events.snapshot(one_room.thread)
    assert ok and len(snap["messages"]) == 2 and snap["state"] == "ended"


def test_targets_lists_the_room(one_room):
    rows = [r for r in sessions.sessions_index() if r.get("source") == "matrix"]
    assert [r["session"] for r in rows] == [one_room.thread]


def test_owner_is_the_first_control_id_that_is_not_the_agent():
    env = {"MATRIX_SAM_ID": SAM, "MATRIX_CONTROL_IDS": f"{SAM},{ME}"}
    assert matrix._owner(env) == ME
    assert matrix._owner({**env, "MATRIX_OWNER_ID": MEL}) == MEL


def test_a_reply_without_an_owner_token_is_refused_not_resumed(one_room, monkeypatch):
    from agent_media_server import auth, send

    monkeypatch.delenv("MATRIX_OWNER_TOKEN", raising=False)
    monkeypatch.setattr(auth, "gate", lambda bearer: ("david", None))
    ok, err = send.reply("", "hello", "tok", session=one_room.thread)
    assert not ok and err["status"] == 409
    assert matrix.rows()[0]["drivable"] is False


def test_a_reply_is_sent_as_the_owner_and_kept_once(one_room, monkeypatch):
    from agent_media_server import auth, send

    sent = []

    def put(config, path, body, token, timeout=15.0):
        sent.append((path, body, token))
        return {"event_id": "$mine"}

    monkeypatch.setenv("MATRIX_OWNER_TOKEN", "owner-tok")
    monkeypatch.setattr(matrix, "_put", put)
    monkeypatch.setattr(auth, "gate", lambda bearer: ("david", None))
    ok, detail = send.reply("", "on my way", "tok", session=one_room.thread,
                            quote="where are you?")
    assert ok and detail["submitted"] and detail["event_id"] == "$mine"
    (path, body, token), = sent
    assert token == "owner-tok" and "/rooms/%21sam%3Aryer.org/send/m.room.message/" in path
    assert body == {"msgtype": "m.text", "body": "> where are you?\n\non my way"}
    one_room.on_event({"type": "m.room.message", "event_id": "$mine", "sender": ME,
                       "origin_server_ts": 1, "content": body})   # the sync's echo
    msgs, _ = one_room.page(30)
    assert [m["id"] for m in msgs][-1:] == ["$mine"] and len(msgs) == 3
    assert msgs[-1]["role"] == "user" and "peer" not in msgs[-1]
    assert matrix.rows()[0]["drivable"] is True
    ok, err = send.reply("", "x", "tok", session=one_room.thread, mode="branch")
    assert not ok and err["status"] == 409


def test_a_room_starts_quiet_once(monkeypatch):
    from agent_media_core import speak_priority

    t = matrix.thread_of(ROOM)
    matrix._seed_quiet([t])
    assert speak_priority.level_of(t) == "quiet"
    speak_priority.clear_level(t)                 # David picks the default
    matrix._seed_quiet([t])
    assert speak_priority.level_of(t) == speak_priority.default_level()


def test_held_speech_is_a_play_on_its_message(one_room):
    from agent_media_core.state import StateStore

    one_room.load()
    st = StateStore()
    rid = st.add_history(sink="speech", uri="/tmp/x.ogg", started_at=5.0,
                         extras={"session": one_room.thread, "matrix_event": "$2",
                                 "held": True})
    env = matrix.envelope(one_room, limit=30)
    by = {m["id"]: m for m in env["messages"]}
    assert by["$1"]["spoken"] is None
    assert by["$2"]["spoken"] == {"id": rid, "key": "", "at": 5.0, "unheard": True}
    st.mark_heard(rid)
    assert "unheard" not in matrix.envelope(one_room, limit=30)["messages"][1]["spoken"]


def test_a_rooms_level_can_be_set_from_the_app(one_room, monkeypatch):
    from agent_media_core import speak_priority
    from agent_media_server import auth, pins

    monkeypatch.setattr(auth, "gate", lambda bearer: ("david", None))
    ok, _ = pins.session_priority(one_room.thread, None, "tok", level="auto")
    assert ok and speak_priority.level_of(one_room.thread) == "auto"


# --- the owner's loop: a bridge's rooms (David, 11 Oct 2026) ---------------

BOT = "@gmessagesbot:ryer.org"


def test_a_bridge_sender_is_its_bot_or_its_puppets():
    b = {BOT}
    assert matrix._bridge_sender(BOT, b)
    assert matrix._bridge_sender("@gmessages_1.42:ryer.org", b)
    assert not matrix._bridge_sender("@gmessages_1.42:evil.org", b)
    assert not matrix._bridge_sender(MEL, b)


@pytest.fixture
def owner_loop(monkeypatch):
    monkeypatch.setattr(matrix, "_ROOMS", {})
    monkeypatch.setenv("MATRIX_OWNER_TOKEN", "owner-tok")
    base = core_matrix.Config(homeserver="https://hs.test", token="sam-tok", rooms={ROOM})
    joined = []
    monkeypatch.setattr(matrix, "_post", lambda config, path, body, timeout=15.0:
                        joined.append((path, config.token)) or {})
    return matrix._Owner(base, ME, {BOT}, None), joined


def test_a_bridges_invitation_is_accepted_and_the_room_taken_in(owner_loop):
    from agent_media_core import speak_priority

    o, joined = owner_loop
    invite = [{"type": "m.room.member", "state_key": ME, "sender": "@gmessages_1.9:ryer.org"}]
    o._invited("!sms:ryer.org", invite)
    assert joined == [("/rooms/%21sms%3Aryer.org/join", "owner-tok")]
    r = matrix._ROOMS[matrix.thread_of("!sms:ryer.org")]
    assert r.config.token == "owner-tok" and "!sms:ryer.org" in o.config.rooms
    assert speak_priority.level_of(r.thread) == "quiet"


def test_anyone_elses_invitation_is_left_alone(owner_loop):
    o, joined = owner_loop
    o._invited("!x:ryer.org", [{"type": "m.room.member", "state_key": ME, "sender": MEL}])
    assert joined == [] and not o.config.rooms


def test_discover_takes_the_rooms_a_bridge_is_in(owner_loop, monkeypatch):
    o, _ = owner_loop

    def get(config, path, params=None, timeout=10.0):
        if path == "/joined_rooms":
            return {"joined_rooms": ["!sms:ryer.org", "!family:ryer.org"]}
        return {"joined": {ME: {}, BOT: {}} if "sms" in path else {ME: {}, MEL: {}}}

    monkeypatch.setattr(matrix, "_get", get)
    o.discover()
    assert o.config.rooms == {"!sms:ryer.org"}


def test_a_rooms_service_is_named_from_its_bridge_state():
    proto = {"id": "gmessages-sms", "displayname": "Google Messages (SMS)"}
    assert matrix._network([{"type": "m.room.name"}, {"type": "m.bridge",
                            "content": {"protocol": proto}}]) == {"id": "gmessages", "name": "Google Messages"}
    assert matrix._network([{"type": "m.room.create"}]) is None


def test_a_room_can_be_archived_and_brought_back(one_room, monkeypatch):
    # Archiving a chat from the Messages tab was a 404 ("no such session"):
    # the route knew only coding sessions, though the room's row reads the
    # same flags (David, 19 Oct 2026).
    from agent_media_server import archive, auth

    monkeypatch.setattr(auth, "gate", lambda bearer: ({"name": "david"}, {}))
    ok, got = archive.session_archive(one_room.thread, True, "tok")
    assert ok and got == {"session": one_room.thread, "archived": True}
    (row,) = matrix.rows(frozenset(archive.archived()))
    assert row["archived"] is True
    ok, _ = archive.session_archive(one_room.thread, False, "tok")
    assert ok and one_room.thread not in archive.archived()
    ok, got = archive.session_archive("8f0e6c1e-0000-4000-8000-000000000000", True, "tok")
    assert not ok and got["status"] == 404
