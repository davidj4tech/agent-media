"""Keeping or discarding a draft zettel from the app (org_zettel.py). Each
test writes its own tree; nothing reaches ~/org."""

from __future__ import annotations

import pytest

from agent_media_server import auth, org, org_zettel

DISTILLED = """\
:PROPERTIES:
:ID:          zk:aaaa1111:1
:ZK_STATUS:   draft
:END:
#+title: Pauses must be undone
#+filetags: :zk:inbox:

- Source :: [[id:claude:red5:aaaa1111][a session]]

The body.
"""


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    root = tmp_path / "org"
    notes = root / "roam" / "notes"
    notes.mkdir(parents=True)
    (notes / "20260927T100000==aaaa1111z1--pauses__zk_inbox.org").write_text(DISTILLED)
    (notes / "20260928T100000--mine__inbox.org").write_text("#+title: Mine\n#+filetags: :inbox:\n\nText.\n")
    (notes / "kept__zk.org").write_text("#+title: Kept\n#+filetags: :zk:\n")
    (root / "roam" / "sessions").mkdir()
    (root / "roam" / "sessions" / "s.org").write_text("#+title: S\n#+filetags: :claude:inbox:\n")
    monkeypatch.setenv("MEDIA_ORG_DIR", str(root))
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}) if b == "good"
                        else (None, {"error": "no", "status": 401}))
    return root


D = "roam/notes/20260927T100000==aaaa1111z1--pauses__zk_inbox.org"


def test_read_and_the_folder_mark_drafts_only_in_the_notes_folder(tree):
    assert org.read(D, 0, "good")[1]["draft"] is True
    assert "draft" not in org.read("roam/notes/kept__zk.org", 0, "good")[1]
    assert "draft" not in org.read("roam/sessions/s.org", 0, "good")[1]
    rows = {r["title"]: r.get("draft") for r in org._folder_notes(tree / "roam" / "notes")}
    assert rows == {"Pauses must be undone": True, "Mine": True, "Kept": None}


def test_keep_retags_marks_permanent_and_renames(tree):
    ok, got = org_zettel.review(D, "keep", "good")
    assert ok and got == {"path": "roam/notes/20260927T100000==aaaa1111z1--pauses__zk.org",
                          "action": "keep", "title": "Pauses must be undone"}
    text = (tree / got["path"]).read_text()
    assert "#+filetags: :zk:\n" in text
    assert ":ZK_STATUS:   permanent\n" in text
    assert ":ID:          zk:aaaa1111:1" in text and "The body." in text
    assert not (tree / D).exists()
    # Twice is a 409: it is not a draft any more.
    ok, got = org_zettel.review(got["path"], "keep", "good")
    assert not ok and got["status"] == 409


def test_keep_a_hand_note_with_only_the_inbox_tag(tree):
    ok, got = org_zettel.review("roam/notes/20260928T100000--mine__inbox.org", "keep", "good")
    assert ok and got["path"] == "roam/notes/20260928T100000--mine.org"
    assert "#+filetags:\n" in (tree / got["path"]).read_text()


def test_discard_deletes(tree):
    ok, got = org_zettel.review(D, "discard", "good")
    assert ok and got["action"] == "discard"
    assert not (tree / D).exists()


def test_refusals(tree):
    assert org_zettel.review(D, "keep", "bad")[1]["status"] == 401
    assert org_zettel.review(D, "burn", "good")[1]["status"] == 400
    assert org_zettel.review("roam/sessions/s.org", "keep", "good")[1]["status"] == 400
    assert org_zettel.review("roam/notes/../../x.org", "keep", "good")[1]["status"] == 404


def test_every_org_route_is_let_through():
    """The dispatcher (`app._org`) only sees paths in ORG_PATHS: a route added
    to one and not the other answers "not found" on the live server."""
    import inspect
    import re as _re

    from agent_media_server import app

    handled = set(_re.findall(r'"(/org[a-z/]*)"', inspect.getsource(app._org)))
    assert "/org/zettel" in handled
    assert handled <= app.ORG_PATHS
