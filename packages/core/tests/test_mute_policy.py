"""Durable per-pane / per-session speech-mute policy in StateStore."""

import tempfile
from pathlib import Path

import pytest

from agent_media_core.state.store import StateStore


@pytest.fixture
def store():
    return StateStore(Path(tempfile.mkdtemp()) / "state.db")


def test_unset_resolves_unmuted(store):
    assert store.get_mute("pane", "%1") is None
    assert store.resolve_mute("%1", "sess:") is False


def test_set_get_clear_roundtrip(store):
    store.set_mute("pane", "%1", True)
    assert store.get_mute("pane", "%1") is True
    store.set_mute("pane", "%1", False)
    assert store.get_mute("pane", "%1") is False
    store.set_mute("pane", "%1", None)   # delete → back to unset
    assert store.get_mute("pane", "%1") is None


def test_empty_key_is_ignored(store):
    store.set_mute("pane", "", True)
    assert store.list_mutes() == {"panes": {}, "sessions": {}}
    assert store.get_mute("pane", "") is None


def test_pane_overrides_session(store):
    store.set_mute("session", "sess:", True)
    # No pane row → session mute applies to any pane in it.
    assert store.resolve_mute("%9", "sess:") is True
    # Explicit pane unmute beats the session mute.
    store.set_mute("pane", "%9", False)
    assert store.resolve_mute("%9", "sess:") is False
    # A different pane in the same session is still muted.
    assert store.resolve_mute("%10", "sess:") is True


def test_pane_mute_without_session(store):
    store.set_mute("pane", "%3", True)
    assert store.resolve_mute("%3", "") is True
    assert store.resolve_mute("%3", "sess:") is True


def test_list_mutes_buckets(store):
    store.set_mute("pane", "%1", True)
    store.set_mute("pane", "%2", False)
    store.set_mute("session", "work:", True)
    assert store.list_mutes() == {
        "panes": {"%1": True, "%2": False},
        "sessions": {"work:": True},
    }


def test_prune_removes_dead_panes_keeps_sessions(store):
    store.set_mute("pane", "%1", True)
    store.set_mute("pane", "%99", True)   # since-closed pane
    store.set_mute("session", "work:", True)
    removed = store.prune_panes(["%1"])    # only %1 is live
    assert removed == 1
    assert store.list_mutes() == {
        "panes": {"%1": True},
        "sessions": {"work:": True},       # sessions never pruned
    }


def test_prune_empty_is_noop_not_masswipe(store):
    """A failed/empty tmux query must not wipe the policy."""
    store.set_mute("pane", "%1", True)
    assert store.prune_panes([]) == 0
    assert store.get_mute("pane", "%1") is True
