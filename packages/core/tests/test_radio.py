"""The radio station (agent_media_core.radio), with the phone faked.

The Mix listing, the player's properties, the queueing and the prefetch are
each replaced at the module's seams; what is pinned is what the station does
with them: stays a song ahead, follows a song that comes up, steps aside for
one it did not queue, and keeps a 👎'd song (and a twice-👎'd channel) off.
"""

from __future__ import annotations

import pytest

from agent_media_core import radio


def _song(i: int, channel: str = "Kenny Rogers") -> dict:
    return {"id": f"song{i:02d}xxxxx", "title": f"Song {i}", "channel": channel, "dur": 200}


SEED = {"id": "jGYJrjwiPC4", "title": "The Gambler", "channel": "Kenny Rogers", "dur": 211}
MIX = [SEED] + [_song(i, "Kenny Rogers" if i < 3 else f"Artist {i}") for i in range(1, 12)]


class Player:
    """A phone player: a playlist of ids and the index playing."""

    def __init__(self, first: str):
        self.items = [first]
        self.pos = 0
        self.t = 30.0
        self.labels: list = []
        self.seeks: list = []

    def props(self, where):
        if not self.items:
            return {"playlist-pos": -1, "playlist-count": 0, "idle-active": True}
        return {"path": f"http://localhost:6616/{self.items[self.pos]}.mka",
                "playlist-pos": self.pos, "playlist-count": len(self.items),
                "idle-active": False, "time-pos": self.t, "duration": 200.0}


@pytest.fixture()
def station(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    player = Player(SEED["id"])
    sent: list = []
    fetched: list = []
    mixes: list = []
    def mix(vid):
        # The seed's Mix is MIX; any other's, ten songs of its own.
        mixes.append(vid)
        return list(MIX) if vid == SEED["id"] else \
            [{**_song(i, f"Artist {i}"), "id": f"{vid[:5]}{i:02d}more"} for i in range(10)]

    monkeypatch.setattr(radio, "mix", mix)
    monkeypatch.setattr(radio, "_props", lambda where: player.props(where))

    refuse: list = []

    def send(where, song, replace=False):
        sent.append((song["id"], replace))
        if song["id"] in refuse:
            return False
        if replace:
            player.items, player.pos = [song["id"]], 0
        else:
            player.items.append(song["id"])
        return True

    monkeypatch.setattr(radio, "_send", send)
    monkeypatch.setattr(radio, "_prefetch", lambda song: (fetched.append(song["id"]), True)[1])
    monkeypatch.setattr(radio, "_label", lambda where, song: player.labels.append(song["id"]))
    monkeypatch.setattr(radio, "_note", lambda song: None)
    monkeypatch.setattr(radio, "_liked_ids", lambda: set())
    import agent_media_core.sinks._mpv_ipc as ipc
    monkeypatch.setattr(ipc, "command", lambda *a, **k: None)
    monkeypatch.setattr(radio, "_sink", lambda where: type("S", (), {
        "_endpoint": lambda self: "x",
        "seek_cur": lambda self, position_ms=0: player.seeks.append(position_ms)})())
    player.refuse = refuse
    return player, sent, fetched, mixes


def test_a_station_is_the_mix_without_its_seed(station):
    snap = radio.start(f"yt:https://www.youtube.com/watch?v={SEED['id']}", "sasonica", playing=True)
    assert snap["on"] and snap["seed"] == {"id": SEED["id"], "title": "The Gambler"}
    assert [r["id"] for r in snap["next"]] == [s["id"] for s in MIX[1:11]]
    assert snap["more"] == 1


def test_it_stays_one_song_queued_and_one_downloaded(station):
    player, sent, fetched, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()
    assert sent == [(MIX[1]["id"], False)]
    radio.tick()
    assert fetched == [MIX[2]["id"]]
    radio.tick()                   # nothing to do while the seed plays on
    assert len(sent) == 1 and fetched == [MIX[2]["id"]]
    states = [r["state"] for r in radio.snapshot()["next"][:3]]
    assert states == ["ready", "ready", None]


def test_a_song_that_comes_up_is_labelled_and_the_next_queued(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()
    player.pos = 1
    radio.tick()
    assert player.labels == [MIX[1]["id"]]
    assert radio.read()["current"] == MIX[1]["id"]
    assert sent[-1] == (MIX[2]["id"], False)


def test_a_song_it_has_no_record_of_is_played_on(station):
    """A canvas restarted mid-queueing loses the record of a song the player
    got; the station turned itself off on it (29 Sep 2026). It plays on."""
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.items, player.pos = ["aVtOLfGT3_8"], 0
    radio.tick()
    assert radio.is_on() and radio.read()["current"] == "aVtOLfGT3_8"
    assert sent[-1] == (MIX[1]["id"], False)


def test_a_play_of_something_else_ends_it(station, monkeypatch):
    from agent_media_core import cli

    radio.start(SEED["id"], "sasonica", playing=True)
    monkeypatch.setattr(cli, "_resolve_music_where", lambda w: "rooms")
    monkeypatch.setattr(cli.StateStore, "set_music_intent", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_note_music_where", lambda *a: None)

    played: list = []
    monkeypatch.setattr(cli, "SinkMusicRouter", lambda m: type("M", (), {
        "play": lambda self, uri, *a, **k: played.append(uri)})())
    cli.cmd_music(type("A", (), {"action": "play", "uri": "yt:abc", "add": False,
                                 "where": "rooms", "as_type": None, "title": ""})())
    assert played == ["yt:abc"]
    assert radio.is_on() is False


def test_dislike_skips_and_keeps_the_song_off(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()
    radio.dislike()
    assert player.pos == 0 or player.items[player.pos] == MIX[1]["id"]
    assert SEED["id"] in radio.read()["banned"]


def test_two_thumbs_down_on_a_channel_keep_it_off(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.dislike()                # the seed: Kenny Rogers, strike one
    radio.tick()
    radio.dislike()                # Song 1: Kenny Rogers, strike two
    left = [s["channel"] for s in radio.read()["queue"]]
    assert "Kenny Rogers" not in left and left


def test_a_short_list_is_topped_up_from_another_mix(station):
    _, _, _, mixes = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player, _, _, _ = station
    for _ in range(60):
        radio.tick()
        if player.pos + 1 < len(player.items):
            player.pos += 1        # each queued song is heard at once
    assert len(mixes) >= 2 and len(mixes) == len(set(mixes))


def test_off_leaves_the_music_playing(station):
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.stop()
    assert radio.is_on() is False


def test_only_a_youtube_track_on_the_phone(station):
    with pytest.raises(ValueError):
        radio.start("/storage/music/song.mp3", "sasonica", playing=True)
    with pytest.raises(ValueError):
        radio.start(SEED["id"], "rooms", playing=True)


def test_a_song_cut_off_is_put_on_again_where_it_was(station):
    """An app update restarts its player empty (David, 29 Sep 2026: "It got
    interrupted towards the end with an update... continue from where it
    was at please")."""
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.t = 150.0
    radio.tick()                   # seen at 2:30, the next queued
    player.items, player.pos = [], 0
    radio.tick()
    assert sent[-1] == (SEED["id"], True)
    assert player.seeks == [150000] and player.labels[-1] == SEED["id"]
    radio.tick()                   # and the station goes on from there
    assert sent[-1] == (MIX[1]["id"], False)


def test_a_song_played_out_is_not_put_on_again(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.t = 196.0
    radio.tick()
    player.items, player.pos = [], 0
    radio.tick()
    assert (SEED["id"], True) not in sent and player.seeks == []


def test_a_refused_song_is_tried_again_then_dropped(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.refuse.append(MIX[1]["id"])
    for _ in range(3):
        radio.tick()
    assert [x for x in sent if x[0] == MIX[1]["id"]] == [(MIX[1]["id"], False)] * 3
    radio.tick()
    assert sent[-1] == (MIX[2]["id"], False)
    assert MIX[1]["id"] not in [r["id"] for r in radio.snapshot()["next"]]


def test_idle_with_a_queue_is_between_two_songs(station):
    """The app's player can say idle between two songs with the next still
    queued; that is not a lost song, and the next one is the station's."""
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()                   # MIX[1] queued
    real = player.props
    player.props = lambda where: {"playlist-pos": 0, "playlist-count": 2, "idle-active": True}
    radio.tick()
    assert (SEED["id"], True) not in sent
    player.props = real


def test_its_own_song_from_the_list_does_not_turn_it_off(station):
    player, _, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.items, player.pos = [MIX[3]["id"]], 0     # still on the list
    radio.tick()
    assert radio.is_on() and radio.read()["current"] == MIX[3]["id"]
    assert MIX[3]["id"] not in [s["id"] for s in radio.read()["queue"]]


def test_a_song_tapped_on_the_list_plays_now(station):
    """A tap on Up next (David, 29 Sep 2026: "I'd like to be able to click on
    things on the up next list")."""
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()                   # MIX[1] queued in the player
    radio.play(MIX[4]["id"])
    assert sent[-1] == (MIX[4]["id"], True) and player.labels[-1] == MIX[4]["id"]
    st = radio.read()
    assert st["current"] == MIX[4]["id"]
    # The songs before it stay to come, the one the replace cleared first.
    assert [r["id"] for r in radio.snapshot()["next"][:4]] == [s["id"] for s in (MIX[1], MIX[2], MIX[3], MIX[5])]
    radio.tick()                   # and the station goes on from there
    assert sent[-1] == (MIX[1]["id"], False) and radio.is_on()


def test_only_a_song_on_the_list_can_be_tapped(station):
    radio.start(SEED["id"], "sasonica", playing=True)
    with pytest.raises(ValueError):
        radio.play(SEED["id"])     # heard already
    with pytest.raises(ValueError):
        radio.play("notonthelst")
    radio.stop()
    assert radio.play(MIX[2]["id"]) is None


def test_a_tapped_song_the_player_refuses_stays_on_the_list(station):
    player, _, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.refuse.append(MIX[3]["id"])
    with pytest.raises(RuntimeError):
        radio.play(MIX[3]["id"])
    assert [s["id"] for s in radio.read()["queue"]][:3] == [MIX[1]["id"], MIX[2]["id"], MIX[3]["id"]]
    assert radio.read()["current"] == SEED["id"]
