"""The tmux status segment: now-playing, the marquee, and what it may cost.

This runs as a fresh process for every pane on every redraw, so the tests that
matter here are as much about cost and concurrency as about formatting: a call
that is merely slow is a bug at this frequency.
"""

from __future__ import annotations

import pytest

from agent_media_core import cli


# --- marquee -------------------------------------------------------------

@pytest.fixture
def marquee_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "state_dir", lambda: tmp_path)
    return tmp_path


def test_short_text_is_not_scrolled(marquee_state):
    assert cli._marquee("short", 20) == "short"


def test_long_text_is_windowed(marquee_state):
    out = cli._marquee("a very long title indeed", 10)
    assert len(out) == 10


def test_rate_does_not_depend_on_how_many_panes_render(marquee_state,
                                                       monkeypatch):
    """The bug this pins: the offset used to advance once per CALL, and every
    pane calls it, so the marquee sped up with the number of panes watching."""
    text = "a very long title that certainly does not fit in the window"
    now = [1000.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])

    once = cli._marquee(text, 10)

    # Same instant, many panes redrawing: all agree, none advance the crawl.
    for _ in range(20):
        assert cli._marquee(text, 10) == once

    now[0] += 3.0                      # three seconds later, at 1 col/s
    assert cli._marquee(text, 10) != once


def test_offset_tracks_elapsed_time(marquee_state, monkeypatch):
    text = "0123456789abcdefghijklmnopqrstuvwxyz"
    now = [500.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    first = cli._marquee(text, 8)
    now[0] += 1.0
    after_1s = cli._marquee(text, 8)
    assert after_1s == (text + "   ")[1:9]
    assert after_1s != first


def test_new_subject_restarts_the_crawl(marquee_state, monkeypatch):
    now = [10.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    long_a = "aaaaaaaaaaaaaaaaaaaaaaa first subject"
    long_b = "bbbbbbbbbbbbbbbbbbbbbbb second subject"
    cli._marquee(long_a, 8)
    now[0] += 30.0                     # scrolled well into A
    fresh = cli._marquee(long_b, 8)
    assert fresh == long_b[:8]         # B starts from column zero


def test_clock_going_backwards_does_not_wedge(marquee_state, monkeypatch):
    text = "a long subject line that scrolls along nicely"
    now = [9000.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    cli._marquee(text, 10)
    now[0] -= 500.0                    # NTP step / suspend-resume
    assert len(cli._marquee(text, 10)) == 10


def test_marquee_writes_only_when_the_subject_changes(marquee_state,
                                                      monkeypatch):
    """One write per new track, not one per redraw — this is per pane, per
    second, forever."""
    text = "a long subject line that scrolls along nicely"
    now = [1.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    cli._marquee(text, 10)
    stamp = (marquee_state / "marquee-status").stat().st_mtime_ns
    for _ in range(5):
        now[0] += 1.0
        cli._marquee(text, 10)
    assert (marquee_state / "marquee-status").stat().st_mtime_ns == stamp


# --- now-playing segment -------------------------------------------------

@pytest.fixture
def channels(tmp_path, monkeypatch):
    """Fake both mpv sockets; `snaps` maps socket path -> props (or None)."""
    snaps: dict[str, dict | None] = {}
    book = tmp_path / "sink-book.sock"
    music = tmp_path / "mopidy-mpv.sock"
    for s in (book, music):
        s.touch()

    monkeypatch.setattr(cli, "state_dir", lambda: tmp_path)
    monkeypatch.setattr("agent_media_core.sinks.music._mpv_socket",
                        lambda: str(music))

    def fake_get_properties(sock, _props):
        snap = snaps.get(str(sock))
        if snap is None:
            raise OSError("not listening")
        return snap

    monkeypatch.setattr("agent_media_core.sinks._mpv_ipc.get_properties",
                        fake_get_properties)
    return snaps, str(book), str(music)


def _playing(title, pos=10.0, dur=100.0, **kw):
    return {"idle-active": False, "pause": False, "time-pos": pos,
            "duration": dur, "media-title": title, **kw}


def test_idle_channels_render_nothing(channels):
    snaps, book, music = channels
    snaps[book] = {"idle-active": True}
    snaps[music] = {"idle-active": True}
    assert cli._now_playing_segment(40) == ""


def test_music_is_rendered_with_times(channels):
    snaps, book, music = channels
    snaps[book] = {"idle-active": True}
    snaps[music] = _playing("Rubber Soul")
    out = cli._now_playing_segment(40)
    assert "Rubber Soul" in out and "♪" in out and "[00:10/01:40]" in out


def test_book_wins_over_music(channels):
    """A book is what the room is listening to; music under it is the bed."""
    snaps, book, music = channels
    snaps[book] = _playing("Besieged")
    snaps[music] = _playing("Rubber Soul")
    out = cli._now_playing_segment(40)
    assert "Besieged" in out and "Rubber Soul" not in out and "📖" in out


def test_paused_shows_the_paused_icon(channels):
    snaps, book, music = channels
    snaps[book] = {"idle-active": True}
    snaps[music] = _playing("Rubber Soul", pause=True)
    assert "⏸" in cli._now_playing_segment(40)


def test_a_dead_socket_is_just_not_playing(channels):
    snaps, book, music = channels
    snaps[book] = None                          # raises on read
    snaps[music] = _playing("Rubber Soul")
    assert "Rubber Soul" in cli._now_playing_segment(40)


def test_segment_respects_its_window(channels):
    snaps, book, music = channels
    snaps[book] = {"idle-active": True}
    snaps[music] = _playing("An extremely long album title that will not fit")
    for window in (20, 30, 46):
        assert len(cli._now_playing_segment(window)) <= window


def test_chapter_is_shown_before_the_title(channels):
    snaps, book, music = channels
    snaps[book] = {"idle-active": True}
    snaps[music] = _playing("Long Album",
                            **{"chapter-metadata/by-key/title": "Track Four"})
    assert cli._now_playing_segment(60).index("Track Four") < \
        cli._now_playing_segment(60).index("Long Album")


def test_segment_never_consults_the_service_layer(channels, monkeypatch):
    """`_srv()` costs ~0.6s and book_now_playing() ~2.6s. Neither may appear on
    a path that runs every second in every pane."""
    snaps, book, music = channels
    snaps[book] = {"idle-active": True}
    snaps[music] = _playing("Rubber Soul")

    def boom():
        raise AssertionError("status must not build the service layer")

    monkeypatch.setattr(cli, "_srv", boom)
    assert "Rubber Soul" in cli._now_playing_segment(40)


# --- speech state: no remote round trip on the status path ---------------

def _mirror(**extras):
    return {"extras": {"writer_pid": None, **extras}}


def test_announced_timeline_is_used_instead_of_the_phone(monkeypatch):
    """~2s per render on this link, once a second in every pane — so when the
    submit process announced a timeline, use it and don't ask."""
    monkeypatch.setattr(cli, "_remote_speech", lambda: True)
    monkeypatch.setattr(cli, "_now_speaking",
                        lambda: _mirror(total_duration_s=9.0, live_pos_s=3.0))

    def boom():
        raise AssertionError("must not round-trip when the timeline is local")

    monkeypatch.setattr(cli, "_remote_snapshot", boom)
    idle, pos, dur, *_ = cli._speech_display_state(prefer_local=True)
    assert (idle, pos, dur) == (False, 3.0, 9.0)


def test_remote_say_still_falls_back_to_the_phone(monkeypatch):
    """The regression this pins, which reached the live bar: the `remote-say`
    path records NO total_duration_s on purpose — nothing local measures audio
    played on another device — so the far side is the only thing that knows the
    utterance is running. Preferring local must not mean refusing to ask."""
    monkeypatch.setattr(cli, "_remote_speech", lambda: True)
    monkeypatch.setattr(cli, "_now_speaking",
                        lambda: _mirror(kind="remote-say", writer_pid=None))
    monkeypatch.setattr(cli, "_remote_snapshot",
                        lambda: {"idle-active": False, "time-pos": 2.0,
                                 "duration": 5.0, "pause": False,
                                 "mute": False, "speed": 1.0})
    idle, pos, dur, *_ = cli._speech_display_state(prefer_local=True)
    assert (idle, pos, dur) == (False, 2.0, 5.0), \
        "a phone-targeted reply must still show a progress bar"


def test_no_remote_opt_out_is_blind_but_cheap(monkeypatch):
    monkeypatch.setattr(cli, "_remote_speech", lambda: True)
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)

    def boom():
        raise AssertionError("allow_remote=False must not round-trip")

    monkeypatch.setattr(cli, "_remote_snapshot", boom)
    assert cli._speech_display_state(allow_remote=False,
                                     prefer_local=True)[0] is True


def test_remote_is_still_used_when_allowed(monkeypatch):
    monkeypatch.setattr(cli, "_remote_speech", lambda: True)
    # No announced reply to lift the reading onto — without this the snapshot
    # is measured against whatever this machine happens to be speaking, so the
    # test passed only on an idle host and failed on a busy one.
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)
    monkeypatch.setattr(cli, "_remote_snapshot",
                        lambda: {"idle-active": False, "time-pos": 3.0,
                                 "duration": 9.0, "pause": False,
                                 "mute": False, "speed": 1.0})
    idle, pos, dur, *_ = cli._speech_display_state(allow_remote=True)
    assert (idle, pos, dur) == (False, 3.0, 9.0)


# --- window sizing -------------------------------------------------------

def test_now_playing_window_scales_and_clamps(monkeypatch):
    monkeypatch.delenv("MEDIA_STATUS_NOW_PLAYING_MIN", raising=False)
    monkeypatch.delenv("MEDIA_STATUS_NOW_PLAYING_MAX", raising=False)
    assert cli._now_playing_window(30) == 18       # narrow phone -> floor
    assert cli._now_playing_window(90) == 30       # scales with the client
    assert cli._now_playing_window(400) == 46      # ultrawide -> ceiling
