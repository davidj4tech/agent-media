"""The shared checkout guard (.githooks/pre-commit, #152).

A commit in the main checkout is refused unless HEAD is main; a linked
worktree commits on any branch.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[3] / ".githooks"

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh hook"),
]


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "core.hooksPath", str(HOOKS))
    git(r, "commit", "-q", "--allow-empty", "-m", "root")
    return r


def commit(cwd: Path) -> subprocess.CompletedProcess:
    return git(cwd, "commit", "-q", "--allow-empty", "-m", "x", check=False)


def test_main_checkout_on_main_commits(repo: Path) -> None:
    assert commit(repo).returncode == 0


def test_main_checkout_on_a_branch_is_refused(repo: Path) -> None:
    git(repo, "switch", "-q", "-c", "feature")
    r = commit(repo)
    assert r.returncode != 0
    assert "shared checkout" in r.stderr
    assert "git worktree add" in r.stderr
    assert "'feature'" in r.stderr


def test_main_checkout_detached_is_refused(repo: Path) -> None:
    git(repo, "switch", "-q", "--detach")
    assert commit(repo).returncode != 0


def test_worktree_on_a_branch_commits(repo: Path, tmp_path: Path) -> None:
    wt = tmp_path / "proj-feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    assert commit(wt).returncode == 0


def test_no_verify_bypasses(repo: Path) -> None:
    git(repo, "switch", "-q", "-c", "feature")
    r = git(repo, "commit", "-q", "--no-verify", "--allow-empty", "-m", "x", check=False)
    assert r.returncode == 0
