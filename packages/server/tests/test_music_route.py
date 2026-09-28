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


@pytest.fixture(autouse=True)
def _no_phone(monkeypatch):
    """A track's chapters are read on the phone over ssh; never here."""
    from agent_media_core.sinks import music_sasonica
    monkeypatch.setattr(music_sasonica, "chapters", lambda url: [])


@pytest.fixture()
def player(monkeypatch):
    ran: list = []
    monkeypatch.setattr(music, "_status", lambda: dict(NOW))
    monkeypatch.setattr(music, "_run", lambda argv: (ran.append(argv), (True, ""))[1])
    music._reset_cache(forget=True)
    yield ran
    music._reset_cache(forget=True)


def test_now_is_the_status_and_the_picker(server, signed_in, audio_host, player):
    res, obj = call(server, "GET", "/music", headers=AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "now", "chapters", "where"}
    assert obj["now"] == NOW and obj["chapters"] == []
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
    ({"action": "play", "uri": "yt:https://www.youtube.com/watch?v=Gkl8blLusFc"},
     ["play", "yt:https://www.youtube.com/watch?v=Gkl8blLusFc"]),
    ({"action": "add", "uri": "https://www.youtube.com/watch?v=Gkl8blLusFc"},
     ["play", "https://www.youtube.com/watch?v=Gkl8blLusFc", "--add"]),
])
def test_controls_are_the_cli_verbs(server, signed_in, audio_host, player, body, argv):
    res, obj = call(server, "POST", "/music", body, AUTH)
    assert res.status == 200, obj
    assert player == [argv]
    assert obj["now"] == NOW


@pytest.mark.parametrize("body", [{}, {"action": "quit"}, {"action": "seek"},
                                  {"action": "play"}, {"action": "play", "uri": "--where rooms"},
                                  {"action": "add", "uri": "/etc/passwd"},
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


def test_a_mix_lists_its_tracks(server, signed_in, audio_host, player, monkeypatch):
    from agent_media_core.sinks import music_sasonica
    monkeypatch.setattr(music_sasonica, "phone_path", lambda url: "/x.mka" if url.startswith("http://localhost:6616/") else None)
    monkeypatch.setattr(music_sasonica, "chapters", lambda url: [
        {"title": "Sun Salutation", "start": 0.0, "end": 264.0},
        {"title": "Trance Life", "start": 674.5, "end": 800.0}])
    _, obj = call(server, "GET", "/music", headers=AUTH)
    assert obj["chapters"] == [{"title": "Sun Salutation", "start_ms": 0},
                               {"title": "Trance Life", "start_ms": 674500}]


def test_a_failed_read_after_a_track_keeps_the_track(server, signed_in, audio_host, monkeypatch):
    reads = [dict(NOW), {"backend": "mopidy", "pos_ms": None}]
    monkeypatch.setattr(music, "_status", lambda: reads.pop(0) if len(reads) > 1 else reads[0])
    music._reset_cache(forget=True)
    _, first = call(server, "GET", "/music", headers=AUTH)
    music._reset_cache()
    _, second = call(server, "GET", "/music", headers=AUTH)
    assert first["now"] == NOW and second["now"] == NOW
    music._reset_cache(forget=True)


def test_a_stop_is_believed_at_once(server, signed_in, audio_host, monkeypatch):
    monkeypatch.setattr(music, "_run", lambda argv: (True, ""))
    states = [dict(NOW)]
    monkeypatch.setattr(music, "_status", lambda: states[0])
    music._reset_cache(forget=True)
    call(server, "GET", "/music", headers=AUTH)
    states[0] = {"backend": "mopidy", "pos_ms": None}
    _, obj = call(server, "POST", "/music", {"action": "stop"}, AUTH)
    assert obj["now"]["pos_ms"] is None
    music._reset_cache(forget=True)


def test_recent_is_the_played_list(server, signed_in, audio_host, monkeypatch):
    from agent_media_core import music_recent
    items = [{"id": 3, "uri": "yt:https://www.youtube.com/watch?v=Gkl8blLusFc", "title": "As Hope",
              "at": 1790596000.0, "session": "ed0469b8", "inferred": True}]
    monkeypatch.setattr(music_recent, "recent", lambda: items)
    res, obj = call(server, "GET", "/music/recent", headers=AUTH)
    assert res.status == 200 and obj == {"ok": True, "items": items}


def test_recent_is_gated(server, monkeypatch, audio_host):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda bearer: (None, 401))
    res, obj = call(server, "GET", "/music/recent", headers=AUTH)
    assert res.status == 401 and obj["ok"] is False
