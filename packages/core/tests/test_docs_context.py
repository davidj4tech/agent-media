"""Context promotes; it never filters.

Two signals say what "relevant now" means: the tree you are standing in, and
the tmux session you are in. They work differently — the project tree *adds*
files that live under no configured root, while the session only *reorders*
files already listed — and neither may remove anything. A list that quietly
shrinks because of where you stand is indistinguishable from one that quietly
truncates: you cannot tell "not relevant" from "not there".
"""

from pathlib import Path

import pytest

from agent_media_core import docs
from agent_media_core.docs import Doc, list_docs, project_root


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "global.org").write_text("#+title: Global Note\n#+filetags: :ref:\n")
    (root / "sess.org").write_text("#+title: Session Note\n#+filetags: :myproj:\n")
    monkeypatch.setenv("MEDIA_DOC_ROOTS", str(root))

    proj = tmp_path / "myproj"
    (proj / ".git").mkdir(parents=True)
    (proj / "README.md").write_text("# Project Readme\n")
    (proj / "design.md").write_text("# Design\n")
    return root, proj


def test_project_tree_adds_files_no_root_covers(tree):
    _root, proj = tree
    titles = {d.title for d in list_docs()}
    assert "Design" not in titles                 # not under any root
    with_ctx = {d.title for d in list_docs(cwd=proj)}
    assert "Design" in with_ctx
    assert "Global Note" in with_ctx, "context must not drop the global list"


def test_project_readmes_are_kept_but_root_indexes_are_not(tree):
    root, proj = tree
    (root / "README.md").write_text("# Root Index\n")
    titles = {d.title for d in list_docs(cwd=proj)}
    assert "Project Readme" in titles   # a project's README is the document
    assert "Root Index" not in titles   # a root's README is its index


def test_project_rows_are_marked_and_sorted_first(tree):
    _root, proj = tree
    rows = list_docs(cwd=proj)
    assert rows[0].origin == "proj"
    assert rows[0].as_row().startswith("▸proj ")


def test_session_reorders_but_adds_nothing(tree):
    plain = {d.title for d in list_docs()}
    ranked = list_docs(session="myproj")
    assert {d.title for d in ranked} == plain, "session must not add or remove"
    assert ranked[0].title == "Session Note"
    assert ranked[0].origin == "sess"


def test_session_matches_a_tag_or_a_path_segment(tmp_path, monkeypatch):
    """Both conventions are live in the same tree: `scratch` is a filetag,
    while the notes for `agent-media` are a directory and no tag at all."""
    root = tmp_path / "n"
    (root / "sessions" / "agent-media").mkdir(parents=True)
    (root / "sessions" / "agent-media" / "a.org").write_text("#+title: ByPath\n")
    (root / "b.org").write_text("#+title: ByTag\n#+filetags: :agent-media:\n")
    (root / "c.org").write_text("#+title: Neither\n")
    monkeypatch.setenv("MEDIA_DOC_ROOTS", str(root))
    got = {d.title: d.origin for d in list_docs(session="p-agent-media")}
    assert got["ByPath"] == "sess"
    assert got["ByTag"] == "sess"
    assert got["Neither"] == ""


@pytest.mark.parametrize("name,expected", [
    ("p-agent-media", ["agent-media", "p-agent-media"]),
    ("scratch", ["scratch"]),
    ("1", []),            # numeric: matches nothing useful
    ("ab", []),           # too short to be a signal
    ("", []),
])
def test_session_name_normalisation(name, expected):
    assert sorted(docs._session_terms(name)) == sorted(expected)


def test_no_repo_means_no_project_scope(tmp_path):
    """Falling back to the bare directory offered a picker full of pytest
    fixtures when run from /tmp."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "stray.md").write_text("# Stray\n")
    assert project_root(plain) is None
    assert "Stray" not in {d.title for d in list_docs(cwd=plain)}


def test_project_root_is_the_git_root_not_the_cwd(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    assert project_root(deep) == repo.resolve()


def test_project_scan_is_capped(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    for i in range(30):
        (repo / f"d{i:02}.md").write_text(f"# Doc {i}\n")
    monkeypatch.setenv("MEDIA_DOC_ROOTS", str(tmp_path / "empty"))
    monkeypatch.setenv("MEDIA_DOC_PROJECT_MAX", "5")
    assert len(list_docs(cwd=repo)) == 5


def test_context_off_is_the_plain_global_list(tree):
    _root, proj = tree
    assert {d.title for d in list_docs(cwd=None, session="")} == \
        {d.title for d in list_docs()}
