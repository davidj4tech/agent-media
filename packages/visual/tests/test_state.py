"""Scene-continuity memory + spool GC (state.py)."""

import json
import os
import time

from agent_media_visual import state


def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def test_scene_roundtrip(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    state.save_scene("sess-1", "a lighthouse at dusk")
    assert state.load_scene("sess-1") == "a lighthouse at dusk"
    assert state.load_scene("other") == ""


def test_scene_empty_session_uses_default_key(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    state.save_scene("", "shared scene")
    assert state.load_scene("") == "shared scene"


def test_scene_expires_after_ttl(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    state.save_scene("sess-1", "old scene")
    # Age the record past the TTL by rewriting its timestamp.
    p = state._scenes_path()
    d = json.loads(p.read_text())
    d["sess-1"]["t"] = time.time() - state.continuity_ttl() - 5
    p.write_text(json.dumps(d))
    assert state.load_scene("sess-1") == ""


def test_save_prunes_expired_entries(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    state.save_scene("stale", "old")
    p = state._scenes_path()
    d = json.loads(p.read_text())
    d["stale"]["t"] = time.time() - state.continuity_ttl() - 5
    p.write_text(json.dumps(d))
    state.save_scene("fresh", "new")
    d = json.loads(p.read_text())
    assert "stale" not in d and "fresh" in d


def test_continuity_can_be_disabled(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    state.save_scene("sess-1", "a scene")
    monkeypatch.setenv("MEDIA_VISUAL_CONTINUITY", "0")
    assert state.load_scene("sess-1") == ""


def test_empty_scene_is_not_saved(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    state.save_scene("sess-1", "")
    assert not state._scenes_path().exists()


def test_gc_keeps_newest(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    spool = state.spool_dir()
    now = time.time()
    for i in range(6):
        f = spool / f"img-{i}.webp"
        f.write_bytes(b"x")
        os.utime(f, (now - 100 + i, now - 100 + i))  # i=5 is newest
    removed = state.gc_spool(keep=2)
    assert removed == 4
    left = sorted(f.name for f in spool.glob("img-*"))
    assert left == ["img-4.webp", "img-5.webp"]


def test_gc_ignores_non_image_files(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    state.save_scene("s", "scene")            # scenes.json must survive
    (state.spool_dir() / "img-a.webp").write_bytes(b"x")
    state.gc_spool(keep=1)
    assert state._scenes_path().exists()


def test_gc_keep_env_override(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    monkeypatch.setenv("MEDIA_VISUAL_SPOOL_KEEP", "1")
    spool = state.spool_dir()
    now = time.time()
    for i in range(3):
        f = spool / f"img-{i}.webp"
        f.write_bytes(b"x")
        os.utime(f, (now - 10 + i, now - 10 + i))
    assert state.gc_spool() == 2
