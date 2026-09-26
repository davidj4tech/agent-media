"""A chat about a note (org_chat.py): the first message names the item,
the session opens in the notes tree, and the item remembers its chats.
send.ask is replaced — nothing opens a pane."""

from __future__ import annotations

import pytest

from agent_media_server import auth, org_chat, send

INBOX = """\
#+title: Inbox

* THIS WEEK
** TODO Fix the TV ssh
   Body about the telly.
** Plain heading
"""


@pytest.fixture()
def org(tmp_path, monkeypatch):
    root = tmp_path / "org"
    root.mkdir()
    (root / "inbox.org").write_text(INBOX)
    monkeypatch.setenv("MEDIA_ORG_DIR", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}) if b == "good"
                        else (None, {"error": "no", "status": 401}))
    asked = []

    def fake_ask(text, bearer, **kw):
        asked.append((text, kw))
        return True, {"session": f"s{len(asked)}", "pane": "%1", "opened": True}

    monkeypatch.setattr(send, "ask", fake_ask)
    return root, asked


def test_a_heading_is_named_and_the_chat_opens_in_the_tree(org):
    root, asked = org
    ok, got = org_chat.ask("inbox.org", 4, "  what's blocking this?\n", "good")
    assert ok and got["session"] == "s1" and got["path"] == "inbox.org" and got["at"] == 4
    text, kw = asked[0]
    assert text == 'About "Fix the TV ssh" in my Org notes (~/org/inbox.org, line 4, TODO): what\'s blocking this?'
    assert kw["cwd"] == str(root) and kw["cwd_trusted"] is True


def test_the_item_keeps_its_chats_newest_first(org):
    org_chat.ask("inbox.org", 4, "first", "good")
    org_chat.ask("inbox.org", 4, "second", "good")
    org_chat.ask("inbox.org", 6, "other", "good")
    rows = org_chat.chats("inbox.org", "Fix the TV ssh")
    assert [r["session"] for r in rows] == ["s2", "s1"] and rows[0]["title"] == "second"
    assert [r["session"] for r in org_chat.chats("inbox.org", "Plain heading")] == ["s3"]


def test_a_whole_note_has_no_line(org):
    _, asked = org
    org_chat.ask("inbox.org", 0, "summarise", "good")
    assert asked[0][0] == 'About "Inbox" in my Org notes (~/org/inbox.org): summarise'


def test_refusals_start_nothing(org):
    _, asked = org
    assert org_chat.ask("inbox.org", 4, "   ", "good")[0] is False
    assert org_chat.ask("inbox.org", 4, "hi", "bad")[1]["status"] == 401
    assert org_chat.ask("../etc/passwd", 0, "hi", "good")[1]["status"] == 404
    assert org_chat.ask("inbox.org", 5, "hi", "good")[1]["status"] == 409
    assert asked == []
