"""`/targets` `places[].mail`: pending agent-mail per place (mailbox.py)."""
import sqlite3
import subprocess

import pytest

from agent_media_server import mailbox


@pytest.fixture
def mail(tmp_path, monkeypatch):
    """An agent-mail database of our own; `add(recipient, state)`."""
    db = tmp_path / "mail.sqlite3"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY, recipient TEXT NOT NULL,
                   claimed_session TEXT, acknowledged_at TEXT)""")
    monkeypatch.setenv("AGENT_MAIL_DB", str(db))
    monkeypatch.setattr(mailbox, "_counts", (0.0, None))
    monkeypatch.setattr(mailbox, "_names", {})

    def add(recipient, state="pending"):
        con.execute("INSERT INTO messages (recipient, claimed_session, acknowledged_at)"
                    " VALUES (?, ?, ?)",
                    (recipient, "s" if state in ("claimed", "acked") else None,
                     "t" if state == "acked" else None))
        con.commit()
        monkeypatch.setattr(mailbox, "_counts", (0.0, None))
    return add


def _repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                    "PATH": "/usr/bin:/bin"})
    return path


def test_only_pending_mail_counts(mail, tmp_path):
    proj = tmp_path / "agent-media"
    proj.mkdir()
    for state in ("pending", "pending", "claimed", "acked"):
        mail("agent-media", state)
    mail("other")
    rows = mailbox.with_mail([{"name": "agent-media", "path": str(proj)}])
    assert rows[0]["mail"] == 2


def test_a_place_with_no_mail_says_zero(mail, tmp_path):
    (tmp_path / "quiet").mkdir()
    assert mailbox.with_mail([{"path": str(tmp_path / "quiet")}])[0]["mail"] == 0


def test_a_git_folder_reads_its_repo_s_mailbox_and_a_worktree_its_main_repo(mail, tmp_path):
    repo = _repo(tmp_path / "websites")
    sub = repo / "somahz-home"
    sub.mkdir()
    tree = tmp_path / "elsewhere"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(tree)], check=True)
    assert mailbox.mailbox_of(str(sub)) == "websites"
    assert mailbox.mailbox_of(str(tree)) == "websites"
    mail("websites")
    rows = mailbox.with_mail([{"path": str(sub)}, {"path": str(tree)}])
    assert [r["mail"] for r in rows] == [1, 1]


def test_the_shared_mailbox_is_no_place_s(mail, tmp_path):
    (tmp_path / "sam").mkdir()
    mail("sam")
    assert mailbox.with_mail([{"path": str(tmp_path / "sam")}])[0]["mail"] == 0


def test_no_mailbox_no_field(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_DB", str(tmp_path / "absent.sqlite3"))
    monkeypatch.setattr(mailbox, "_counts", (0.0, None))
    assert "mail" not in mailbox.with_mail([{"path": str(tmp_path)}])[0]


def test_a_poll_reuses_the_last_count(mail, tmp_path, monkeypatch):
    (tmp_path / "p").mkdir()
    mail("p")
    assert mailbox.with_mail([{"path": str(tmp_path / "p")}])[0]["mail"] == 1
    reads = []
    monkeypatch.setattr(mailbox, "_read", lambda: reads.append(1) or {})
    assert mailbox.with_mail([{"path": str(tmp_path / "p")}])[0]["mail"] == 1
    assert reads == []
