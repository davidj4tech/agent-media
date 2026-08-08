"""Reading the error table.

Eleven call sites wrote here and nothing read it, so failures components
recovered from — a render falling back to another engine, a transcript that
couldn't be injected — were invisible unless you happened to be tailing the
journal. `media errors` and the MCP `errors` tool read this.
"""

import time

import pytest

from agent_media_core.state import StateStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return StateStore()


def test_empty_by_default(store):
    assert store.recent_errors() == []


def test_most_recent_first(store):
    now = time.time()
    store.log_error("intake", "oldest", at=now - 300)
    store.log_error("intake", "newest", at=now)
    store.log_error("intake", "middle", at=now - 100)
    assert [r["message"] for r in store.recent_errors()] == [
        "newest", "middle", "oldest"]


def test_limit_applies_after_ordering(store):
    now = time.time()
    for i in range(5):
        store.log_error("intake", f"e{i}", at=now - i)
    assert [r["message"] for r in store.recent_errors(limit=2)] == ["e0", "e1"]


def test_filter_by_component(store):
    store.log_error("intake", "render failed")
    store.log_error("voice-bridge", "injection failed")
    rows = store.recent_errors(component="voice-bridge")
    assert [r["message"] for r in rows] == ["injection failed"]


def test_since_excludes_older(store):
    now = time.time()
    store.log_error("intake", "ancient", at=now - 3600)
    store.log_error("intake", "recent", at=now - 10)
    rows = store.recent_errors(since=now - 60)
    assert [r["message"] for r in rows] == ["recent"]


def test_extras_come_back_as_a_dict(store):
    store.log_error("voice-bridge", "injection failed",
                    extras={"target": "local session ghost", "chars": 23})
    row = store.recent_errors()[0]
    assert row["extras"]["target"] == "local session ghost"
    assert row["extras"]["chars"] == 23


def test_missing_extras_stay_none(store):
    store.log_error("intake", "no extras")
    assert store.recent_errors()[0]["extras"] is None
