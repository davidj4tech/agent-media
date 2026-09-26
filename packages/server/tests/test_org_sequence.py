"""Sequenced projects closed from the phone: Org's blocking
(`org-enforce-todo-dependencies`, org_edit.blocked_by) and paragtd's one
org-edna trigger (org-paragtd's sequence.py), as `paragtd-sequence-subtree`
writes them."""

from __future__ import annotations

import datetime as dt

import pytest

from agent_media_server import auth, org_edit, org_profile

NOW = dt.datetime(2026, 9, 24, 10, 0)
TRIGGER = 'next-sibling todo!(NEXT) scheduled!("++2d")'

PROJECT = f"""\
#+title: Projects

* TODO Renew the passport
:PROPERTIES:
:ORDERED: t
:END:
** NEXT Get photos taken
:PROPERTIES:
:TRIGGER: {TRIGGER}
:END:
** TODO Fill in the form
:PROPERTIES:
:TRIGGER: {TRIGGER}
:END:
** TODO Post it
* TODO Unordered project
** TODO Either
** TODO Or
"""


@pytest.fixture()
def org(tmp_path, monkeypatch):
    root = tmp_path / "org"
    root.mkdir()
    (root / "inbox.org").write_text("#+title: Inbox\n")
    (root / "next-actions.org").write_text("#+title: Next actions\n\n* Inbox\n")
    (root / "projects.org").write_text(PROJECT)
    monkeypatch.setenv("MEDIA_ORG_DIR", str(root))
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "paragtd")
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}))
    org_profile._reset_for_tests()
    return root


def _at(org, title: str) -> int:
    lines = (org / "projects.org").read_text().splitlines()
    return next(i + 1 for i, ln in enumerate(lines) if ln.endswith(title))


def _close(org, title: str, state: str = "DONE"):
    return org_edit.set_state("projects.org", _at(org, title), title, state, "good", now=NOW)


def test_a_later_step_waits_for_an_earlier_one(org):
    ok, got = _close(org, "Fill in the form")
    assert not ok and got["status"] == 409 and "Get photos taken" in got["error"]
    assert "TODO Fill in the form" in (org / "projects.org").read_text()


def test_a_project_waits_for_its_steps(org):
    ok, got = _close(org, "Renew the passport")
    assert not ok and got["status"] == 409 and "Get photos taken" in got["error"]
    # Without ORDERED the children still block their parent, but not each other.
    assert _close(org, "Or")[0]
    ok, got = _close(org, "Unordered project")
    assert not ok and "Either" in got["error"]


def test_closing_a_step_makes_the_next_one_next_and_dates_it(org):
    ok, got = _close(org, "Get photos taken")
    assert ok and got["trigger"] == "ran"
    assert got["triggered"] == {"title": "Fill in the form", "state": "NEXT",
                                "scheduled": "2026-09-26", "at": _at(org, "Fill in the form")}
    text = (org / "projects.org").read_text()
    assert ("** NEXT Fill in the form\n   SCHEDULED: <2026-09-26 Sat>\n:PROPERTIES:\n"
            f":TRIGGER: {TRIGGER}\n") in text
    assert "** DONE Get photos taken\n   CLOSED: [2026-09-24 Thu 10:00]\n" in text
    # And now it may close in turn; the last step has no trigger.
    ok, got = _close(org, "Fill in the form")
    assert ok and got["triggered"]["title"] == "Post it"
    ok, got = _close(org, "Post it")
    assert ok and "trigger" not in got
    assert _close(org, "Renew the passport")[0]


def test_cancelling_counts_as_finishing(org):
    ok, got = _close(org, "Get photos taken", "CANCELLED")
    assert ok and got["triggered"]["title"] == "Fill in the form"


def test_reopening_triggers_nothing(org):
    _close(org, "Get photos taken")
    ok, got = _close(org, "Get photos taken", "TODO")
    assert ok and "trigger" not in got


def test_another_trigger_is_left_for_emacs(org):
    text = (org / "projects.org").read_text().replace(
        TRIGGER, "ids(abc) todo!(NEXT)", 1)
    (org / "projects.org").write_text(text)
    ok, got = _close(org, "Get photos taken")
    assert ok and got["trigger"] == "skipped"
    assert "** TODO Fill in the form" in (org / "projects.org").read_text()


def test_plain_org_blocks_only_when_asked(org, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "none")
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "c.toml"))
    (org / "projects.org").write_text("#+TODO: TODO NEXT | DONE\n" + PROJECT)
    # Off, as in Org: it closes, and no trigger runs (that is paragtd's).
    ok, got = _close(org, "Fill in the form")
    assert ok and "trigger" not in got
    (tmp_path / "c.toml").write_text("[notes]\nenforce_todo_dependencies = true\n")
    ok, got = _close(org, "Post it")
    assert not ok and "Get photos taken" in got["error"]
