"""Mid-response mute toggles the music duck (release on mute, re-duck on
unmute) by reusing the interruption marker before_speech stashed, without
clearing it — after_speech still owns the final restore."""

from agent_media_core.route import coordinator as coord_mod


class _FakeMusic:
    def __init__(self):
        self.calls = []

    def duck(self, target, level):
        self.calls.append(("duck", level))

    def unduck(self, target, restore=100):
        self.calls.append(("unduck", restore))


class _FakeState:
    """Returns a fixed now_playing("music") marker; records clears."""

    def __init__(self, interruption):
        self._np = ({"extras": {"interruption": interruption}}
                    if interruption is not None else None)
        self.cleared = []

    def get_now_playing(self, sink):
        return self._np

    def clear_now_playing(self, sink):
        self.cleared.append(sink)


def _coord(interruption):
    music = _FakeMusic()
    state = _FakeState(interruption)
    c = coord_mod.Coordinator(music=music, state=state)
    return c, music, state


def test_release_and_reapply_use_marker_values():
    c, music, state = _coord(
        {"strategy": "duck", "duck_level": 12, "baseline_volume": 45})

    c.release_music_duck()
    c.reapply_music_duck()

    assert music.calls == [("unduck", 45), ("duck", 12)]
    # The marker must survive so after_speech can still restore.
    assert state.cleared == []


def test_pause_strategy_is_left_alone():
    c, music, _ = _coord({"strategy": "pause", "pause_pos_ms": 1000})

    c.release_music_duck()
    c.reapply_music_duck()

    assert music.calls == []  # only duck-strategy music is toggled


def test_no_marker_is_noop():
    c, music, _ = _coord(None)

    c.release_music_duck()
    c.reapply_music_duck()

    assert music.calls == []


def test_watcher_fires_on_mute_edges_only():
    from agent_media_core.intake import submit

    c, music, _ = _coord(
        {"strategy": "duck", "duck_level": 10, "baseline_volume": 45})

    class _FakeSink:
        def __init__(self):
            self.mute = False

        def muted(self, target):
            return self.mute

    sink = _FakeSink()
    w = submit._MuteDuckWatcher(sink, object(), c)

    w.poll()              # audible, no edge
    assert music.calls == []

    sink.mute = True
    w.poll()              # edge → release
    w.poll()              # held muted, no repeat
    assert music.calls == [("unduck", 45)]

    sink.mute = False
    w.poll()              # edge → re-duck
    assert music.calls == [("unduck", 45), ("duck", 10)]
