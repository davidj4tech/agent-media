"""Editing a document from the reader (doc_chat.py): the first message names
the document, the heading and the paragraph quoted whole, then the action;
the session opens in the document's project, and the document keeps its
chats. send.ask is replaced — nothing opens a pane."""

from __future__ import annotations

import subprocess

import pytest

from agent_media_server import auth, doc_chat, doc_read, send

DOC = """\
# The plan

Intro paragraph
over two lines.

## Steps

- first item
  continued
- second item

```
code here

more code
```
"""


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    repo = root / "thing"
    (repo / "docs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    doc = repo / "docs" / "plan.md"
    doc.write_text(DOC)
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(root))
    monkeypatch.setenv("MEDIA_DOC_ROOTS", "")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}) if b == "good"
                        else (None, {"error": "no", "status": 401}))
    asked = []

    def fake_ask(text, bearer, **kw):
        asked.append((text, kw))
        return True, {"session": f"s{len(asked)}", "pane": "%1", "opened": True}

    monkeypatch.setattr(send, "ask", fake_ask)
    return repo, doc, asked


def test_blocks_are_cut_as_the_reader_draws_them():
    assert doc_chat.block_at(DOC, 2) == ("Intro paragraph\nover two lines.", "The plan")
    assert doc_chat.block_at(DOC, 5) == ("## Steps", "The plan")
    assert doc_chat.block_at(DOC, 7) == ("- first item\n  continued", "Steps")
    assert doc_chat.block_at(DOC, 9) == ("- second item", "Steps")
    assert doc_chat.block_at(DOC, 11)[0] == "```\ncode here\n\nmore code\n```"
    assert doc_chat.block_at(DOC, 99) == ("", "")
    assert doc_chat.block_at("* Top\nbody\n", 1, org=True) == ("body", "Top")


def test_an_action_quotes_the_paragraph_and_opens_in_the_project(proj):
    repo, doc, asked = proj
    ok, got = doc_chat.ask(str(doc), 7, "shorten", "", "good")
    assert ok and got["session"] == "s1" and got["line"] == 7
    text, kw = asked[0]
    assert text.startswith(f'About my document "The plan" ({doc}), under "Steps", '
                           'the paragraph at line 8: "- first item continued": '
                           "Shorten this paragraph, keeping what it says. ")
    assert "git commit -- plan.md" in text
    assert kw["cwd"] == str(repo.resolve()) and kw["cwd_trusted"] is True


def test_rewrite_and_ask_take_words_the_others_do_not(proj):
    _, doc, asked = proj
    assert doc_chat.ask(str(doc), 2, "rewrite", " ", "good")[1]["status"] == 400
    assert doc_chat.ask(str(doc), 2, "", "", "good")[1]["status"] == 400
    doc_chat.ask(str(doc), 2, "rewrite", "as a question", "good")
    assert ': Rewrite this paragraph: as a question If this' in asked[0][0]
    doc_chat.ask(str(doc), None, "", "what's missing?", "good")
    assert asked[1][0].startswith(f'About my document "The plan" ({doc}): what\'s missing? ')
    doc_chat.ask(str(doc), None, "plainer", "", "good")
    assert "Say this document more plainly." in asked[2][0]


def test_the_document_keeps_its_chats_newest_first(proj):
    _, doc, _ = proj
    doc_chat.ask(str(doc), 2, "", "first", "good")
    doc_chat.ask(str(doc), 7, "delete", "", "good")
    rows = doc_chat.chats(str(doc.resolve()))
    assert [r["session"] for r in rows] == ["s2", "s1"]
    assert [r["title"] for r in rows] == ["Delete", "first"]


def test_refusals_start_nothing(proj):
    repo, doc, asked = proj
    assert doc_chat.ask(str(doc), 2, "shout", "", "good")[1]["status"] == 400
    assert doc_chat.ask(str(doc), 2, "shorten", "", "bad")[1]["status"] == 401
    assert doc_chat.ask("/etc/passwd", 0, "shorten", "", "good")[1]["status"] == 404
    git_doc = repo / ".git" / "notes.md"
    git_doc.write_text("x")
    assert doc_chat.ask(str(git_doc), 0, "shorten", "", "good")[1]["status"] == 404
    assert asked == []


def test_read_since_says_only_whether_it_changed(proj):
    _, doc, _ = proj
    ok, got = doc_read.read(str(doc), "good")
    assert ok and got["mtime"] == doc.stat().st_mtime
    ok, again = doc_read.read(str(doc), "good", got["mtime"])
    assert ok and again == {"path": got["path"], "mtime": got["mtime"], "unchanged": True}
    ok, older = doc_read.read(str(doc), "good", got["mtime"] - 5)
    assert ok and older["text"] == DOC


def test_notes_quote_each_paragraph_with_its_note(proj):
    _, doc, asked = proj
    ok, got = doc_chat.ask(str(doc), None, "notes", "", "good",
                           notes=[{"line": 9, "text": "drop this"}, {"line": 2, "text": "too vague"}])
    assert ok
    text = asked[0][0]
    assert text.startswith(f'My notes on my document "The plan" ({doc}), made while reading it; act on each one: '
                           '(1) under "The plan", line 3: "Intro paragraph over two lines." — too vague '
                           '(2) under "Steps", line 10: "- second item" — drop this ')
    assert "git commit -- plan.md" in text
    assert doc_chat.chats(str(doc.resolve()))[0]["title"] == "2 notes"
    assert doc_chat.ask(str(doc), None, "notes", "", "good", notes=[])[1]["status"] == 400
    assert doc_chat.ask(str(doc), None, "notes", "", "good", notes=[{"line": 2, "text": " "}])[1]["status"] == 400


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                   capture_output=True)


def test_undo_takes_back_an_edit_committed_on_its_own(proj):
    repo, doc, _ = proj
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "start")
    assert doc_chat.last_edit(str(doc)) is None          # the first commit: nothing before it
    doc.write_text(DOC.replace("Intro paragraph", "Short intro"))
    assert doc_chat.last_edit(str(doc)) is None          # written, not yet committed
    _git(repo, "commit", "-qm", "Shorten the intro", "--", "docs/plan.md")
    got = doc_chat.last_edit(str(doc))
    assert got["subject"] == "Shorten the intro"
    assert doc_chat.undo(str(doc), "abc", "good")[1]["status"] == 409
    ok, res = doc_chat.undo(str(doc), got["sha"], "good")
    assert ok, res
    assert doc.read_text() == DOC and res["undone"] == "Shorten the intro"
    # The undo is an edit of its own: Undo again puts the edit back.
    assert doc_chat.last_edit(str(doc))["subject"] == 'Undo "Shorten the intro"'


def test_no_undo_for_a_commit_that_touched_more_or_a_file_changed_since(proj):
    repo, doc, _ = proj
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "start")
    doc.write_text(DOC + "more\n")
    (repo / "other.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "both")
    assert doc_chat.last_edit(str(doc)) is None
    doc.write_text(DOC)
    _git(repo, "commit", "-qm", "alone", "--", "docs/plan.md")
    assert doc_chat.last_edit(str(doc))["subject"] == "alone"
    doc.write_text(DOC + "dirty\n")
    assert doc_chat.last_edit(str(doc)) is None
