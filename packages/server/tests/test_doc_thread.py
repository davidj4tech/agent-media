"""A thread's own documents on the project page (doc_read.thread_docs): the
ones its transcript wrote or edited, its subagents' too, and the ones it was
started about from the reader. Reads do not count. Everything is made up in
tmp dirs: a fake transcript, a fake chat store."""

from __future__ import annotations

import json

import pytest

from agent_media_server import auth, doc_read, org_chat

SID = "11111111-2222-3333-4444-555555555555"


def _use(name: str, fp: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": name, "input": {"file_path": fp}}]}})


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    repo = root / "thing"
    (repo / "docs" / "proposals").mkdir(parents=True)
    (repo / "README.md").write_text("# Thing\n")
    for name in ("a", "b", "c", "d"):
        (repo / "docs" / "proposals" / f"{name}.md").write_text(f"# {name}\n")
    claude = tmp_path / "claude"
    tdir = claude / "projects" / "-thing"
    tdir.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(root))
    monkeypatch.setenv("MEDIA_DOC_ROOTS", "")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}))
    return repo, tdir


def test_edits_subagents_and_reader_chats_count_reads_do_not(proj):
    repo, tdir = proj
    p = repo / "docs" / "proposals"
    (tdir / f"{SID}.jsonl").write_text("\n".join([
        _use("Edit", str(p / "a.md")),
        _use("Read", str(p / "b.md")),
        _use("Write", str(p / "c.md")),
        _use("Edit", str(p / "a.md")),           # touched again: last
        _use("Edit", str(repo / "notes.py")),     # not a document
        "not json",
    ]) + "\n")
    sub = tdir / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text(_use("MultiEdit", str(p / "d.md")) + "\n")
    org_chat._remember(str(p / "b.md"), "", {"session": "other", "title": "x", "at": 1})

    ok, out = doc_read.project_docs("thing", "", "good", SID)
    assert ok
    assert out["thread"] == [str(p / "a.md"), str(p / "c.md"), str(p / "d.md")]

    org_chat._remember(str(p / "b.md"), "", {"session": SID, "title": "x", "at": 2})
    ok, out = doc_read.project_docs("thing", "", "good", SID)
    assert str(p / "b.md") in out["thread"]


def test_no_session_no_thread_and_unknown_session_is_empty(proj):
    ok, out = doc_read.project_docs("thing", "", "good")
    assert ok and "thread" not in out
    ok, out = doc_read.project_docs("thing", "", "good", SID)
    assert ok and out["thread"] == []
