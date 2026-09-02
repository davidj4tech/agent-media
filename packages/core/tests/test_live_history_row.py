"""The turn that is speaking counts as history row 1 while it speaks.

Every lane writes its history row on the way OUT (`add_history` in a finally),
so for the whole time a reply is audible it is missing from the list the popup
traverses — and the previous turn sits where it should be. `r` replayed the
turn before the one playing, `<` stepped back one turn too far, and a
conversation's *first* reply scoped to an empty history, which made every
traversal key do nothing at all for as long as it was talking.

`_speech_history(include_live=True)` closes that by folding now_playing in as
the newest row.
"""

import argparse

import pytest

from agent_media_core import cli


def _row(sess, started_at, text, uri="/clips/old.wav"):
    return {"id": int(started_at), "sink": "speech", "uri": uri,
            "started_at": started_at, "ended_at": started_at + 5,
            "target": "phone", "source": "stop", "text": text,
            "extras": {"source_session": sess, "clip_uris": [uri]}}


@pytest.fixture
def store(monkeypatch):
    """A history of two finished turns in conversation `aaa`."""
    rows = [_row("aaa", 200.0, "second"), _row("aaa", 100.0, "first")]

    class FakeStore:
        def recent_history(self, *, sink=None, limit=20):
            return list(rows)

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    return rows


def _speaking(monkeypatch, *, session="aaa", started_at=300.0,
              uri="/clips/live.wav", extras=None):
    ex = {"source_session": session, "text": "live", "clip_uris": [uri]}
    ex.update(extras or {})
    monkeypatch.setattr(cli, "_now_speaking",
                        lambda: {"uri": uri, "started_at": started_at,
                                 "target": "phone", "extras": ex})


def test_live_turn_is_row_one(store, monkeypatch):
    _speaking(monkeypatch)
    rows = cli._speech_history(3, session="aaa", include_live=True)
    assert [r["text"] for r in rows] == ["live", "second", "first"]
    assert rows[0]["id"] is None                 # not a record yet


def test_live_turn_absent_without_the_flag(store, monkeypatch):
    """`media history` and the id-addressed clip browser see records only."""
    _speaking(monkeypatch)
    rows = cli._speech_history(3, session="aaa")
    assert [r["text"] for r in rows] == ["second", "first"]


def test_live_turn_dedupes_against_its_own_row(store, monkeypatch):
    """As the turn ends its row lands with the same started_at — it must not
    then appear twice."""
    _speaking(monkeypatch, started_at=200.0)
    rows = cli._speech_history(3, session="aaa", include_live=True)
    assert [r["text"] for r in rows] == ["second", "first"]


def test_first_reply_of_a_conversation_scopes_to_itself(store, monkeypatch):
    """The case that read as a dead keybinding: nothing of this conversation
    has finished yet, so scoped history was empty and every key no-opped."""
    _speaking(monkeypatch, session="bbb")
    assert cli._speech_history(3, session="bbb", include_live=True)
    assert cli._anchor_session() == "bbb"


def test_remote_rendered_turn_is_counted_but_not_playable(store, monkeypatch):
    """A lane that renders on the far side records the command it ran, not a
    clip. The row still has to count — otherwise the indices shift back — but
    handing that pseudo-uri to mpv would be handing it a command."""
    _speaking(monkeypatch, uri="remote-say:phone",
              extras={"clip_uris": None, "kind": "remote-say"})
    rows = cli._speech_history(3, session="aaa", include_live=True)
    assert len(rows) == 3
    assert rows[0]["uri"] is None
    assert cli._replay_row(rows[0]) == 1        # refused, cleanly


def test_step_back_lands_on_the_previous_turn(store, monkeypatch, capsys):
    """`<` at the start of a live turn steps back ONE turn. With the live turn
    missing from history that same press skipped to the one before it."""
    _speaking(monkeypatch)
    monkeypatch.setattr(cli, "_speech_display_state",
                        lambda: (False, 0.5, None, False, False, 1.0, True))
    monkeypatch.delenv("MEDIA_POPUP_PREV_RESTART_S", raising=False)
    played = []
    monkeypatch.setattr(cli, "_replay_row", lambda row: played.append(row["text"]) or 0)
    assert cli.cmd_replay_prev(argparse.Namespace(idx=1)) == 0
    assert played == ["second"]
    assert capsys.readouterr().out.strip() == "2"


# --- a replay is not a new turn ---------------------------------------------
#
# _replay_row refreshes now_playing so the cards and the bar follow the clip it
# just started — with a started_at of its own, because that is when it started.
# Folded in as a live row, the turn you were hearing appeared TWICE and every
# index below it shifted by one: `<` stepped from the phantom onto the record
# it mirrored — the same turn, from the top. That was half of "pressing < twice
# just replays the same clip". The stamp that tells them apart is history_id:
# the record the audible turn already occupies, absent on a first-time readout.

def test_a_replay_is_not_a_second_row(store, monkeypatch):
    _speaking(monkeypatch, started_at=300.0, extras={"history_id": 200})
    rows = cli._speech_history(3, session="aaa", include_live=True)
    assert [r["text"] for r in rows] == ["second", "first"]


def test_step_back_during_a_replay_skips_the_turn_being_replayed(
        store, monkeypatch, capsys):
    """`<` while replaying "second" must reach "first", not "second" again."""
    _speaking(monkeypatch, started_at=300.0, extras={"history_id": 200})
    monkeypatch.setattr(cli, "_speech_display_state",
                        lambda: (False, 0.5, None, False, False, 1.0, True))
    monkeypatch.delenv("MEDIA_POPUP_PREV_RESTART_S", raising=False)
    played = []
    monkeypatch.setattr(cli, "_replay_row", lambda row: played.append(row["text"]) or 0)
    assert cli.cmd_replay_prev(argparse.Namespace(idx=1)) == 0
    assert played == ["first"]
    assert capsys.readouterr().out.strip() == "2"


def test_restarting_the_live_turn_does_not_double_it_when_it_lands(monkeypatch):
    """`<` mid-reply restarts a turn that has no record yet, so the mirror it
    leaves cannot name a history id. It names the start the turn will be filed
    under instead — otherwise, the moment the turn ends and its row lands, the
    list held both and `<` stepped onto the turn it was already playing."""
    rows = [_row("aaa", 300.0, "the reply"), _row("aaa", 200.0, "second")]

    class FakeStore:
        def recent_history(self, *, sink=None, limit=20):
            return list(rows)

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    _speaking(monkeypatch, started_at=400.0,          # the restart's own start
              extras={"replay_of_started_at": 300.0})
    out = cli._speech_history(3, session="aaa", include_live=True)
    assert [r["text"] for r in out] == ["the reply", "second"]
