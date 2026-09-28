"""`GET /music` and `POST /music` (§6.9a): the Media tab's now playing and controls.

Nothing reaches a player: the status read and the `media music` runner are
faked at the module's seams, and what is pinned is the HTTP answer and the
verbs the runner is handed.
"""

from __future__ import annotations

import pytest

from agent_media_server import auth_abs, music

from test_contract import AUTH, audio_host, call, keys, server, signed_in, typed  # noqa: F401

NOW = {"backend": "phone", "uri": "yt:x", "media_id": "x", "path": "http://localhost:6616/x.mka",
       "title": "A Mix", "chapter": None, "pos_ms": 7000, "dur_ms": 3019000,
       "paused": False, "speed": 1.0, "volume": 100, "held": False}


@pytest.fixture()
def player(monkeypatch):
    ran: list = []
    monkeypatch.setattr(music, "_status", lambda: dict(NOW))
    monkeypatch.setattr(music, "_run", lambda argv: (ran.append(argv), (True, ""))[1])
    music._reset_cache()
    yield ran
    music._reset_cache()


def test_now_is_the_status_and_the_picker(server, signed_in, audio_host, player):
    res, obj = call(server, "GET", "/music", headers=AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "now", "where"}
    assert obj["now"] == NOW
    assert keys(obj["where"]) == {"current", "next", "overridden", "options"}


@pytest.mark.parametrize("body,argv", [
    ({"action": "pause"}, ["pause"]),
    ({"action": "resume"}, ["resume"]),
    ({"action": "toggle"}, ["toggle"]),
    ({"action": "next"}, ["next"]),
    ({"action": "prev"}, ["prev", "--restart-first"]),
    ({"action": "stop"}, ["stop"]),
    ({"action": "like"}, ["like"]),
    ({"action": "seek", "to": 600}, ["seek", "0:10:00"]),
    ({"action": "seek", "to": 3725.4}, ["seek", "1:02:05"]),
    ({"action": "seek-by", "by": 30}, ["seek", "+30"]),
    ({"action": "seek-by", "by": -15}, ["seek", "-15"]),
])
def test_controls_are_the_cli_verbs(server, signed_in, audio_host, player, body, argv):
    res, obj = call(server, "POST", "/music", body, AUTH)
    assert res.status == 200, obj
    assert player == [argv]
    assert obj["now"] == NOW


@pytest.mark.parametrize("body", [{}, {"action": "quit"}, {"action": "seek"},
                                  {"action": "seek", "to": "10:00"}, {"action": "seek-by", "by": True}])
def test_a_bad_control_is_400_and_runs_nothing(server, signed_in, audio_host, player, body):
    res, obj = call(server, "POST", "/music", body, AUTH)
    assert res.status == 400 and obj["ok"] is False and isinstance(obj["error"], str)
    assert player == []


def test_a_failed_control_is_502_with_the_state(server, signed_in, audio_host, player, monkeypatch):
    monkeypatch.setattr(music, "_run", lambda argv: (False, "nothing playing"))
    res, obj = call(server, "POST", "/music", {"action": "next"}, AUTH)
    assert res.status == 502 and obj["error"] == "nothing playing" and obj["now"] == NOW


@pytest.mark.parametrize("who,status", [((None, 401), 401),
                                        (({"username": "guest", "type": "user"}, 200), 403)])
def test_controls_are_gated(server, monkeypatch, audio_host, player, who, status):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda bearer: who)
    res, obj = call(server, "POST", "/music", {"action": "pause"}, AUTH)
    assert res.status == status and obj["ok"] is False
    assert player == []


def test_reading_is_kept_a_moment(server, signed_in, audio_host, monkeypatch):
    reads = []
    monkeypatch.setattr(music, "_status", lambda: (reads.append(1), dict(NOW))[1])
    music._reset_cache()
    call(server, "GET", "/music", headers=AUTH)
    call(server, "GET", "/music", headers=AUTH)
    assert len(reads) == 1
    music._reset_cache()
