"""The spoken agenda.

Audio is linear and unskimmable, so an agenda read verbatim fails hardest
exactly where there is most of it. This one has 322 entries, 153 past their
date and 234 astrological transits. The briefing has to be selective — and
honest about being selective, because a list that stops without saying so is
heard as the whole list.
"""

import datetime as dt

import pytest

from agent_media_core import agenda as ag


TODAY = dt.date(2026, 8, 9)          # a Sunday


def mk(heading, days=None, todo="TODO", done=False, deadline=False,
       file="inbox.org"):
    day = None if days is None else (TODAY + dt.timedelta(days=days)).isoformat()
    return ag._item({
        "todo": todo, "heading": heading, "done": done, "file": file,
        "scheduled": None if deadline else (f"<{day} Sun>" if day else None),
        "deadline": (f"<{day} Sun>" if day else None) if deadline else None,
    })


def _text(items):
    return ag.agenda_text(items, TODAY)


# --- phrasing --------------------------------------------------------------

@pytest.mark.parametrize("days,said", [
    (0, "today"), (1, "tomorrow"), (-1, "yesterday"),
    (2, "Tuesday"), (-3, "3 days ago"), (-7, "about a week ago"),
    (-14, "about 2 weeks ago"),
])
def test_dates_are_said_the_way_a_person_says_them(days, said):
    assert ag.say_date(TODAY + dt.timedelta(days=days), TODAY) == said


def test_a_week_ago_is_not_one_weeks_ago():
    assert "1 weeks" not in ag.say_date(TODAY - dt.timedelta(days=7), TODAY)


def test_deadlines_outrank_scheduling():
    """One is a commitment, the other a plan."""
    it = ag._item({"todo": "TODO", "heading": "h",
                   "scheduled": "<2026-08-20 Thu>",
                   "deadline": "<2026-08-11 Tue>"})
    assert it.when == dt.date(2026, 8, 11)
    assert "due" in ag.say_item(it, TODAY)


def test_scheduled_items_say_scheduled_not_due():
    assert "scheduled" in ag.say_item(mk("h", days=2), TODAY)


# --- selection -------------------------------------------------------------

def test_done_items_are_left_out():
    body = _text([mk("finished", days=0, todo="DONE", done=True),
                  mk("open", days=0)])
    assert "finished" not in body and "open" in body


def test_today_overdue_and_upcoming_are_separated():
    body = _text([mk("due now", days=0), mk("missed", days=-3),
                  mk("soon", days=2)])
    assert "Today." in body and "Past their date." in body
    assert "Coming up." in body


def test_the_summary_leads_with_the_day_and_the_counts():
    first = _text([mk("a", days=0), mk("b", days=-1)]).split("\n\n")[1]
    assert "Sunday 9 August" in first
    assert "1 thing today" in first
    assert "1 item past its date" in first


def test_many_overdue_items_agree_in_number():
    body = _text([mk(f"x{i}", days=-2) for i in range(5)])
    assert "5 items past their date" in body
    assert "past its date" not in body


def test_nothing_today_is_said_rather_than_omitted():
    assert "Nothing scheduled for today." in _text([mk("later", days=5)])


# --- the caps --------------------------------------------------------------

def test_long_lists_are_capped_and_the_remainder_is_spoken():
    body = _text([mk(f"item {i}", days=-2) for i in range(20)])
    assert "And 14 more." in body          # CAP of 6 read, 14 announced
    assert "item 19" not in body


def test_the_remainder_is_not_pluralised_into_mores():
    assert "mores" not in _text([mk(f"i{n}", days=-2) for n in range(20)])


def test_overdue_is_newest_first():
    """Yesterday's miss is actionable; March's is a decision about whether it
    was ever real."""
    body = _text([mk("old", days=-40), mk("recent", days=-1)])
    assert body.index("recent") < body.index("old")


# --- the aside -------------------------------------------------------------

def test_generated_entries_get_their_own_chapter(monkeypatch):
    monkeypatch.setenv("MEDIA_AGENDA_ASIDE_FILES", "astro.org")
    items = ([mk(f"Moon enters {n}", days=-2, file="astro.org")
              for n in ("Aries", "Taurus", "Gemini")]
             + [mk("Real commitment", days=-2)])
    body = _text(items)
    assert "Astrology." in body
    # The real one is not crowded out of the main list by three moon phases.
    main = body.split("Astrology.")[0]
    assert "Real commitment" in main
    assert "Moon enters" not in main


def test_the_aside_still_reports_its_total(monkeypatch):
    monkeypatch.setenv("MEDIA_AGENDA_ASIDE_FILES", "astro.org")
    body = _text([mk(f"t{i}", days=-2, file="astro.org") for i in range(30)])
    assert "30 entries in total." in body


def test_aside_files_are_configurable(monkeypatch):
    monkeypatch.setenv("MEDIA_AGENDA_ASIDE_FILES", "other.org")
    body = _text([mk("a transit", days=-1, file="astro.org")])
    assert "a transit" in body.split("Astrology.")[0] if "Astrology." in body \
        else "a transit" in body


# --- the fallback source ---------------------------------------------------

def test_file_scan_reads_headings_and_dates(tmp_path):
    p = tmp_path / "x.org"
    p.write_text("* TODO Ring the bank :money:\n  SCHEDULED: <2026-08-11 Tue>\n"
                 "* DONE Old thing\n"
                 "* Not a task\n")
    got = ag.entries_via_files([p])
    assert [i.heading for i in got] == ["Ring the bank", "Old thing"]
    assert got[0].scheduled == dt.date(2026, 8, 11)
    assert got[1].done is True
    assert "money" in got[0].tags


def test_file_scan_survives_a_missing_file():
    assert ag.entries_via_files(["/nope/absent.org"]) == []


# --- the provider contract -------------------------------------------------

def test_provider_command_supplies_entries(monkeypatch):
    monkeypatch.setenv(
        "MEDIA_AGENDA_CMD",
        """printf '[{"todo":"TODO","heading":"from the provider",""" +
        """"done":false,"tags":["x"],"scheduled":"<2026-08-09 Sun>"}]'""")
    got = ag.entries_via_command()
    assert [i.heading for i in got] == ["from the provider"]


def test_a_dead_provider_falls_back_rather_than_failing(monkeypatch):
    """Core must keep working with the front end uninstalled, dead or wedged."""
    monkeypatch.setenv("MEDIA_AGENDA_CMD", "false")
    assert ag.entries_via_command() is None


def test_garbage_from_a_provider_is_not_trusted(monkeypatch):
    monkeypatch.setenv("MEDIA_AGENDA_CMD", "echo 'not json at all'")
    assert ag.entries_via_command() is None


def test_no_provider_configured_is_not_an_error(monkeypatch):
    monkeypatch.delenv("MEDIA_AGENDA_CMD", raising=False)
    assert ag.entries_via_command() is None
