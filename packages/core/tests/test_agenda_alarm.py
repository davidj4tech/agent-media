"""[#A] TODOs read aloud at their clock time (agenda_alarm.py)."""

from __future__ import annotations

import datetime as dt

from agent_media_core import agenda, agenda_alarm

NOW = dt.datetime(2026, 9, 24, 14, 3)


def _it(heading, prio="A", sched=None, dead=None, done=False):
    return agenda._item({"todo": "DONE" if done else "TODO", "heading": heading,
                         "done": done, "priority": prio, "scheduled": sched,
                         "deadline": dead, "file": "inbox.org"})


def test_clock_is_read_off_the_stamp():
    it = _it("x", sched="<2026-09-24 Thu 14:00-15:00>", dead="<2026-09-25 Fri>")
    assert it.scheduled_at == dt.time(14, 0) and it.deadline_at is None


def test_only_open_priority_a_with_a_time_inside_the_window():
    items = [
        _it("call the bank", sched="<2026-09-24 Thu 14:00>"),
        _it("file taxes", dead="<2026-09-24 Thu 13:58>"),
        _it("date only", sched="<2026-09-24 Thu>"),
        _it("priority B", prio="B", sched="<2026-09-24 Thu 14:00>"),
        _it("done", sched="<2026-09-24 Thu 14:00>", done=True),
        _it("too late", sched="<2026-09-24 Thu 13:40>"),
        _it("not yet", sched="<2026-09-24 Thu 14:30>"),
        _it("yesterday", sched="<2026-09-23 Wed 14:00>"),
    ]
    got = [agenda_alarm.phrase(a) for a in agenda_alarm.due(items, NOW)]
    assert got == ["Priority A, due now: file taxes.", "Priority A, now: call the bank."]


def test_each_stamp_is_said_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    items = [_it("call the bank", sched="<2026-09-24 Thu 14:00>")]
    said = []
    speak = lambda t: said.append(t) or True
    assert agenda_alarm.run(NOW, speak, items=items) == ["Priority A, now: call the bank."]
    assert agenda_alarm.run(NOW + dt.timedelta(minutes=1), speak, items=items) == []
    assert said == ["Priority A, now: call the bank."]


def test_a_failed_say_is_tried_again_and_dry_run_marks_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    items = [_it("call the bank", sched="<2026-09-24 Thu 14:00>")]
    assert agenda_alarm.run(NOW, lambda t: False, items=items)
    assert agenda_alarm.run(NOW, dry_run=True, items=items)
    assert agenda_alarm.run(NOW, lambda t: True, items=items)
    assert agenda_alarm.run(NOW, lambda t: True, items=items) == []


def test_the_file_scan_reads_the_priority_cookie(tmp_path):
    f = tmp_path / "inbox.org"
    f.write_text("* TODO [#A] call the bank\n  SCHEDULED: <2026-09-24 Thu 14:00>\n")
    (it,) = agenda.entries_via_files([f])
    assert it.priority == "A" and it.heading == "call the bank"
    assert it.scheduled_at == dt.time(14, 0)
