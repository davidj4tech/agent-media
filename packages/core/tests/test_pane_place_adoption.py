"""A clip that lost its tmux session gets it back from its conversation.

The write side asked tmux where a pane was *after* the reply had been rendered
and had waited its turn in the speech queue. A conversation that has just said
goodbye closes its window in that gap, tmux answers a question about a pane
that has gone with a successful empty string, and the clip lands in history
with no session and no window — which is how the last thing said in three
conversations came to be filed on the phone under "no session", next to the
reminders a cron job speaks that never had one.

Fixed where it is written (the hook resolves both names in the pane, at the
moment the turn ends, and sends them along). These are the rows already in the
store, repaired at display time from what their siblings know.
"""

import pytest

from agent_media_core import cli


def _row(rid, at, text, pane, session=None, tmux=None, window=None):
    ex = {"source_pane": pane, "clip_uris": [f"/clips/{rid}.wav"]}
    if session:
        ex["source_session"] = session
    if tmux:
        ex["source_tmux_session"] = tmux
    if window:
        ex["source_window"] = window
    return {"id": rid, "sink": "speech", "uri": f"/clips/{rid}.wav",
            "started_at": at, "target": "phone", "text": text, "extras": ex}


def _history(rows, monkeypatch, n=20):
    class FakeStore:
        def recent_history(self, *, sink=None, limit=20):
            return [dict(r, extras=dict(r["extras"])) for r in rows]

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)
    return {r["id"]: (r.get("extras") or {}) for r in cli._speech_history(n)}


def test_the_goodbye_is_placed_where_the_conversation_was(monkeypatch):
    rows = [
        _row(3, 300.0, "Enjoy the rest of the DJ set.", "%155", "aaa"),
        _row(2, 200.0, "all mine are in", "%155", "aaa", "work", "the ball"),
        _row(1, 100.0, "an answer", "%155", "aaa", "work", "the ball"),
    ]
    got = _history(rows, monkeypatch)
    assert got[3]["source_tmux_session"] == "work"
    assert got[3]["source_window"] == "the ball"
    assert got[3]["place_adopted"] is True
    assert "place_adopted" not in got[2]      # it said so itself


def test_a_clip_with_no_conversation_falls_back_to_its_pane(monkeypatch):
    # The reminders a cron job speaks name no conversation, and its pane has
    # never named one either — but somebody there knew which tmux session it
    # was sitting in, and that belongs to the pane, not to a conversation.
    rows = [
        _row(2, 200.0, "moon enters Libra", "%178"),
        _row(1, 100.0, "sun enters Virgo", "%178", None, "work", "org agenda"),
    ]
    got = _history(rows, monkeypatch)
    assert got[2]["source_tmux_session"] == "work"
    # ...but not to a title. A window name belongs to a conversation, and
    # wearing a borrowed one would file this under work it has no part in.
    assert not got[2].get("source_window")


def test_the_conversation_beats_the_pane(monkeypatch):
    # A conversation resumed into another pane is the same conversation, and
    # the pane it moved into may be sitting in a different tmux session.
    rows = [
        _row(3, 300.0, "the goodbye", "%9", "aaa"),
        _row(2, 250.0, "somebody else in %9", "%9", "bbb", "scratch", "other"),
        _row(1, 100.0, "the same conversation, earlier", "%2", "aaa",
             "work", "the ball"),
    ]
    got = _history(rows, monkeypatch)
    assert got[3]["source_tmux_session"] == "work"


def test_it_takes_the_nearest_in_time_not_the_newest(monkeypatch):
    # A pane outlives the conversations in it: one that held A this morning and
    # B this afternoon must not hand every one of A's clips to B's session.
    rows = [
        _row(4, 400.0, "B, later", "%1", None, "afternoon", "b"),
        _row(3, 390.0, "an aside", "%1"),
        _row(2, 110.0, "another aside", "%1"),
        _row(1, 100.0, "A, earlier", "%1", None, "morning", "a"),
    ]
    got = _history(rows, monkeypatch)
    assert got[3]["source_tmux_session"] == "afternoon"
    assert got[2]["source_tmux_session"] == "morning"


def test_a_window_of_its_own_is_not_overwritten(monkeypatch):
    # The window name is the conversation's title as it stood when that clip
    # spoke. A later one is a worse answer than the one already recorded.
    rows = [
        _row(2, 200.0, "the goodbye", "%1", "aaa", window="what it was called"),
        _row(1, 100.0, "earlier", "%1", "aaa", "work", "renamed since"),
    ]
    got = _history(rows, monkeypatch)
    assert got[2]["source_tmux_session"] == "work"
    assert got[2]["source_window"] == "what it was called"


def test_a_pane_nobody_ever_named_keeps_its_blank(monkeypatch):
    # Honest: the answer is not in front of us. Inventing one would put a clip
    # in a place it was never said.
    rows = [_row(1, 100.0, "moon enters Libra", "%178")]
    got = _history(rows, monkeypatch)
    assert not got[1].get("source_tmux_session")
    assert "place_adopted" not in got[1]
