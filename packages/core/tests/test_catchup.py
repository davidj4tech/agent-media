"""The catch-up: when it fires, what it reads, and that it always says something."""

from __future__ import annotations

import time

import pytest

from agent_media_core import catchup
from agent_media_core.state import StateStore

T = 1_800_000_000.0


def _busy(*why, since=T):
    return {"free": False, "why": list(why), "since": since}


FREE = {"free": True, "why": [], "since": None}


# --- when ---------------------------------------------------------------------

def test_free_all_along_is_nothing():
    w = catchup.Watcher()
    assert w.tick(FREE, T) is None and w.spell is None


def test_a_call_settles_for_30_s():
    w = catchup.Watcher()
    w.tick(_busy("call"), T)
    assert w.tick(FREE, T + 100) is None
    assert w.tick(FREE, T + 129) is None
    s = w.tick(FREE, T + 130)
    assert s.since == T and s.why == {"call"}
    assert w.tick(FREE, T + 200) is None, "once per spell"


def test_quiet_settles_for_60_s_and_the_longest_wins():
    w = catchup.Watcher()
    w.tick(_busy("call"), T)
    w.tick(_busy("quiet"), T + 10)
    w.tick(FREE, T + 20)
    assert w.tick(FREE, T + 79) is None
    assert w.tick(FREE, T + 80).why == {"call", "quiet"}


def test_a_meeting_is_caught_up_at_once():
    w = catchup.Watcher()
    w.tick(_busy("meeting"), T)
    assert w.tick(FREE, T + 3600).why == {"meeting"}


def test_busy_again_within_the_settle_goes_on():
    w = catchup.Watcher()
    w.tick(_busy("call"), T)
    w.tick(FREE, T + 10)
    w.tick(_busy("call", since=T + 30), T + 30)
    assert w.tick(FREE, T + 50) is None
    s = w.tick(FREE, T + 80)
    assert s.since == T, "one spell, from its first start"


# --- what -----------------------------------------------------------------------

def _store(tmp_path):
    return StateStore(tmp_path / "state.db")


def _row(store, at, text, **extras):
    return store.add_history(sink="speech", uri="x", started_at=at, ended_at=at,
                             target="sasonica", source="claude-code", text=text,
                             extras=extras)


def test_collect_takes_the_spells_held_items(tmp_path):
    s = _store(tmp_path)
    _row(s, T - 10, "before", held=True, held_why="busy", session="a")
    _row(s, T + 1, "the tests passed", held=True, held_why="busy", session="a")
    _row(s, T + 2, "heard already", held=True, held_why="busy", heard=True)
    _row(s, T + 3, "unwatched, not busy", held=True, session="a")
    _row(s, T + 4, "disk at 93%", silenced="busy", alert=True)
    _row(s, T + 5, "ringer, not busy", silenced="ringer", alert=True)
    _row(s, T + 6, "spoken", session="a")
    items = catchup.collect(s, T, T + 100, {"a": "radio"}.get)
    assert [(i["kind"], i["thread"], i["text"]) for i in items] == [
        ("reply", "radio", "the tests passed"),
        ("alert", "", "disk at 93%"),
    ]


def test_a_thread_with_no_title_is_named_by_its_tmux_session(tmp_path):
    s = _store(tmp_path)
    _row(s, T + 1, "x", held=True, held_why="busy", session="b",
         source_tmux_session="agent-media")
    assert catchup.collect(s, T, T + 9, lambda _s: None)[0]["thread"] == "agent-media"


def test_template_counts_and_names():
    items = [{"kind": "reply", "thread": "radio", "text": "a"},
             {"kind": "reply", "thread": "radio", "text": "b"},
             {"kind": "reply", "thread": "agent-media", "text": "c"},
             {"kind": "alert", "thread": "", "text": "disk at 93%\nmore"}]
    t = catchup.template(items, {"meeting"})
    assert t.startswith("While you were in a meeting: 3 replies from radio and agent-media; ")
    assert "one alert, the first: disk at 93%." in t


def test_compose_falls_back_to_the_template(monkeypatch):
    from agent_media_core.intake import _summary
    monkeypatch.setattr(_summary, "_chat", lambda *a, **k: None)
    items = [{"kind": "reply", "thread": "radio", "text": "a"}]
    text, how = catchup.compose(items, {"call"})
    assert how == "template" and text.startswith("While you were on a call: one reply from radio.")


def test_compose_uses_the_summary(monkeypatch):
    from agent_media_core.intake import _summary
    seen = {}

    def chat(prompt, material, timeout, model=None):
        seen["prompt"], seen["material"] = prompt, material
        return "While you were on a call, the radio thread finished."
    monkeypatch.setattr(_summary, "_chat", chat)
    text, how = catchup.compose([{"kind": "reply", "thread": "radio", "text": "done"}],
                                {"call"})
    assert how == "summary" and text.endswith("finished.")
    assert "[reply — radio]\ndone" in seen["material"]
    assert "While you were on a call" in seen["prompt"]


def test_speak_says_it_at_high(monkeypatch):
    from agent_media_core.intake import _summary, submit
    from agent_media_core.types import Priority
    monkeypatch.setattr(_summary, "_chat", lambda *a, **k: None)
    said = []
    monkeypatch.setattr(submit, "submit_event", lambda ev: said.append(ev) or 7)
    items = [{"id": 3, "kind": "reply", "thread": "radio", "text": "a"}]
    assert catchup.speak(items, {"quiet"}, T) == 7
    ev = said[0]
    assert ev.priority is Priority.HIGH
    assert ev.metadata["kind"] == "catchup" and ev.metadata["catchup_items"] == [3]
    assert catchup.speak([], {"quiet"}, T) is None and len(said) == 1


def test_a_catchup_passes_the_busy_gate(monkeypatch):
    from agent_media_core import free
    from agent_media_core.intake import submit
    from agent_media_core.types import Event, Priority, Source, Target
    monkeypatch.setenv("MEDIA_RINGER_TARGET", "sasonica")
    free.report("p8a", {"quiet": True})
    ev = Event(text="While you were away…", source=Source.WATCHER,
               priority=Priority.HIGH, target=Target(name="sasonica"),
               metadata={"kind": "catchup"})
    assert submit._busy_hold(Target(name="sasonica"), ev) is None
