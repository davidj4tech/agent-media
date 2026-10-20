"""The house speakers one by one (music_rooms): which rooms are on the music
stream, and setting one on, off or louder without fighting a reply's duck.

Nothing reaches a Snapcast server: `get_status` and `set_client_volume` are
faked, and the state store is a throwaway one.
"""

import pytest

from agent_media_core import music_rooms, snapcast
from agent_media_core.state import StateStore


@pytest.fixture
def snap(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DUCK_ROOMS_STREAM", "am-music")
    monkeypatch.setenv("MEDIA_SNAP_PHONE_PREFIX", "p8a")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clients = {
        "hpo-music": {"stream": "am-music", "muted": False, "percent": 40, "connected": True},
        "sp4-music": {"stream": "am-music", "muted": True, "percent": 70, "connected": False},
        "p8a-music": {"stream": "am-music", "muted": False, "percent": 100, "connected": True},
        "hpo": {"stream": "am", "muted": True, "percent": 100, "connected": True},
    }
    calls = []

    def get_status(timeout=4.0):
        return {"groups": [{"id": f"g-{cid}", "stream_id": c["stream"],
                            "clients": [{"id": cid, "connected": c["connected"],
                                         "config": {"name": "Kitchen" if cid == "sp4-music" else "",
                                                    "volume": {"muted": c["muted"], "percent": c["percent"]}}}]}
                           for cid, c in clients.items()]}

    def set_client_volume(cid, percent, muted=None, timeout=4.0):
        calls.append((cid, percent, muted))
        clients[cid]["percent"] = percent
        if muted is not None:
            clients[cid]["muted"] = muted

    monkeypatch.setattr(snapcast, "get_status", get_status)
    monkeypatch.setattr(snapcast, "set_client_volume", set_client_volume)
    return clients, calls


def test_rooms_are_the_music_streams_clients_but_the_phones(snap):
    assert music_rooms.rooms() == [
        {"id": "hpo-music", "label": "hpo", "connected": True, "on": True, "volume": 40},
        {"id": "sp4-music", "label": "Kitchen", "connected": False, "on": False, "volume": 70},
    ]


def test_no_stream_no_rooms(snap, monkeypatch):
    monkeypatch.delenv("MEDIA_DUCK_ROOMS_STREAM")
    assert music_rooms.rooms() == []


def test_on_off_and_volume(snap):
    clients, calls = snap
    row = music_rooms.set_room("sp4-music", on=True)
    assert row["on"] is True and calls == [("sp4-music", 70, False)]
    music_rooms.set_room("hpo-music", volume=55)
    assert calls[-1] == ("hpo-music", 55, None)
    music_rooms.set_room("hpo-music", on=False)
    assert clients["hpo-music"]["muted"] is True


@pytest.mark.parametrize("cid", ["p8a-music", "hpo", "nope"])
def test_only_a_room_can_be_set(snap, cid):
    with pytest.raises(ValueError):
        music_rooms.set_room(cid, on=True)
    assert snap[1] == []


def test_under_a_duck_a_change_is_owed_not_played(snap):
    clients, calls = snap
    StateStore().set_rooms_duck({"level": 10, "vols": {"hpo-music": 40}})
    clients["hpo-music"]["percent"] = 10
    assert music_rooms.rooms()[0]["volume"] == 40  # its own level, not the duck's
    music_rooms.set_room("hpo-music", volume=80)
    assert calls[-1] == ("hpo-music", 10, None)
    # A room switched on under the duck joins it at the duck level.
    music_rooms.set_room("sp4-music", on=True)
    assert calls[-1] == ("sp4-music", 10, False)
    assert StateStore().get_rooms_duck()["vols"] == {"hpo-music": 80, "sp4-music": 70}
