"""The thread-watching table is the running server's alone (core's watching.py).

A test's thread stream that closed after its teardown had restored the real
XDG_STATE_HOME wrote `{}` over the live table under pytest's pid, and an open
thread's replies were held as toasts until it reopened (19 Oct 2026). Now a
stream publishes only to the path the server pinned at its start.
"""

from __future__ import annotations

import json

from agent_media_core import watching
from agent_media_server import thread_events


def test_a_stream_in_a_process_that_never_pinned_writes_nothing(tmp_path, monkeypatch):
    real = tmp_path / "real-state"          # stands in for the live state dir
    monkeypatch.setenv("XDG_STATE_HOME", str(real))
    with thread_events._LOCK:
        thread_events._publish()            # as a watcher closing late would
    thread_events._reset_for_tests()
    assert not (real / "agent-media" / watching.NAME).exists()
    assert not any(real.rglob(watching.NAME))


def test_a_pinned_server_keeps_its_path_when_the_environment_moves(tmp_path, monkeypatch):
    mine = tmp_path / "server-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(mine))
    pinned = watching.pin()
    elsewhere = tmp_path / "restored-state"  # what a teardown put back
    monkeypatch.setenv("XDG_STATE_HOME", str(elsewhere))
    with thread_events._LOCK:
        thread_events._publish()
    assert pinned.is_file() and pinned.is_relative_to(mine)
    assert json.loads(pinned.read_text())["sessions"] == {}
    assert not any(elsewhere.rglob(watching.NAME))
    watching._reset_for_tests()
