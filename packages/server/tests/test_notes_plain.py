"""Notes on plain Org: no method's layout assumed (notes_profile.py). Every
top-level file is a view, the roam shelves are the folders found, a heading
moves to the top level of another file, and a fresh tree is just an inbox.
paragtd's layout is chosen only when its package is installed and the tree
looks like it (or it is named)."""

from __future__ import annotations

import datetime as dt

import pytest

from agent_media_server import auth, notes, notes_edit, notes_profile, notes_setup


@pytest.fixture()
def org(tmp_path, monkeypatch):
    root = tmp_path / "org"
    (root / "roam" / "daily").mkdir(parents=True)
    (root / "roam" / "daily" / "d.org").write_text("#+title: A day\n")
    (root / "inbox.org").write_text("#+title: Inbox\n\n* TODO Fix the TV\n")
    (root / "work.org").write_text("#+title: Work things\n\n* TODO Ship it\n* Reference\n")
    (root / "astro.org").write_text("* Full moon\n  SCHEDULED: <2026-09-01 Tue>\n")
    monkeypatch.setenv("MEDIA_NOTES_DIR", str(root))
    # No `[notes] profile` from this machine's config.toml either.
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}) if b == "good"
                        else (None, {"error": "no", "status": 401}))
    monkeypatch.setattr(auth, "may_control_speech", lambda b: (True, {}))
    notes_profile._reset_for_tests()
    return root


def _views(org) -> dict:
    ok, got = notes.views("good")
    assert ok
    return {v["name"]: v for v in got["views"]}


def test_every_top_level_file_is_a_view(org):
    views = _views(org)
    assert list(views) == ["agenda", "astro", "inbox", "work", "roam-daily"]
    assert views["work"]["label"] == "Work things" and views["work"]["count"] == 1
    assert views["roam-daily"] == {"name": "roam-daily", "label": "Daily",
                                   "kind": "folder", "count": 1}


def test_nothing_ages_off_the_agenda(org, monkeypatch):
    # The astro stale rule is paragtd's, not Org's.
    items = notes._agenda(dt.date(2026, 9, 24))
    assert [i["title"] for i in items] == ["Full moon"] and items[0]["overdue"]


def test_the_paragtd_layout_is_detected(org):
    (org / "next-actions.org").write_text("#+title: Next actions\n\n* Inbox\n")
    views = _views(org)
    assert "next" in views and views["next"]["label"] == "Next actions"
    assert "work" not in views


def test_none_turns_detection_off(org, monkeypatch):
    (org / "next-actions.org").write_text("#+title: Next actions\n\n* Inbox\n")
    monkeypatch.setenv("MEDIA_NOTES_PROFILE", "none")
    assert "next-actions" in _views(org) and "next" not in _views(org)


def test_an_unknown_profile_is_plain_org(org, monkeypatch):
    monkeypatch.setenv("MEDIA_NOTES_PROFILE", "no-such-method")
    assert notes.profile() is notes_profile.PLAIN


def test_a_heading_moves_to_another_file(org):
    ok, got = notes_edit.refile("inbox.org", 3, "Fix the TV", "work", "good")
    assert ok and got["to"] == "work"
    assert "* TODO Fix the TV" in (org / "work.org").read_text()
    assert "Fix the TV" not in (org / "inbox.org").read_text()
    # paragtd's targets are not offered.
    ok, got = notes_edit.refile("work.org", 3, "Ship it", "tickler", "good", date="2026-10-01")
    assert not ok and got["status"] == 400


def test_roam_notes_stay_read_only(org):
    ok, got = notes_edit.set_state("roam/daily/d.org", 1, "", "DONE", "good")
    assert not ok and got["status"] == 400


def test_a_fresh_tree_is_just_an_inbox(tmp_path, monkeypatch, org):
    root = tmp_path / "fresh"
    monkeypatch.setenv("MEDIA_NOTES_DIR", str(root))
    monkeypatch.setattr(notes_setup.shutil, "which", lambda _: None)   # no git init
    ok, got = notes_setup.run("org", "create", "good")
    assert ok and got["created"] == ["inbox.org"]
    assert sorted(p.name for p in root.iterdir()) == ["inbox.org"]
