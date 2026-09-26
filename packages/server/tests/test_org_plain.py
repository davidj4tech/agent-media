"""Notes on plain Org: no method's layout assumed (org_profile.py). Every
top-level file is a view, the roam shelves are the folders found, a heading
moves to the top level of another file, and a fresh tree is just an inbox.
paragtd's layout is chosen only when its package is installed and the tree
looks like it (or it is named)."""

from __future__ import annotations

import datetime as dt

import pytest

from agent_media_server import auth, org as org_mod, org_edit, org_profile, org_setup


@pytest.fixture()
def org(tmp_path, monkeypatch):
    root = tmp_path / "org"
    (root / "roam" / "daily").mkdir(parents=True)
    (root / "roam" / "daily" / "d.org").write_text("#+title: A day\n")
    (root / "inbox.org").write_text("#+title: Inbox\n\n* TODO Fix the TV\n")
    (root / "work.org").write_text("#+title: Work things\n\n* TODO Ship it\n* Reference\n")
    (root / "astro.org").write_text("* Full moon\n  SCHEDULED: <2026-09-01 Tue>\n")
    monkeypatch.setenv("MEDIA_ORG_DIR", str(root))
    # No `[org] profile` from this machine's config.toml either.
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}) if b == "good"
                        else (None, {"error": "no", "status": 401}))
    monkeypatch.setattr(auth, "may_control_speech", lambda b: (True, {}))
    # Setup's checklist asks the memory store and systemd; neither is ours here.
    monkeypatch.setattr(org_mod, "_memory_call", lambda *a, **k: None)
    monkeypatch.setattr(org_setup, "_systemctl", lambda *a: (127, "no systemctl here"))
    org_profile._reset_for_tests()
    return root


def _views(org) -> dict:
    ok, got = org_mod.views("good")
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
    items = org_mod._agenda(dt.date(2026, 9, 24))
    assert [i["title"] for i in items] == ["Full moon"] and items[0]["overdue"]


def test_the_agenda_keeps_done_ones_when_asked(org):
    (org / "inbox.org").write_text("* DONE Paid it\n  SCHEDULED: <2026-09-20 Sun>\n"
                                   "* TODO Pay it\n  SCHEDULED: <2026-09-20 Sun>\n")
    assert [i["title"] for i in org_mod._agenda(dt.date(2026, 9, 24))] == ["Full moon", "Pay it"]
    kept = {i["title"]: i for i in org_mod._agenda(dt.date(2026, 9, 24), done=True)}
    assert not kept["Paid it"]["overdue"] and kept["Pay it"]["overdue"]
    ok, got = org_mod.view("agenda", "good", done=True)
    assert ok and "Paid it" in {i["title"] for i in got["items"]}


def test_the_paragtd_layout_is_detected(org):
    (org / "next-actions.org").write_text("#+title: Next actions\n\n* Inbox\n")
    views = _views(org)
    assert "next" in views and views["next"]["label"] == "Next actions"
    assert "work" not in views


def test_none_turns_detection_off(org, monkeypatch):
    (org / "next-actions.org").write_text("#+title: Next actions\n\n* Inbox\n")
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "none")
    assert "next-actions" in _views(org) and "next" not in _views(org)


def test_an_unknown_profile_is_plain_org(org, monkeypatch):
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "no-such-method")
    assert org_mod.profile() is org_profile.PLAIN


def test_a_heading_moves_to_another_file(org):
    ok, got = org_edit.refile("inbox.org", 3, "Fix the TV", "work", "good")
    assert ok and got["to"] == "work"
    assert "* TODO Fix the TV" in (org / "work.org").read_text()
    assert "Fix the TV" not in (org / "inbox.org").read_text()
    # paragtd's targets are not offered.
    ok, got = org_edit.refile("work.org", 3, "Ship it", "tickler", "good", date="2026-10-01")
    assert not ok and got["status"] == 400


def test_roam_notes_stay_read_only(org):
    ok, got = org_edit.set_state("roam/daily/d.org", 1, "", "DONE", "good")
    assert not ok and got["status"] == 400


def test_a_fresh_tree_is_just_an_inbox(tmp_path, monkeypatch, org):
    root = tmp_path / "fresh"
    monkeypatch.setenv("MEDIA_ORG_DIR", str(root))
    monkeypatch.setattr(org_setup.shutil, "which", lambda _: None)   # no git init
    ok, got = org_setup.run("org", "create", "good")
    assert ok and got["created"] == ["inbox.org"]
    assert sorted(p.name for p in root.iterdir()) == ["inbox.org"]


# --- TODO keywords -------------------------------------------------------------------

def test_a_file_declares_its_own_keywords(org):
    (org / "work.org").write_text(
        "#+title: Work\n#+TODO: TODO HOLD(h@) | DONE KILLED\n\n* HOLD Ship it\n* NEXT Not a state\n")
    items = org_mod.view("work", "good")[1]["items"]
    assert [(h["state"], h["title"]) for h in items] == [("HOLD", "Ship it"),
                                                         ("", "NEXT Not a state")]
    ok, got = org_mod.read("work.org", 4, "good")
    assert got["state"] == "HOLD" and got["states"] == {"open": ["TODO", "HOLD"],
                                                        "done": ["DONE", "KILLED"]}
    # KILLED closes it; NEXT is not a state here.
    assert org_edit.set_state("work.org", 4, "Ship it", "KILLED", "good")[0]
    assert "* KILLED Ship it\n  CLOSED:" in (org / "work.org").read_text()
    assert org_mod.view("work", "good")[1]["items"] == [
        {**items[1], "at": 6}]
    ok, got = org_edit.set_state("work.org", 6, "NEXT Not a state", "NEXT", "good")
    assert not ok and got["status"] == 400


def test_without_a_declaration_org_has_todo_and_done(org):
    (org / "work.org").write_text("* NEXT Ship it\n* TODO Other\n")
    items = org_mod.view("work", "good")[1]["items"]
    assert [(h["state"], h["title"]) for h in items] == [("", "NEXT Ship it"), ("TODO", "Other")]


def test_config_names_the_keywords(org, tmp_path, monkeypatch):
    (tmp_path / "no-config.toml").write_text(
        '[org]\ntodo_keywords = ["TODO", "NEXT", "|", "DONE"]\n')
    (org / "work.org").write_text("* NEXT Ship it\n* DONE Old\n")
    items = org_mod.view("work", "good")[1]["items"]
    assert [(h["state"], h["title"]) for h in items] == [("NEXT", "Ship it")]
    assert org_mod.views("good")[1]["states"] == {"open": ["TODO", "NEXT"], "done": ["DONE"]}


# --- agenda files --------------------------------------------------------------------

def test_config_names_the_agenda_files(org, tmp_path, monkeypatch):
    (org / "projects").mkdir()
    (org / "projects" / "house.org").write_text(
        "#+title: House\n* TODO Paint\n  SCHEDULED: <2026-09-24 Thu>\n")
    (tmp_path / "no-config.toml").write_text(
        f'[org]\nagenda_files = ["work.org", "projects", "{tmp_path}/elsewhere.org"]\n')
    (tmp_path / "elsewhere.org").write_text("* TODO outside\n")
    views = _views(org)
    assert [v for v in views if views[v]["kind"] == "file"] == ["work", "projects-house"]
    assert views["projects-house"]["path"] == "projects/house.org"
    assert [i["title"] for i in org_mod._agenda(dt.date(2026, 9, 24))] == ["Paint"]
    # A file under a folder is editable when it is an agenda file.
    assert org_edit.set_state("projects/house.org", 2, "Paint", "DONE", "good")[0]
    # The capture file stays editable even when it is not on the list.
    assert org_edit.set_state("inbox.org", 3, "Fix the TV", "DONE", "good")[0]


def test_media_agenda_files_is_the_fallback(org, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENDA_FILES", f"{org}/work.org")
    assert [v["name"] for v in org_mod.views("good")[1]["views"] if v["kind"] == "file"] == ["work"]


def test_org_says_what_the_app_should_offer(org):
    ok, got = org_mod.views("good")
    assert got["profile"] is None and got["capture_file"] == "inbox.org"
    assert got["states"] == {"open": ["TODO"], "done": ["DONE"]}
    assert {"name": "work", "label": "Work things", "path": "work.org"} in got["refile_targets"]
    assert _views(org)["work"]["states"] == {"open": ["TODO"], "done": ["DONE"]}


def test_paragtd_says_so_too(org, monkeypatch):
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "paragtd")
    ok, got = org_mod.views("good")
    assert got["profile"] == "paragtd"
    # paragtd-todo-keywords, with no manifest to say otherwise.
    assert got["states"] == {"open": ["TODO", "NEXT", "WAITING"], "done": ["DONE", "CANCELLED"]}
    tickler = next(t for t in got["refile_targets"] if t["name"] == "tickler")
    assert tickler == {"name": "tickler", "label": "Tickler", "path": "tickler.org",
                       "needs_date": True}


# --- copying Emacs' settings ---------------------------------------------------------

def test_the_config_writer_keeps_the_rest(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('# mine\n[host]\nroles = ["render"]\n\n[org]\nprofile = "none"\n'
                 'agenda_files = ["old.org"]\n\n[peers.phone]\nhost = "p"\n')
    org_setup._set_org_config({"agenda_files": ["a.org"], "todo_keywords": ["TODO", "|", "DONE"]}, p)
    import tomllib
    got = tomllib.loads(p.read_text())
    assert got["org"] == {"profile": "none", "agenda_files": ["a.org"],
                            "todo_keywords": ["TODO", "|", "DONE"]}
    assert got["host"] == {"roles": ["render"]} and got["peers"]["phone"]["host"] == "p"
    assert p.read_text().startswith("# mine\n")
    fresh = tmp_path / "new.toml"
    org_setup._set_org_config({"agenda_files": []}, fresh)
    assert tomllib.loads(fresh.read_text()) == {"org": {"agenda_files": []}}


def test_import_copies_emacs_settings(org, tmp_path, monkeypatch):
    monkeypatch.setattr(org_setup.shutil, "which", lambda _: "/usr/bin/emacsclient")
    monkeypatch.setattr(org_setup, "_from_emacs", lambda: {
        "files": [str(org / "work.org"), "/elsewhere/x.org"],
        "keywords": [["TODO(t)", "NEXT(n)", "|", "DONE(d!)"], ["BUG", "|", "FIXED"]],
        "enforce": True})
    rows = {r["name"]: r for r in org_setup.status("good")[1]["components"]}
    assert rows["agenda"]["state"] == "off" and rows["agenda"]["actions"] == ["import"]
    ok, got = org_setup.run("agenda", "import", "good")
    assert ok and got["files"] == 1 and got["outside"] == 1
    assert got["keywords"] == ["TODO", "NEXT", "BUG", "|", "DONE", "FIXED"]
    assert got["enforce_todo_dependencies"] is True
    assert org_profile.PLAIN.enforces_dependencies()
    rows = {r["name"]: r for r in org_setup.status("good")[1]["components"]}
    assert rows["agenda"]["state"] == "ok"
    assert [v for v in _views(org) if _views(org)[v]["kind"] == "file"] == ["work"]


def test_a_profile_has_no_agenda_row(org, monkeypatch):
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "paragtd")
    names = [r["name"] for r in org_setup.status("good")[1]["components"]]
    assert "agenda" not in names


# --- paragtd's manifest ------------------------------------------------------------

def test_paragtd_reads_its_manifest(org, monkeypatch):
    import json
    (org / "next-actions.org").write_text("#+title: Next actions\n\n* Inbox\n")
    (org / "extra.org").write_text("#+title: Extra things\n* HOLD Paused\n")
    (org / ".paragtd.json").write_text(json.dumps({
        "version": 1, "files": ["inbox.org", "next-actions.org", "extra.org", "astro.org"],
        "todo_keywords": [["TODO(t)", "HOLD(h@)", "|", "DONE(d!)"], ["BUG", "|", "FIXED"]],
        "astro": {"stale_days": 30}}))
    ok, got = org_mod.views("good")
    assert got["profile"] == "paragtd"     # detected by the tree
    assert got["states"] == {"open": ["TODO", "HOLD", "BUG"], "done": ["DONE", "FIXED"]}
    files = [(v["name"], v["label"]) for v in got["views"] if v["kind"] == "file"]
    assert files == [("inbox", "Inbox"), ("next", "Next actions"), ("extra", "Extra things")]
    assert org_mod.view("extra", "good")[1]["items"][0]["state"] == "HOLD"
    # astro.org is on the agenda, and a month-old alert is still in it.
    assert [i["title"] for i in org_mod._agenda(dt.date(2026, 9, 24))] == ["Full moon"]
    (org / ".paragtd.json").write_text("{not json")
    assert org_mod.views("good")[1]["states"]["open"] == ["TODO", "NEXT", "WAITING"]
