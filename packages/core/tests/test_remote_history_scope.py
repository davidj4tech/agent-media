"""Phone-lane replies must be findable by the popup's history navigation.

`media replay` and the popup's < / > scope traversal to one conversation via
extras.source_session, and rows carrying no tag are deliberately *excluded*
rather than leaked across conversations. The remote-say path wrote only
`session`, so every reply spoken on the phone was invisible to that scoping:
`r` reported nothing to replay and traversal skipped a whole lane's worth of
speech — while the very same clip replayed fine when addressed by id, which is
what made it look like replay itself was broken.
"""

import json

import pytest

from agent_media_core import cli
from agent_media_core.intake import submit
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Priority, Source


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(submit, "_wait_speech_hold", lambda *a, **k: None)
    monkeypatch.setattr(submit, "_speech_flushed", lambda *a, **k: False)
    monkeypatch.setattr(submit, "_tmux_session_for_pane", lambda p: "tmux-1")
    monkeypatch.setattr(submit, "_tmux_window_for_pane", lambda p: "win-1")
    return StateStore()


class _Coord:
    def before_speech(self): pass
    def after_speech(self): pass


def _say(store, text="a reply", session="sess-abc", pane="%42"):
    ev = Event(text=text, source=Source.CLAUDE_CODE, priority=Priority.NORMAL,
               metadata={"session": session, "pane": pane, "kind": "stop"})
    submit._submit_remote_say(text, "cat >/dev/null", _Coord(), store, ev)


def _extras(store):
    row = store.recent_history(sink="speech", limit=1)[0]
    ex = row.get("extras") or {}
    return ex if isinstance(ex, dict) else json.loads(ex or "{}")


def test_remote_row_carries_the_scoping_tags(store):
    _say(store)
    ex = _extras(store)
    assert ex["source_session"] == "sess-abc"
    assert ex["source_pane"] == "%42"
    assert ex["source_tmux_session"] == "tmux-1"
    assert ex["source_window"] == "win-1"


def test_session_scoped_history_finds_a_phone_lane_reply(store, monkeypatch):
    """The actual failure: `media replay` found nothing to replay."""
    _say(store, text="spoken on the phone", session="sess-abc")

    monkeypatch.setattr(cli, "StateStore", lambda: store)
    rows = cli._speech_history(5, session="sess-abc")
    assert [r["text"] for r in rows] == ["spoken on the phone"], (
        "a reply spoken on the phone was invisible to session-scoped history, "
        "so `r` had nothing to replay")


def test_other_conversations_are_still_excluded(store, monkeypatch):
    _say(store, text="mine", session="sess-abc")
    _say(store, text="theirs", session="sess-xyz")

    monkeypatch.setattr(cli, "StateStore", lambda: store)
    rows = cli._speech_history(5, session="sess-abc")
    assert [r["text"] for r in rows] == ["mine"]


def test_pane_lookup_finds_the_remote_row(store, monkeypatch):
    _say(store, pane="%42")
    monkeypatch.setattr(cli, "StateStore", lambda: store)
    assert cli._history_index_for_pane("%42") == 1
    assert cli._history_index_for_pane("%99") is None
