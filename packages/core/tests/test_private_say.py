"""A private reading — an email read aloud on a tap, `media say --private`
(David, 20 Oct 2026: "we're not keeping a clip cache anymore") — is played
and never kept: no history row (so recent, history, catch-up and replay
never list it), no words in the speech event log, and its rendered clips
removed with the text kept beside them. Nothing here plays: every MEDIA_*
and RELAY_* setting is stripped, HOME and the state dirs are throwaway, and
submit_event is replaced where `say` would reach it."""

from __future__ import annotations

import argparse
import json
import os

import pytest


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    for d in ("home", "state", "cache", "config"):
        (tmp_path / d).mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_a_private_row_is_never_written(tmp_path):
    from agent_media_core.state import StateStore

    st = StateStore(tmp_path / "state" / "t.db")
    kept = st.add_history(sink="speech", uri="/x.ogg", started_at=1.0, text="hello",
                          extras={"session": "s"})
    gone = st.add_history(sink="speech", uri="/y.ogg", started_at=2.0,
                          text="From Lionel. Dinner. See you at eight.",
                          extras={"private": True})
    assert kept and gone == 0
    texts = [r.get("text") for r in st.recent_history(sink="speech", limit=10)]
    assert texts == ["hello"]


def test_the_event_log_never_keeps_its_words(tmp_path):
    from agent_media_core.intake import submit

    submit._speech_event("start", text="From Lionel. Dinner.", private=True, session="s")
    submit._speech_event("start", text="a reply", private=False, session="s")
    lines = [json.loads(x) for x in submit._speech_events_path().read_text().splitlines()]
    assert [x["text"] for x in lines] == ["(private)", "a reply"]
    assert "private" not in lines[0]


def test_its_clips_and_their_words_are_removed(tmp_path):
    from agent_media_core.intake import submit

    d = tmp_path / "state" / "audio"
    d.mkdir()
    mine = [d / "20261020T1-mail--000.ogg", d / "20261020T1-mail--001.ogg"]
    for p in mine:
        p.write_bytes(b"ogg")
        p.with_suffix(".txt").write_text("From Lionel")
        p.with_suffix(".tts").write_text("x")
    other = d / "20261020T2-reply--000.ogg"
    other.write_bytes(b"ogg")
    other.with_suffix(".txt").write_text("a reply")
    submit._forget_clips([str(p) for p in mine])
    assert sorted(x.name for x in d.iterdir()) == [other.name, other.with_suffix(".txt").name]


def test_say_private_marks_the_message(monkeypatch):
    from agent_media_core import cli
    from agent_media_core.intake import submit

    got = []
    monkeypatch.setattr(submit, "submit_event", lambda event, **k: got.append(event))
    a = argparse.Namespace(text="From Lionel. Dinner.", private=True, urgent=False,
                           supersede=False, alert=False, hold=False, key=None,
                           label=None, digest=None)
    assert cli.cmd_say(a) == 0
    assert got and got[0].metadata.get("private") is True
    got.clear()
    a.private = False
    cli.cmd_say(a)
    assert "private" not in got[0].metadata
