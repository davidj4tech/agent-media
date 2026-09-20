"""Retention for the state DB.

Nothing pruned this table for the first year of its life, so it kept every
error, every music play row and — two thirds of its bytes — the clip lists of
speech whose audio the cache swept long ago. The rules are unequal on purpose:
the spoken WORDS are a transcript the library still shows, so they stay; what
goes is what nothing can read back.
"""

import json
import sqlite3
import time

import pytest

from agent_media_core.state import StateStore


DAY = 86400.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return StateStore()


def _row(store, sink, uri, at, extras=None, text=None):
    """A history row at an exact age. The writing APIs stamp `now`, and every
    rule here is about age, so the rows are laid down directly."""
    db = sqlite3.connect(str(store.path))
    with db:
        db.execute("INSERT INTO history(sink, uri, started_at, text, extras)"
                   " VALUES (?, ?, ?, ?, ?)",
                   (sink, uri, at, text,
                    json.dumps(extras) if extras is not None else None))
    db.close()


def _speech(store, *, at, clips):
    _row(store, "speech", f"say:{at}", at,
         {"clip_uris": clips, "clip_sentences": ["a"],
          "clip_durations_s": [1.0], "source_session": "s1"},
         text="the words")


def test_old_errors_go_recent_ones_stay(store):
    now = time.time()
    store.log_error("intake", "ancient", at=now - 60 * DAY)
    store.log_error("intake", "today", at=now)
    assert store.gc()["errors"] == 1
    assert [e["message"] for e in store.recent_errors()] == ["today"]


def test_music_rows_expire_speech_never(store):
    now = time.time()
    _row(store, "music", "spotify:old", now - 120 * DAY)
    _speech(store, at=now - 120 * DAY, clips=[])
    res = store.gc()
    assert res["history"] == 1
    assert store.recent_history(sink="music", limit=10) == []
    assert len(store.recent_history(sink="speech", limit=10)) == 1


def test_clip_lists_go_when_the_audio_has_gone(store, tmp_path):
    now = time.time()
    _speech(store, at=now - 60 * DAY, clips=[str(tmp_path / "swept.mp3")])
    assert store.gc()["clips"] == 1
    row = store.recent_history(sink="speech", limit=1)[0]
    # The words survive; only the unplayable arrays leave.
    assert row["text"] == "the words"
    assert row["extras"]["source_session"] == "s1"
    assert "clip_uris" not in row["extras"]
    assert "clip_durations_s" not in row["extras"]


def test_clip_lists_stay_while_one_clip_is_on_disk(store, tmp_path):
    now = time.time()
    here = tmp_path / "kept.mp3"
    here.write_bytes(b"x")
    _speech(store, at=now - 60 * DAY, clips=[str(here), str(tmp_path / "gone.mp3")])
    assert store.gc()["clips"] == 0
    assert store.recent_history(sink="speech", limit=1)[0]["extras"]["clip_uris"]


def test_a_file_uri_is_a_path(store, tmp_path):
    """The clips are written as `file://…` by some writers and bare by others."""
    now = time.time()
    here = tmp_path / "kept.mp3"
    here.write_bytes(b"x")
    _speech(store, at=now - 60 * DAY, clips=[f"file://{here}"])
    assert store.gc()["clips"] == 0


def test_recent_speech_is_left_alone(store, tmp_path):
    _speech(store, at=time.time(), clips=[str(tmp_path / "missing.mp3")])
    assert store.gc()["clips"] == 0


def test_dry_run_counts_and_changes_nothing(store, tmp_path):
    now = time.time()
    store.log_error("intake", "ancient", at=now - 60 * DAY)
    _row(store, "music", "spotify:old", now - 120 * DAY)
    _speech(store, at=now - 60 * DAY, clips=[str(tmp_path / "swept.mp3")])
    dry = store.gc(dry_run=True)
    assert (dry["errors"], dry["history"], dry["clips"]) == (1, 1, 1)
    assert dry["bytes"] > 0
    assert len(store.recent_errors()) == 1
    assert len(store.recent_history(sink="music", limit=10)) == 1
    assert store.gc() == dry


def test_a_row_without_clips_is_not_rewritten(store):
    """An extras blob with no clip list has nothing to strip — and is not
    counted, or the daily run would report work it never did."""
    now = time.time()
    _row(store, "speech", "say:notif", now - 60 * DAY, {"kind": "notif"}, "ding")
    assert store.gc()["clips"] == 0
    assert json.loads(json.dumps(
        store.recent_history(sink="speech", limit=1)[0]["extras"])) == {"kind": "notif"}
