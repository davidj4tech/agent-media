"""The live turn's timeline: measured starts beat summed durations.

On the streaming lane `started_at` is stamped at submit, before the first clip
is rendered, and summed clip lengths leave out the gaps between clips — so a
reader following `started_at` + durations ran ahead of the voice.
"""

from __future__ import annotations

import os
import time

from agent_media_core import book_tracks
from agent_media_core.state import store as store_mod

SID = "6c73498c-02c1-4846-8350-a82006973571"


def _row(monkeypatch, extras, started_at):
    class Fake:
        def get_now_playing(self, sink):
            return {"started_at": started_at, "target": "app",
                    "extras": {"source_session": SID, "text": "One. Two.",
                               "writer_pid": os.getpid(), **extras}}
    monkeypatch.setattr(store_mod, "StateStore", Fake)


def test_starts_are_the_timeline_when_present(monkeypatch):
    now = time.time()
    _row(monkeypatch, {"clip_durations_s": [2.0, 3.0],
                       "clip_starts_s": [0.0, 2.6],
                       "play_started_at": now - 3.0}, started_at=now - 9.0)
    live = book_tracks._live_turn(SID)
    assert live["offsets"] == [0.0, 2.6]
    # Counted from when it started playing, not from submit.
    assert 2.9 < live["elapsed"] < 3.5


def test_durations_still_serve_without_starts(monkeypatch):
    _row(monkeypatch, {"clip_durations_s": [2.0, 3.0]}, started_at=time.time())
    assert book_tracks._live_turn(SID)["offsets"] == [0.0, 2.0]


def test_sentences_still_to_come_are_predicted(monkeypatch):
    now = time.time()
    _row(monkeypatch, {"clip_sentences": ["One.", "Two.", "Three.", "Four."],
                       "clip_durations_s": [2.0, 3.0, 4.0, 1.0],
                       "clip_starts_s": [0.0, 2.5],
                       "play_started_at": now - 3.0}, started_at=now - 9.0)
    # Measured for the two begun; the rest follow on from the last one.
    assert book_tracks._live_turn(SID)["offsets"] == [0.0, 2.5, 5.5, 9.5]
