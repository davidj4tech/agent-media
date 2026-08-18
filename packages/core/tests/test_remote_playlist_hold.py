"""The remote (tcp://) playlist path must not release the speech token the
instant it loses the follow-along.

Regression for: a long reply on the phone path (gapless remote playlist) got
cut off by a shorter queued reply and never resumed. Cause — when a flaky
bridge tripped the follow loop's misses/stall guards *while the phone was still
playing*, the loop broke straight to `finally` and released the playback lock;
the next queued (equal-priority) reply grabbed it and `play_playlist`
stop+cleared the still-playing audio. The fix keeps holding the token past a
bail until it can positively confirm the player is idle (or the reply's own
duration has elapsed) — the "blind-hold".
"""

from pathlib import Path

import pytest

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source, Target


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")
    # Make "phone" a tcp:// target so submit_event takes the remote-playlist path.
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")
    # No real sleeping — the follow loop's per-tick sleeps would make this ~5s.
    monkeypatch.setattr(S.time, "sleep", lambda *_a, **_k: None)
    return tmp_path


def _fake_render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


class _RecordingCoord:
    def __init__(self):
        self.before = 0
        self.after = 0

    def pre_pause_remote(self):
        pass

    def before_speech(self, title="", priority="", defer_music=False,
                          text=""):
        self.before += 1

    def speaking_line(self, text=""): pass
    def after_speech(self):
        self.after += 1

    # Never reached in this scenario (mute watcher only polls on readable snaps),
    # but present so an accidental call is a no-op rather than an AttributeError.
    def release_music_duck(self):
        pass

    def reapply_music_duck(self):
        pass


class _DeadBridgeThenIdleSink:
    """Snapshot reads fail (dead bridge) long enough to trip the misses bail,
    then come back reporting idle — so the blind-hold can release cleanly.

    Fifty-one unreadable snapshots trip the follow loop's ``misses > 50`` guard;
    a correct blind-hold then polls at least once more (call 52) and, seeing a
    *readable* idle, releases. Without the hold the loop would return at 51.
    """

    def __init__(self):
        self.snapshot_calls = 0
        self.playlists = 0

    def prefetch(self, paths, target=None):
        pass

    def play_playlist(self, uris, target=None, gapless=True):
        self.playlists += 1

    def snapshot(self, target=None):
        self.snapshot_calls += 1
        if self.snapshot_calls <= 51:
            return None                      # bridge unreadable -> misses bail
        return {"idle-active": True}         # recovered: positively idle

    def set_playlist_pos(self, pos, target=None):
        pass

    def stop(self, target=None):
        pass

    def muted(self, target):
        return False

    def idle(self, target):
        # Reports idle on IPC error too — must NOT be trusted by the blind-hold.
        return True


def _phone_event():
    return Event(text="First sentence here. Second sentence here.",
                 source=Source.CLI, target=Target(name="phone"),
                 metadata={"pane": "%7"})


def test_bail_holds_token_until_idle_confirmed(state_env, monkeypatch):
    monkeypatch.setattr(S, "render_text", _fake_render)
    state = StateStore()
    coord, sink = _RecordingCoord(), _DeadBridgeThenIdleSink()

    rid = S.submit_event(_phone_event(), state=state, sink=sink, coordinator=coord)

    # It played (remote playlist loaded) and finished cleanly.
    assert rid is not None
    assert sink.playlists == 1
    # The follow loop bailed at 51 unreadable snapshots; the blind-hold then
    # polled again instead of releasing — so snapshot ran past the bail point.
    # (Without the hold this would be exactly 51.)
    assert sink.snapshot_calls > 51
    # Duck bracketed exactly once and nothing was left playing.
    assert (coord.before, coord.after) == (1, 1)
    assert state.get_now_playing("speech") is None
