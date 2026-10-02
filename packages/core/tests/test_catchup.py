"""The catch-up: when it fires, what it reads, and that it always says something."""

from __future__ import annotations

import time

import pytest

from agent_media_core import catchup
from agent_media_core.state import StateStore

T = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(catchup, "_LAST", None)
    monkeypatch.setattr(catchup, "_listeners", [])
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent")


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


def test_deliver_records_tells_and_says(monkeypatch):
    from agent_media_core.intake import _summary
    monkeypatch.setattr(_summary, "_chat", lambda *a, **k: None)
    said, poked = [], []
    monkeypatch.setattr(catchup, "_say", said.append)
    catchup.on_made(lambda: poked.append(1))
    items = [{"id": 3, "kind": "reply", "thread": "radio", "text": "a"},
             {"id": 4, "kind": "alert", "thread": "", "text": "disk"},
             {"id": 5, "kind": "digest", "thread": "", "text": "agenda"}]
    rec = catchup.deliver(items, {"quiet"}, T)
    assert rec == catchup.last()
    assert (rec["replies"], rec["alerts"], rec["digests"]) == (1, 1, 1)
    assert rec["how"] == "template" and rec["why"] == ["quiet"]
    assert rec["text"].startswith("While you were away: one reply from radio; one alert")
    assert "one digest to play" in rec["text"]
    assert poked and said[0]["items"] == [3, 4, 5] and said[0]["text"] == rec["text"]
    assert catchup.deliver([], {"quiet"}, T) is None and len(said) == 1


def test_say_speaks_at_high(monkeypatch):
    from agent_media_core.intake import submit
    from agent_media_core.types import Priority
    got = []
    monkeypatch.setattr(submit, "submit_event", lambda ev: got.append(ev) or 7)
    assert catchup.say({"text": "While you were away…", "items": [3], "how": "summary"}) == 7
    ev = got[0]
    assert ev.priority is Priority.HIGH and ev.metadata["kind"] == "catchup"
    assert ev.metadata["catchup_items"] == [3]
    assert catchup.say({"text": " "}) is None


def test_a_digest_held_in_the_spell_joins_it(tmp_path):
    s = _store(tmp_path)
    _row(s, T + 1, "Agenda for today", held=True, digest="agenda")
    _row(s, T + 2, "heard digest", held=True, digest="x", heard=True)
    assert [i["kind"] for i in catchup.collect(s, T, T + 9)] == ["digest"]


def test_on_demand_takes_whatever_waits(tmp_path, monkeypatch):
    s = _store(tmp_path)
    now = time.time()
    _row(s, now - 13 * 3600, "too old", held=True)
    _row(s, now - 60, "unwatched", held=True, session="a")
    _row(s, now - 50, "ringer alert", silenced="ringer", alert=True)
    _row(s, now - 40, "heard", held=True, heard=True)
    _row(s, now - 30, "spoken", session="a")
    started = []
    monkeypatch.setattr(catchup.threading, "Thread",
                        lambda target, args, **k: type("T", (), {
                            "start": lambda self: started.append(args)})())
    assert catchup.request({"a": "radio"}.get, store=s, now=now) == 2
    items, why, since = started[0]
    assert [i["text"] for i in items] == ["unwatched", "ringer alert"]
    assert why == set()


def test_on_demand_begins_with_what_waits():
    t = catchup.template([{"kind": "reply", "thread": "radio", "text": "a"}], set())
    assert t.startswith("Here's what's waiting: one reply from radio.")


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
