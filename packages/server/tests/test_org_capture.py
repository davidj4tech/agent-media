"""Capture templates filled and filed without Emacs (org_capture.py), and
offered by /org and taken by /org/capture under paragtd's profile."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from agent_media_server import auth, org as org_mod, org_capture as nc, org_profile

NOW = dt.datetime(2026, 9, 24, 17, 5)


# --- filling -------------------------------------------------------------------------

def test_what_a_phone_can_fill():
    assert nc.fields("* TODO %?\n:CREATED: %U\n%i%a %<%Y>") == []
    got = nc.fields("* %^{Name} %^{State|Calm|Busy} %^{When}t %^T %^{Name}")
    assert got == [
        {"id": "f0", "label": "Name", "type": "text"},
        {"id": "f1", "label": "State", "type": "choice", "options": ["Calm", "Busy"]},
        {"id": "f2", "label": "When", "type": "date", "active": True},
        {"id": "f3", "label": "Date and time", "type": "datetime", "active": True},
    ]


@pytest.mark.parametrize("tpl", ["* %(ryer/prompt-topics)", "* %[~/x]", "* %^g", "* %:subject", "* %c"])
def test_what_needs_emacs(tpl):
    assert nc.fields(tpl) is None
    with pytest.raises(nc.CaptureError):
        nc.fill(tpl, "x", {}, NOW)


def test_filling():
    tpl = "* TODO %?\nSCHEDULED: %^T\n:PROPERTIES:\n:CREATED: %U\n:SEEN: %u %t %T\n:END:\n%<Week %W>\n"
    got = nc.fill(tpl, "Ring the bank\nabout the card\n* not a heading", {"f0": "2026-10-01T09:30"}, NOW)
    assert got == ("* TODO Ring the bank\nSCHEDULED: <2026-10-01 Thu 09:30>\n:PROPERTIES:\n"
                   ":CREATED: [2026-09-24 Thu 17:05]\n:SEEN: [2026-09-24 Thu] <2026-09-24 Thu> "
                   "<2026-09-24 Thu 17:05>\n:END:\nWeek 38\nabout the card\n * not a heading\n")
    # A choice left empty is its first option; the same prompt twice is one field.
    assert nc.fill("* %^{A} %^{S|x|y} %^{A}", "", {"f0": "Ann"}, NOW) == "* Ann x Ann\n"


def test_a_missing_field_is_refused():
    with pytest.raises(nc.CaptureError, match="Name"):
        nc.fill("* %^{Name}", "", {}, NOW)
    with pytest.raises(nc.CaptureError, match="date"):
        nc.fill("* %^t", "", {"f0": "soon"}, NOW)


# --- filing --------------------------------------------------------------------------

def test_file_and_prepend():
    lines = ["#+title: X", "", "* old", ""]
    at = nc.place(lines, {"target": "file"}, "* new\nbody\n", NOW)
    assert (at, lines) == (3, ["#+title: X", "", "* old", "* new", "body", ""])
    lines = ["#+title: X", "", "* old"]
    at = nc.place(lines, {"target": "file", "prepend": True}, "* new\n", NOW)
    assert (at, lines) == (2, ["#+title: X", "", "* new", "* old"])


def test_under_a_headline():
    lines = ["* Inbox", "** a", "* Other", "** b"]
    at = nc.place(lines, {"target": "file+headline", "headline": "Inbox"}, "* NEXT c\n** sub\n", NOW)
    assert (at, lines) == (2, ["* Inbox", "** a", "** NEXT c", "*** sub", "* Other", "** b"])
    lines = ["#+title: W"]
    nc.place(lines, {"target": "file+headline", "headline": "Waiting"}, "* WAITING d\n", NOW)
    assert lines == ["#+title: W", "", "* Waiting", "** WAITING d"]


def test_a_week_tree_in_date_order():
    lines = ["#+title: J", "* 2026", "** 2026-W38", "*** 2026-09-18 Friday", "**** old",
             "** 2026-W41", "*** 2026-10-05 Monday"]
    tpl = {"target": "file+olp+datetree", "tree_type": "week"}
    at = nc.place(lines, tpl, "* [now] hi\n", NOW)
    assert lines[5:8] == ["** 2026-W39", "*** 2026-09-24 Thursday", "**** [now] hi"]
    assert at == 7 and lines[8] == "** 2026-W41"
    # The same day again goes under the same heading, after what is there.
    nc.place(lines, tpl, "* again\n", NOW)
    assert lines[5:9] == ["** 2026-W39", "*** 2026-09-24 Thursday", "**** [now] hi", "**** again"]


def test_a_month_tree():
    lines: list[str] = []
    nc.place(lines, {"target": "file+olp+datetree"}, "* e\n", NOW)
    assert lines == ["* 2026", "** 2026-09 September", "*** 2026-09-24 Thursday", "**** e"]


def test_the_target_stays_in_the_tree(tmp_path):
    with pytest.raises(nc.CaptureError):
        nc.target_path(tmp_path, "../elsewhere.org")
    with pytest.raises(nc.CaptureError):
        nc.target_path(tmp_path, ".git/x.org")
    assert nc.target_path(tmp_path, "roleplay/inbox.org") == (tmp_path / "roleplay/inbox.org").resolve()


# --- over the routes -----------------------------------------------------------------

@pytest.fixture()
def org(tmp_path, monkeypatch):
    root = tmp_path / "org"
    root.mkdir()
    (root / "inbox.org").write_text("#+title: Inbox\n")
    (root / "next-actions.org").write_text("#+title: Next actions\n\n* Inbox\n")
    monkeypatch.setenv("MEDIA_ORG_DIR", str(root))
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "paragtd")
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}))
    monkeypatch.setattr(org_mod, "_memory_call", lambda *a, **k: None)
    org_profile._reset_for_tests()
    return root


def test_paragtd_offers_its_templates(org):
    kinds = {k["name"]: k for k in org_mod.views("good")[1]["capture_kinds"]}
    assert list(kinds) == ["t", "n", "w", "k", "p", "j"]
    assert kinds["k"]["fields"] == [{"id": "f0", "label": "Date and time", "type": "datetime", "active": True}]
    ok, got = org_mod.capture("Water the fern", "n", "good")
    assert ok and got["path"] == "next-actions.org" and got["at"] == 4
    assert "* Inbox\n** NEXT Water the fern\n:PROPERTIES:\n:CREATED: [" in (org / "next-actions.org").read_text()
    ok, got = org_mod.capture("Renew passport", "p", "good")
    text = (org / "projects.org").read_text()
    assert ok and "* TODO Renew passport\n" in text and '** NEXT First step\n:PROPERTIES:\n:TRIGGER: next-sibling' in text
    ok, got = org_mod.capture("Library book", "k", "good")
    assert not ok and got["status"] == 400 and "Date and time" in got["error"]
    ok, got = org_mod.capture("Library book", "k", "good", fields={"f0": "2026-10-01T10:00"})
    assert ok and "SCHEDULED: <2026-10-01 Thu 10:00>" in (org / "tickler.org").read_text()
    # The plain to-do is still there, and an unknown key is refused.
    assert org_mod.capture("plain", "todo", "good")[0]
    assert org_mod.capture("x", "zz", "good")[1]["status"] == 400


def test_the_manifest_brings_site_templates(org):
    (org / ".paragtd.json").write_text(json.dumps({"version": 1, "capture": [
        {"key": "rp", "label": "New Partner", "type": "entry", "target": "file+headline",
         "file": "roleplay/inbox.org", "headline": "Partners", "template": "* %^{Name}\n\n** Basics\n"},
        {"key": "jm", "label": "Morning", "type": "entry", "target": "file+olp+datetree",
         "file": "journal.org", "template": "* %(ryer/prompt-topics)\n"}]}))
    kinds = org_mod.views("good")[1]["capture_kinds"]
    assert [k["name"] for k in kinds] == ["rp"] and kinds[0]["needs_text"] is False
    ok, got = org_mod.capture("", "rp", "good", fields={"f0": "Sam"})
    assert ok and got == {"path": "roleplay/inbox.org", "at": 2, "kind": "rp", "remembered": False}
    assert (org / "roleplay" / "inbox.org").read_text() == "* Partners\n** Sam\n\n*** Basics\n"
    assert org_mod.capture("", "jm", "good")[1]["status"] == 400


def test_plain_org_has_none(org, monkeypatch):
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "none")
    assert org_mod.views("good")[1]["capture_kinds"] == []
