"""Harness profiles: a second login is the same agent with its directory moved."""

import os
import sqlite3

import pytest

from agent_media_core import harness_profiles as hp
from agent_media_core import harnesses

UUID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
UUID2 = "1f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    for v in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(v, raising=False)
    hp._CACHE = (-1.0, [])
    return tmp_path


def test_no_profiles_is_just_the_default(tmp_path):
    assert hp.load() == []
    assert hp.env("codex", "") == {}
    assert hp.dirs("codex", tmp_path / ".codex") == [("", tmp_path / ".codex")]


def test_create_makes_a_private_dir_and_env_points_at_it(tmp_path):
    p = hp.create("codex", "work")
    d = tmp_path / "state" / "agent-media" / "harness-profiles" / "codex-work"
    assert p.dir == str(d) and d.is_dir()
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert hp.env("codex", "work") == {"CODEX_HOME": str(d)}
    assert hp.env("codex", "gone") == {}, "a profile that is not there is the default"
    assert [n for n, _ in hp.dirs("codex", tmp_path / ".codex")] == ["", "work"]


def test_refusals(tmp_path):
    with pytest.raises(hp.ProfileError):
        hp.create("hermes", "x")                 # Hermes keeps its own
    with pytest.raises(hp.ProfileError):
        hp.create("codex", "default")
    with pytest.raises(hp.ProfileError):
        hp.create("codex", "Bad Name")
    hp.create("codex", "work")
    with pytest.raises(hp.ProfileError):
        hp.create("codex", "work")
    with pytest.raises(hp.ProfileError):
        hp.create("pi", "x", adopt=str(tmp_path / "nowhere"))


def test_adopted_dirs_are_never_deleted(tmp_path):
    mine = tmp_path / ".codex-work"
    mine.mkdir()
    hp.create("codex", "work", adopt=str(mine))
    assert hp.remove("codex", "work", delete=True)
    assert mine.is_dir()
    made = hp.create("pi", "home")
    assert hp.remove("pi", "home", delete=True)
    assert not os.path.exists(made.dir)
    assert not hp.remove("pi", "home")


def test_sessions_are_found_in_every_profile(tmp_path):
    (tmp_path / ".codex" / "sessions" / "2026" / "09" / "27").mkdir(parents=True)
    (tmp_path / ".codex" / "sessions" / "2026" / "09" / "27" / f"rollout-x-{UUID}.jsonl").write_text("{}\n")
    work = hp.create("codex", "work")
    day = os.path.join(work.dir, "sessions", "2026", "09", "27")
    os.makedirs(day)
    with open(os.path.join(day, f"rollout-x-{UUID2}.jsonl"), "w") as fh:
        fh.write("{}\n")
    rows = {r.session: r.profile for r in harnesses.stored() if r.harness == "codex"}
    assert rows == {UUID: "", UUID2: "work"}
    assert harnesses.transcript(UUID2)[0] == "codex"
    assert harnesses.profile_of(UUID2) == "work"
    assert harnesses.profile_of(UUID) == ""


def _opencode_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute("create table session (id text, directory text, title text, parent_id text, "
                  "time_created int, time_updated int, time_archived int)")
    return path


def test_opencode_reads_each_profiles_database(tmp_path):
    sid = "ses_" + "a" * 26
    _opencode_db(str(tmp_path / "share" / "opencode" / "opencode.db"))
    work = hp.create("opencode", "work")
    db = _opencode_db(os.path.join(work.dir, "opencode", "opencode.db"))
    with sqlite3.connect(db) as c:
        c.execute("insert into session values (?, '/w', 'Work chat', null, 1, 2000, null)", (sid,))
    assert [(r.session, r.profile) for r in harnesses.stored() if r.harness == "opencode"] == [(sid, "work")]
    assert harnesses.profile_of(sid) == "work"
    assert harnesses.opencode_rows("select title from session where id = ?", (sid,)) == [("Work chat",)]
