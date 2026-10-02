"""Can David be spoken to now (free.py), and the gate in submit that waits.

The fail-open half matters most, as with the ringer gate: a report that is
stale, missing or unreadable must end in speech.
"""

from __future__ import annotations

import time

import pytest

from agent_media_core import free, jev
from agent_media_core.intake import submit
from agent_media_core.sinks import speech
from agent_media_core.types import Event, Priority, Source, Target

PHONE = Target(name="sasonica")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_RINGER_TARGET", "sasonica")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("MEDIA_JEV_MODE", raising=False)
    monkeypatch.delenv("MEDIA_FREE_GATE", raising=False)
    monkeypatch.setattr(speech, "read_ringer", lambda *a, **k: None)


# --- the answer --------------------------------------------------------------

def test_no_report_is_free():
    a = free.answer()
    assert a == {"free": True, "why": [], "since": None, "until": None,
                 "age_s": None}


@pytest.mark.parametrize("fields,why", [
    ({"call": True}, ["call"]),
    ({"voice": True}, ["call"]),
    ({"quiet": True}, ["quiet"]),
    ({"meeting_until": time.time() + 600}, ["meeting"]),
    ({"manual_until": time.time() + 600}, ["manual"]),
    ({"quiet": True, "call": True}, ["call", "quiet"]),
])
def test_each_reason(fields, why):
    a = free.report("p8a", fields)
    assert a["free"] is False and a["why"] == why


def test_an_ended_meeting_is_free():
    a = free.report("p8a", {"meeting_until": time.time() - 1,
                            "manual_until": time.time() - 1})
    assert a["free"] is True


def test_a_stale_report_is_free():
    now = time.time()
    free.report("p8a", {"quiet": True}, now=now - free.STALE_S - 1)
    a = free.answer(now=now)
    assert a["free"] is True and a["age_s"] == int(free.STALE_S + 1)


def test_until_is_the_latest_end_and_none_when_open_ended():
    now = time.time()
    a = free.report("p8a", {"meeting_until": now + 600,
                            "manual_until": now + 3600}, now=now)
    assert a["until"] == pytest.approx(now + 3600)
    a = free.report("p8a", {"meeting_until": now + 600, "quiet": True}, now=now)
    assert a["until"] is None


def test_since_is_kept_across_reports():
    t0 = time.time()
    free.report("p8a", {"quiet": True}, now=t0)
    a = free.report("p8a", {"quiet": True, "call": True}, now=t0 + 60)
    assert a["since"] == pytest.approx(t0)
    assert free.report("p8a", {}, now=t0 + 120)["since"] is None


def test_any_device_busy_is_busy():
    free.report("tablet", {})
    assert free.report("p8a", {"call": True})["free"] is False


def test_junk_is_dropped():
    free.report("p8a", {"quiet": "yes", "meeting_until": "soon",
                        "meeting_title": "x" * 500, "evil": 1})
    rep = free._load()["devices"]["p8a"]
    assert rep["quiet"] is True and "meeting_until" not in rep
    assert len(rep["meeting_title"]) == free.TITLE_MAX and "evil" not in rep


def test_an_unreadable_file_is_free():
    free.state_path().parent.mkdir(parents=True, exist_ok=True)
    free.state_path().write_text("{not json")
    assert free.answer()["free"] is True


def test_facts():
    assert free.facts() == {}
    free.report("p8a", {"quiet": True})
    f = free.facts()
    assert f["free"] == "no" and f["free_why"] == "quiet"


# --- the gate ----------------------------------------------------------------

def _reply(priority=Priority.NORMAL, **meta) -> Event:
    return Event(text="the tests passed", source=Source.CLI, target=PHONE,
                 priority=priority, metadata=dict(meta))


def test_free_speaks():
    assert submit._busy_hold(PHONE, _reply()) is None


def test_busy_holds_a_normal_reply():
    free.report("p8a", {"meeting_until": time.time() + 600})
    a = submit._busy_hold(PHONE, _reply())
    assert a and a["why"] == ["meeting"]


@pytest.mark.parametrize("priority", [Priority.HIGH, Priority.URGENT])
def test_high_and_urgent_speak(priority):
    free.report("p8a", {"quiet": True})
    assert submit._busy_hold(PHONE, _reply(priority)) is None


def test_another_target_speaks():
    free.report("p8a", {"quiet": True})
    assert submit._busy_hold(Target(name="rooms"), _reply()) is None


def test_an_already_held_reply_is_left_alone():
    free.report("p8a", {"quiet": True})
    assert submit._busy_hold(PHONE, _reply(held=True)) is None


def test_the_gate_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("MEDIA_FREE_GATE", "0")
    free.report("p8a", {"quiet": True})
    assert submit._busy_hold(PHONE, _reply()) is None


def test_a_broken_answer_speaks(monkeypatch):
    free.report("p8a", {"quiet": True})
    monkeypatch.setattr(free, "answer", lambda: 1 / 0)
    assert submit._busy_hold(PHONE, _reply()) is None


def test_a_held_reply_is_marked_busy():
    ev = _reply()
    submit._hold_for_busy(ev)
    assert ev.metadata["held"] is True and ev.metadata["held_why"] == "busy"


def test_a_busy_alert_is_written_down(tmp_path):
    from agent_media_core.state import StateStore

    store = StateStore(tmp_path / "state.db")
    free.report("p8a", {"call": True})
    hid = submit.submit_event(_reply(alert=True), state=store)
    assert hid is not None
    row = store.recent_history(limit=1)[0]
    ex = row["extras"]
    assert ex["silenced"] == "busy" and ex["busy_why"] == ["call"]
    assert free.held_count(free.answer()["since"], store) == 1


def test_jev_on_and_sure_speaks(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("MEDIA_JEV_MODE", "on")
    monkeypatch.setattr(jev, "ask", lambda s, q, timeout=None: (
        {"breaks_through": {"noul": 0.95}}, "ok", 200))
    free.report("p8a", {"quiet": True})
    assert submit._busy_hold(PHONE, _reply(), "prod is down") is None


def test_jev_in_shadow_never_decides(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    asked = []
    monkeypatch.setattr(jev, "ask", lambda s, q, timeout=None: (
        asked.append(s) or ({"breaks_through": {"noul": 0.95}}, "ok", 200)))
    free.report("p8a", {"quiet": True})
    assert submit._busy_hold(PHONE, _reply(), "prod is down") is not None
    for _ in range(50):
        if asked:
            break
        time.sleep(0.02)
    assert asked and asked[0]["item"]["text"] == "prod is down"
    assert asked[0]["busy_because"] == "quiet"


def test_a_new_meeting_is_put_to_jev_once(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    asked = []
    monkeypatch.setattr(jev, "event_busy", lambda ev, rule: asked.append((ev, rule)))
    end = time.time() + 1800
    free.report("p8a", {"meeting_until": end, "meeting_title": "1:1"})
    free.report("p8a", {"meeting_until": end, "meeting_title": "1:1", "quiet": True})
    for _ in range(50):
        if asked:
            break
        time.sleep(0.02)
    time.sleep(0.05)
    assert len(asked) == 1
    ev, rule = asked[0]
    assert ev["title"] == "1:1" and rule == "hold" and 28 <= ev["minutes"] <= 30
