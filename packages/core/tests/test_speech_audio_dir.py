"""Rendered speech lives in the state dir, and old cache clips move there.

18 Oct 2026: the clips were kept only under ~/.cache/agent-media/audio, so
clearing the cache lost replay of every past reply. They are state now
(`_paths.speech_audio_dir`); the first use moves what the cache still holds,
rewrites the history rows that name those files, and leaves a link behind.
Every path here is under tmp_path: no real HOME, cache, state or device.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from agent_media_core import _paths
from agent_media_core.state.store import StateStore


@pytest.fixture
def xdg(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(_paths, "_moved_once", set())
    return tmp_path


def _old(xdg):
    return xdg / "cache" / "agent-media" / "audio"


def _new(xdg):
    return xdg / "state" / "agent-media" / "audio"


def test_clips_land_in_the_state_dir(xdg):
    from agent_media_core.intake.submit import _audio_dir

    d = _audio_dir()
    assert d == _new(xdg) and d.is_dir()
    # Nothing to move, but the cache path is a link to it (the clip server
    # serves the phone from the cache folder's /audio/).
    assert _old(xdg).is_symlink() and _old(xdg).resolve() == d.resolve()


def test_media_audio_dir_still_wins(xdg, monkeypatch):
    monkeypatch.setenv("MEDIA_AUDIO_DIR", str(xdg / "big-disk"))
    old = _old(xdg)
    old.mkdir(parents=True)
    (old / "a.mp3").write_bytes(b"x")
    assert _paths.speech_audio_dir() == xdg / "big-disk"
    assert (old / "a.mp3").is_file()          # not touched


def test_old_cache_clips_move_and_rows_follow(xdg):
    old, new = _old(xdg), _new(xdg)
    old.mkdir(parents=True)
    clips = [old / f"20261018T1200-{i}--claude-code--000.mp3" for i in range(3)]
    for c in clips:
        c.write_bytes(b"ID3 clip")
    st = StateStore()
    row = st.add_history(
        sink="speech", uri=str(clips[0]), started_at=1.0, source="claude-code",
        text="hello", extras={"clip_uris": [str(c) for c in clips]})
    st.close()

    d = _paths.speech_audio_dir()

    assert d == new
    assert sorted(p.name for p in new.iterdir()) == sorted(c.name for c in clips)
    assert old.is_symlink()
    got = StateStore().history_row(row)
    assert got["uri"] == str(new / clips[0].name)
    assert got["extras"]["clip_uris"] == [str(new / c.name) for c in clips]
    # The old paths still resolve, through the link.
    assert all(c.is_file() for c in clips)


def test_migration_is_idempotent_and_never_overwrites(xdg):
    old, new = _old(xdg), _new(xdg)
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "same.mp3").write_bytes(b"old copy")
    (new / "same.mp3").write_bytes(b"new copy")
    (old / "only-old.mp3").write_bytes(b"o")

    first = _paths.migrate_speech_audio(old, new)
    assert first["moved"] == 1 and first["kept"] == 1
    # The clash stays where it was, so the folder is kept rather than linked.
    assert (new / "same.mp3").read_bytes() == b"new copy"
    assert (old / "same.mp3").read_bytes() == b"old copy"
    assert not old.is_symlink()
    assert (new / "only-old.mp3").is_file()

    again = _paths.migrate_speech_audio(old, new)
    assert again["moved"] == 0 and again["kept"] == 1


def test_a_cleared_cache_gets_its_link_back(xdg):
    _paths.speech_audio_dir()
    (_new(xdg) / "kept.mp3").write_bytes(b"x")
    _old(xdg).unlink()                         # "rm -rf ~/.cache/agent-media"
    _paths._moved_once.clear()                 # a new process
    _paths.speech_audio_dir()
    assert (_old(xdg) / "kept.mp3").is_file()


def test_two_processes_racing_move_each_clip_once(xdg):
    old, new = _old(xdg), _new(xdg)
    old.mkdir(parents=True)
    names = [f"c{i:03d}.mp3" for i in range(60)]
    for n in names:
        (old / n).write_bytes(n.encode())
    errors = []

    def run():
        try:
            _paths.migrate_speech_audio(old, new)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=run) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert sorted(p.name for p in new.iterdir()) == names
    assert all((new / n).read_bytes() == n.encode() for n in names)
    assert old.is_symlink()


def test_a_private_state_dir_leaves_the_real_cache_alone(monkeypatch, tmp_path):
    """XDG_STATE_HOME pointed elsewhere with the cache left default (a test, a
    sandbox) must not carry the default cache's clips off with it."""
    home = tmp_path / "home"
    real_old = home / ".cache" / "agent-media" / "audio"
    real_old.mkdir(parents=True)
    (real_old / "real.mp3").write_bytes(b"x")
    monkeypatch.setattr(_paths, "canonical_home", lambda: home)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("MEDIA_AUDIO_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(_paths, "_moved_once", set())

    d = _paths.speech_audio_dir()
    assert d == tmp_path / "elsewhere" / "agent-media" / "audio"
    assert (real_old / "real.mp3").is_file() and not real_old.is_symlink()


def test_relocate_prefix_matches_whole_folder_names(xdg):
    st = StateStore()
    a = st.add_history(sink="speech", uri="/x/audio/a.mp3", started_at=1.0)
    b = st.add_history(sink="speech", uri="/x/audio-old/b.mp3", started_at=2.0)
    assert st.relocate_prefix("/x/audio", "/y/audio") == 1
    assert st.history_row(a)["uri"] == "/y/audio/a.mp3"
    assert st.history_row(b)["uri"] == "/x/audio-old/b.mp3"
