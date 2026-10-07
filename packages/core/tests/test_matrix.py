"""The shared Matrix /sync loop (agent_media_core.matrix): one pass at a time,
against a canned homeserver — no network."""

import json
import urllib.parse

from agent_media_core import matrix

ROOM = "!sam:ryer.org"
OTHER = "!other:ryer.org"


def _cfg():
    return matrix.Config(homeserver="https://hs.test", token="tok",
                         rooms={ROOM}, timeout_ms=10)


def _sync(tmp_path, pages):
    asked = []

    def fetch(req, timeout):
        asked.append(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query))
        assert req.get_header("Authorization") == "Bearer tok"
        return pages.pop(0)

    return matrix.Sync(_cfg(), state_path=tmp_path / "sync.json", fetch=fetch), asked


def _page(batch, rooms):
    return {"next_batch": batch, "rooms": {"join": {
        r: {"timeline": {"events": [{"event_id": e, "type": "m.room.message"}
                                    for e in evs]}}
        for r, evs in rooms.items()}}}


def test_only_allowed_rooms_reach_a_subscriber_and_once(tmp_path):
    s, asked = _sync(tmp_path, [_page("b1", {ROOM: ["$1", "$2"], OTHER: ["$x"]}),
                                _page("b2", {ROOM: ["$2", "$3"]})])
    got = []
    s.subscribe(lambda room, ev: got.append((room, ev["event_id"])))
    s.poll_once()
    s.poll_once()
    assert got == [(ROOM, "$1"), (ROOM, "$2"), (ROOM, "$3")]
    assert "since" not in asked[0] and asked[1]["since"] == ["b1"]


def test_state_survives_a_restart(tmp_path):
    s, _ = _sync(tmp_path, [_page("b1", {ROOM: ["$1"]})])
    s.poll_once()
    assert json.loads((tmp_path / "sync.json").read_text())["next_batch"] == "b1"
    s2, asked = _sync(tmp_path, [_page("b2", {ROOM: ["$1"]})])
    got = []
    s2.subscribe(lambda room, ev: got.append(ev["event_id"]))
    s2.poll_once()
    assert asked[0]["since"] == ["b1"] and got == []


def test_a_failing_subscriber_does_not_starve_the_others(tmp_path):
    s, _ = _sync(tmp_path, [_page("b1", {ROOM: ["$1"]})])
    got = []

    def bad(room, ev):
        raise RuntimeError("boom")

    s.subscribe(bad)
    s.subscribe(lambda room, ev: got.append(ev["event_id"]))
    s.poll_once()
    assert got == ["$1"]


def test_config_needs_a_token_and_a_room():
    assert matrix.Config.from_env({"MATRIX_ACCESS_TOKEN": "t"}) is None
    assert matrix.Config.from_env({"MATRIX_ROOM_ALLOW": ROOM}) is None
    c = matrix.Config.from_env({"MATRIX_ACCESS_TOKEN": "t",
                                "MATRIX_ROOM_ALLOW": f" {ROOM}, ",
                                "MATRIX_HOMESERVER": "https://hs.test/"})
    assert c.rooms == {ROOM} and c.homeserver == "https://hs.test"
