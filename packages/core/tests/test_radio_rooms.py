"""The radio's house player (radio_io.RoomsPlayer): Mopidy's queue read as
the station reads a phone player's, and a song fetched into the rooms cache
before it is queued.

Nothing reaches Mopidy, the renderer or the phone: the MPD round-trip, the
renderer's read and the fetch are faked.
"""

from __future__ import annotations

import pytest

from agent_media_core import radio_io
from agent_media_core.sinks import music, music_fetch

VID = "jGYJrjwiPC4"


class Mpd:
    """MPD's answers: a queue of files and the one playing."""

    def __init__(self):
        self.files: list = []
        self.pos = None
        self.state = "stop"
        self.sent: list = []

    def __call__(self, *lines):
        out = []
        for line in lines:
            self.sent.append(line)
            if line == "status":
                song = f"song: {self.pos}\n" if self.pos is not None else ""
                out.append(f"state: {self.state}\n{song}playlistlength: {len(self.files)}\nelapsed: 12.5\nOK\n")
            elif line == "currentsong":
                out.append(f"file: {self.files[self.pos]}\nOK\n" if self.pos is not None else "OK\n")
            elif line == "playlistinfo":
                out.append("".join(f"file: {f}\n" for f in self.files) + "OK\n")
            else:
                if line == "clear":
                    self.files, self.pos = [], None
                elif line.startswith("add "):
                    self.files.append(line[5:-1])
                elif line == "play" or line.startswith("play "):
                    self.pos = int(line[5:]) if line.startswith("play ") else 0
                    self.state = "play"
                out.append("OK\n")
        return out


@pytest.fixture()
def house(monkeypatch):
    mpd = Mpd()
    monkeypatch.setattr(radio_io.RoomsPlayer, "_mpd", staticmethod(mpd))
    monkeypatch.setattr(music, "mpv_now_props", lambda: {"time-pos": 61.0, "duration": 200.0})
    fetched: list = []
    monkeypatch.setattr(music_fetch, "ensure_local",
                        lambda url: (fetched.append(url), f"/rooms/cache/{url[-11:]}.mka")[1])
    monkeypatch.setattr(radio_io.RoomsPlayer, "_quiet_phone", staticmethod(lambda: None))
    return mpd, fetched


def test_the_house_is_a_radio_player():
    assert isinstance(radio_io.player("rooms"), radio_io.RoomsPlayer)


def test_idle_house(house):
    p = radio_io.RoomsPlayer().props()
    assert p["idle-active"] is True and p["playlist-count"] == 0 and p["playlist-pos"] is None


def test_a_song_is_fetched_then_played(house):
    mpd, fetched = house
    room = radio_io.RoomsPlayer()
    assert room.send({"id": VID}, replace=True) is True
    assert fetched == [f"https://www.youtube.com/watch?v={VID}"]
    assert mpd.files == [f"mpv:/rooms/cache/{VID}.mka"] and mpd.state == "play"
    p = room.props()
    # Position and length are the renderer's, and the id is read from the file.
    assert p["playlist-pos"] == 0 and p["playlist-count"] == 1
    assert p["time-pos"] == 61.0 and p["duration"] == 200.0
    assert room.playing() == VID and room.holds(VID)


def test_a_queued_song_waits_its_turn(house):
    mpd, _ = house
    room = radio_io.RoomsPlayer()
    room.send({"id": VID}, replace=True)
    room.send({"id": "song01xxxxx"})
    assert len(mpd.files) == 2 and mpd.pos == 0


def test_a_song_that_will_not_fetch_is_not_streamed(house, monkeypatch):
    mpd, _ = house
    monkeypatch.setattr(music_fetch, "ensure_local", lambda url: None)
    assert radio_io.RoomsPlayer().send({"id": VID}, replace=True) is False
    assert mpd.files == []


def test_clear_keeps_the_song_playing(house):
    mpd, _ = house
    room = radio_io.RoomsPlayer()
    room.send({"id": VID}, replace=True)
    room.clear()
    assert mpd.sent[-1] == "delete 1:"
