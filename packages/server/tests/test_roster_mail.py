"""PHS roster emails -> current shifts. Made-up people, places and times."""
import datetime as dt

import pytest

from agent_media_server import roster_calendar as roster
from agent_media_server import roster_mail

HEAD = ('<!DOCTYPE html><html><head><style type="text/css">td{color:#202020}'
        ' /* Day/date: Mon, 01/01/2001 */</style></head><body><table>')


def block(day, time, breaks=""):
    extra = f"<tr><td>Breaks:</td><td>{breaks}</td></tr>" if breaks else ""
    return (f"<tr><td>Location:</td><td>SAMPLE SPA / Treatments</td></tr>"
            f"<tr><td>Day/date:</td><td>{day}</td></tr>"
            f"<tr><td>Time:</td><td>{time}</td></tr>{extra}"
            f"<tr><td>Role:</td><td>Therapist</td></tr>")


def assigned(*blocks):
    return (HEAD + "<p>Hi Alex,</p><p>You have been assigned the following shifts for Sample Spa</p>"
            + "".join(blocks) + "<p>You can log in to the employee portal</p></table></body></html>")


def changed(old, new):
    return (HEAD + "<p>Hi Alex Example,</p><p>A shift you are assigned for Sample Spa has been changed.</p>"
            "<p>Old shift:</p>" + old + "<p>New shift:</p>" + new + "</table></body></html>")


def removed(*blocks):
    return (HEAD + "<p>Hi Alex,</p><p>You are no longer rostered to work the following shifts for Sample Spa</p>"
            + "".join(blocks) + "</table></body></html>")


def message(key, at, body, subject="New shifts are available"):
    # notmuch show --format=json: the text/plain part also holds the HTML.
    return {"id": key, "timestamp": int(at.timestamp()), "headers": {"Subject": subject},
            "body": [{"content-type": "multipart/alternative", "content": [
                {"content-type": "text/plain", "content": body},
                {"content-type": "text/html", "content": body}]}]}


AT = dt.datetime(2026, 10, 20, 9, 0, tzinfo=roster_mail.ZONE)


def test_assigned_notice_reads_each_block_and_ignores_breaks():
    notice = roster_mail.parse(roster_mail.text_of(assigned(
        block("Thu, 05/11/2026", "2:30 pm - 6:15 pm"),
        block("Fri., 06/11/2026", "10:45 am - 6:15 pm", "14:00 pm-14:30 pm (optional)"))))
    assert notice == {"kind": "assigned", "old": [], "new": [
        ["2026-11-05T14:30", "2026-11-05T18:15"], ["2026-11-06T10:45", "2026-11-06T18:15"]]}


def test_changed_and_removed_notices_and_other_payroll_mail():
    change = roster_mail.parse(roster_mail.text_of(changed(
        block("Thu, 22/10/2026", "2:30 pm - 6:15 pm"), block("Thu, 22/10/2026", "2:30 pm - 5:45 pm"))))
    assert change == {"kind": "changed", "old": [["2026-10-22T14:30", "2026-10-22T18:15"]],
                      "new": [["2026-10-22T14:30", "2026-10-22T17:45"]]}
    gone = roster_mail.parse(roster_mail.text_of(removed(block("Tue, 13/10/2026", "2:30 pm - 6:15 pm"))))
    assert gone["kind"] == "removed" and gone["old"] == [["2026-10-13T14:30", "2026-10-13T18:15"]]
    assert roster_mail.parse(roster_mail.text_of(HEAD + "<p>Your payslip is ready</p>")) is None


def test_overnight_shift_ends_next_day():
    notice = roster_mail.parse(roster_mail.text_of(assigned(block("Sat, 03/10/2026", "10:00 pm - 6:00 am"))))
    assert notice["new"] == [["2026-10-03T22:00", "2026-10-04T06:00"]]


@pytest.mark.parametrize("day,time", [
    ("Wed, 05/11/2026", "2:30 pm - 6:15 pm"),   # 5 Nov 2026 is a Thursday: a swapped date
    ("Thu, 05/11/2026", "half past two"),
    ("soon", "2:30 pm - 6:15 pm")])
def test_unreadable_roster_notice_raises(day, time):
    with pytest.raises(ValueError):
        roster_mail.parse(roster_mail.text_of(assigned(block(day, time))))


def test_replay_keeps_id_through_a_change_and_drops_removed():
    first = ["2026-11-05T14:30", "2026-11-05T18:15"]
    later = ["2026-11-05T14:30", "2026-11-05T17:45"]
    other = ["2026-11-06T10:45", "2026-11-06T18:15"]
    shifts, unmatched = roster_mail.replay([
        {"at": 3, "kind": "removed", "old": [other], "new": []},
        {"at": 1, "kind": "assigned", "old": [], "new": [first, other]},
        {"at": 2, "kind": "changed", "old": [first], "new": [later]},
        {"at": 4, "kind": "removed", "old": [["2026-11-09T09:00", "2026-11-09T12:00"]], "new": []}])
    assert shifts == {roster_mail.shift_id(*first): later}
    assert unmatched == 1


def test_change_without_its_assignment_still_lands():
    shifts, unmatched = roster_mail.replay([{"at": 1, "kind": "changed",
        "old": [["2026-11-05T14:30", "2026-11-05T18:15"]], "new": [["2026-11-05T15:00", "2026-11-05T18:15"]]}])
    assert list(shifts.values()) == [["2026-11-05T15:00", "2026-11-05T18:15"]]
    assert unmatched == 1


def fake_mail(monkeypatch, messages):
    monkeypatch.setattr(roster_mail, "_messages", lambda query: list(messages))


def test_read_reports_skipped_recent_notices_and_remembers_parsed_ones(monkeypatch):
    now = AT + dt.timedelta(days=1)
    good = message("a@x", AT, assigned(block("Thu, 05/11/2026", "2:30 pm - 6:15 pm")))
    bad = message("b@x", AT, assigned(block("Thu, 05/11/2026", "whenever")))
    old_bad = message("c@x", AT - dt.timedelta(days=200), assigned(block("Thu, 05/11/2026", "whenever")))
    payslip = message("d@x", AT, HEAD + "<p>Your payslip</p>", subject="Payslip")
    fake_mail(monkeypatch, [good, bad, old_bad, payslip])
    data = roster_mail.read(dt.date(2026, 10, 21), dt.date(2026, 11, 4), now=now)
    assert data["ok"] and data["complete"] and data["skipped"] == 1
    assert data["shifts"] == [{"id": "mail-20261105T1430-225", "start": "2026-11-05T14:30", "end": "2026-11-05T18:15"}]
    # Archived in Gmail (so gone from notmuch): the shift is still known.
    fake_mail(monkeypatch, [])
    again = roster_mail.read(dt.date(2026, 10, 21), dt.date(2026, 11, 4), now=now)
    assert again["shifts"] == data["shifts"] and again["skipped"] == 0
    stored = roster_mail._state_path().read_text()
    assert "Alex" not in stored and "SAMPLE" not in stored


def test_notmuch_failure_raises(monkeypatch):
    monkeypatch.setattr(roster_mail.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    with pytest.raises(RuntimeError):
        roster_mail.read(dt.date(2026, 10, 21), dt.date(2026, 11, 4))


# --- roster_calendar.sync with the mail source ------------------------------------

def mail_setup(monkeypatch, messages, old=()):
    from agent_media_core import config
    monkeypatch.setattr(config, "load", lambda: {"calendar": {"phs": {
        "write_pair": "work", "roster_source": "mail"}}})
    fake_mail(monkeypatch, messages)
    runs = []
    monkeypatch.setattr(roster.subprocess, "run", lambda command, **kw: runs.append(command)
                        or type("Result", (), {"returncode": 0})())
    monkeypatch.setattr(roster.calendar_access, "query", lambda *a: {"events": list(old)})
    monkeypatch.setattr(roster.phs_calendar, "sync", lambda: {"ok": True, "treatments": 0})
    writes, inspected = [], []
    monkeypatch.setattr(roster.calendar_write, "write", lambda event, **kw: writes.append((event, kw)) or {"created": True})
    monkeypatch.setattr(roster.calendar_write, "inspect_event", lambda uid, **kw: inspected.append(uid) or {"exists": True, "etag": '"1"'})
    return writes, inspected, runs


def day_text(day: dt.date) -> str:
    return f"{day:%a}, {day:%d/%m/%Y}"


def test_first_mail_run_replaces_browser_entries_without_duplicates(monkeypatch):
    now = dt.datetime.now(roster.ZONE)
    tomorrow, later = now.date() + dt.timedelta(days=1), now.date() + dt.timedelta(days=3)
    yesterday = now.date() - dt.timedelta(days=1)
    started = (now - dt.timedelta(hours=1)).replace(second=0, microsecond=0)
    ends = started + dt.timedelta(hours=4)
    clock = lambda t: t.strftime("%I:%M %p").lstrip("0").lower()
    messages = [message("a@x", now - dt.timedelta(days=5), assigned(
        block(day_text(tomorrow), "10:45 am - 6:15 pm"), block(day_text(later), "2:30 pm - 6:15 pm")))]
    if started.date() == ends.date():
        messages.append(message("b@x", now - dt.timedelta(days=4), assigned(
            block(day_text(started.date()), f"{clock(started)} - {clock(ends)}"))))
    old = [
        {"uid": "phs-shift-43466750@sasonica", "managed": True, "shift_source": "phs-shift",
         "start_at": f"{tomorrow}T10:45:00+11:00", "end_at": f"{tomorrow}T18:15:00+11:00"},
        {"uid": "phs-shift-43466751@sasonica", "managed": True, "shift_source": "phs-shift",
         "start_at": started.isoformat(), "end_at": ends.isoformat()},
        {"uid": "phs-shift-43466700@sasonica", "managed": True, "shift_source": "phs-shift",
         "start_at": f"{yesterday}T10:00:00+11:00", "end_at": f"{yesterday}T11:00:00+11:00"},
        {"uid": "someone-else@example", "managed": False, "shift_source": "phs-shift",
         "start_at": f"{tomorrow}T10:45:00+11:00", "end_at": f"{tomorrow}T18:15:00+11:00"}]
    writes, inspected, runs = mail_setup(monkeypatch, messages, old)
    result = roster.sync()
    assert result["ok"] and result["roster_source"] == "mail" and result["roster_mail_skipped"] == 0
    created = [event["uid"] for event, kw in writes if not kw.get("delete")]
    assert f"phs-shift-mail-{tomorrow:%Y%m%d}T1045-450@sasonica" in created
    deleted = [event["uid"] for event, kw in writes if kw.get("delete")]
    expected = ["phs-shift-43466750@sasonica"]
    if started.date() == ends.date():
        expected.append("phs-shift-43466751@sasonica")  # under way, replaced by its mail twin
    assert deleted == expected  # never the finished one or another owner's
    assert runs[-1][:2] == ["vdirsyncer", "sync"] and not any("notmuch" in c for c in runs)


def test_mail_source_empty_with_old_shifts_retains_them(monkeypatch):
    tomorrow = dt.datetime.now(roster.ZONE).date() + dt.timedelta(days=1)
    old = [{"uid": "phs-shift-mail-x@sasonica", "managed": True, "shift_source": "phs-shift",
            "start_at": f"{tomorrow}T10:00:00+11:00", "end_at": f"{tomorrow}T12:00:00+11:00"}]
    writes, inspected, _ = mail_setup(monkeypatch, [], old)
    with pytest.raises(RuntimeError, match="unexpectedly empty"):
        roster.sync()
    assert not writes and not inspected


def test_mail_source_failure_retains_entries(monkeypatch):
    writes, inspected, _ = mail_setup(monkeypatch, [])
    def broken(query):
        raise RuntimeError("notmuch unavailable")
    monkeypatch.setattr(roster_mail, "_messages", broken)
    with pytest.raises(RuntimeError, match="previous calendar entries retained"):
        roster.sync()
    assert not writes


def test_unreadable_notice_is_skipped_and_counted_not_fatal(monkeypatch):
    now = dt.datetime.now(roster.ZONE)
    tomorrow = now.date() + dt.timedelta(days=1)
    writes, _, _ = mail_setup(monkeypatch, [
        message("a@x", now - dt.timedelta(days=2), assigned(block(day_text(tomorrow), "10:45 am - 6:15 pm"))),
        message("b@x", now - dt.timedelta(days=1), changed(block("??", "??"), block("??", "??")),
                subject="A shift assigned to you has been changed")])
    result = roster.sync()
    assert result["ok"] and result["roster_mail_skipped"] == 1 and result["roster_shifts"] == 1
