"""music_fetch: rooms-side YouTube acquisition (phone fetch → rooms cache)."""

import subprocess

from agent_media_core.sinks import music_fetch as mf
from agent_media_core.sinks import music as m


# ---- watch_id --------------------------------------------------------------

def test_watch_id_forms():
    assert mf.watch_id("https://www.youtube.com/watch?v=a82hE1aupo8") == "a82hE1aupo8"
    assert mf.watch_id("https://youtu.be/a82hE1aupo8?t=10") == "a82hE1aupo8"
    assert mf.watch_id("https://www.youtube.com/shorts/a82hE1aupo8") == "a82hE1aupo8"
    assert mf.watch_id("a82hE1aupo8") == "a82hE1aupo8"
    assert mf.watch_id("https://example.com/stream.mp3") is None
    assert mf.watch_id("not-an-id") is None


# ---- rooms host derivation --------------------------------------------------

def test_rooms_ssh_host_explicit_override(monkeypatch):
    monkeypatch.setenv("MEDIA_MUSIC_ROOMS_SSH", "hub")
    assert mf.rooms_ssh_host() == "hub"
    monkeypatch.setenv("MEDIA_MUSIC_ROOMS_SSH", "")   # empty = force local
    assert mf.rooms_ssh_host() is None


def test_rooms_ssh_host_from_mpd_env(monkeypatch):
    monkeypatch.delenv("MEDIA_MUSIC_ROOMS_SSH", raising=False)
    monkeypatch.setenv("MEDIA_MPD_HOST", "127.0.0.1")
    assert mf.rooms_ssh_host() is None                # loopback → local
    monkeypatch.setenv("MEDIA_MPD_HOST", "otherhost")
    assert mf.rooms_ssh_host() == "otherhost"         # remote hub → ssh


# ---- ensure_local ------------------------------------------------------------

def test_ensure_local_disabled(monkeypatch):
    monkeypatch.setenv("MEDIA_MUSIC_ROOMS_FETCH", "0")
    assert mf.ensure_local("https://youtu.be/a82hE1aupo8") is None


def test_ensure_local_cache_hit_skips_phone(monkeypatch):
    monkeypatch.delenv("MEDIA_MUSIC_ROOMS_FETCH", raising=False)
    monkeypatch.setattr(mf, "_cached_path",
                        lambda vid: f"/home/u/.cache/music-offline/{vid}.mka")
    def no_phone(*a, **k):
        raise AssertionError("phone fetch must not run on a cache hit")
    monkeypatch.setattr(mf, "_phone_fetch", no_phone)
    got = mf.ensure_local("https://www.youtube.com/watch?v=a82hE1aupo8")
    assert got == "/home/u/.cache/music-offline/a82hE1aupo8.mka"


def test_ensure_local_fetch_failure_returns_none(monkeypatch):
    monkeypatch.delenv("MEDIA_MUSIC_ROOMS_FETCH", raising=False)
    monkeypatch.setattr(mf, "_cached_path", lambda vid: None)
    monkeypatch.setattr(mf, "_phone_fetch", lambda url: None)
    assert mf.ensure_local("https://youtu.be/a82hE1aupo8") is None


# ---- _localise_youtube wiring -------------------------------------------------

def test_localise_swaps_watch_url_for_local_file(monkeypatch):
    monkeypatch.setattr(mf, "ensure_local", lambda url: "/cache/vid.mka")
    assert m._localise_youtube("mpv:https://www.youtube.com/watch?v=x") == \
        "mpv:/cache/vid.mka"


def test_localise_falls_back_to_stream_on_failure(monkeypatch):
    monkeypatch.setattr(mf, "ensure_local", lambda url: None)
    u = "mpv:https://www.youtube.com/watch?v=x"
    assert m._localise_youtube(u) == u


def test_localise_leaves_local_and_foreign_uris(monkeypatch):
    def boom(url):
        raise AssertionError("must not fetch for non-YouTube-stream URIs")
    monkeypatch.setattr(mf, "ensure_local", boom)
    assert m._localise_youtube("mpv:/home/u/file.mka") == "mpv:/home/u/file.mka"
    assert m._localise_youtube("local:track:x") == "local:track:x"
    assert m._localise_youtube("https://stream.example/radio") == \
        "https://stream.example/radio"
