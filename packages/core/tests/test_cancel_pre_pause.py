"""A reply that started pausing other media and then never spoke gives it
back: the pre-pause is undone, not left for an after_speech that never runs
(David, 28 Sep 2026: a When open reply, not open, paused his audio for good)."""

from __future__ import annotations

import threading

from agent_media_core.route import coordinator as coord_mod
from agent_media_core.route.coordinator import Coordinator


class _Half:
    """Just the state pre_pause_remote leaves behind."""
    cancel_pre_pause = Coordinator.cancel_pre_pause

    def __init__(self):
        self.flags = []
        self._remote_pause_done = threading.Event()
        self._remote_pause_done.set()
        self._mpris_remote_paused = {"desk": ["spotify"]}
        self._android_paused = ["p8a"]

    def _speaking(self, on):
        self.flags.append(on)


def test_undoes_what_the_pre_pause_paused(monkeypatch):
    resumed = []
    monkeypatch.setattr(coord_mod._mpris, "resume_remote",
                        lambda host, names: resumed.append(("mpris", host, names)))
    monkeypatch.setattr(coord_mod._android, "resume",
                        lambda host: resumed.append(("android", host)))
    c = _Half()
    c.cancel_pre_pause()
    assert resumed == [("mpris", "desk", ["spotify"]), ("android", "p8a")]
    assert c.flags == [False]
    assert c._android_paused == [] and c._mpris_remote_paused == {}
    assert c._remote_pause_done is None
    # Twice is harmless: nothing is resumed again.
    c.cancel_pre_pause()
    assert len(resumed) == 2


def test_a_failed_resume_does_not_stop_the_rest(monkeypatch):
    resumed = []

    def boom(host, names):
        raise OSError("ssh")

    monkeypatch.setattr(coord_mod._mpris, "resume_remote", boom)
    monkeypatch.setattr(coord_mod._android, "resume", lambda host: resumed.append(host))
    _Half().cancel_pre_pause()
    assert resumed == ["p8a"]
