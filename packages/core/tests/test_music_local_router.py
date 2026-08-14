"""Stage 1: phone-local music backend + router + auto routing.

Covers the seam that made phone-local playout duckable: the router forwards
coordinator control to whichever backend is live, the tcp:// IPC endpoint
parses, and `--where auto` resolves rooms-vs-phone the way play-music did.
"""

from __future__ import annotations

import pytest

from agent_media_core.sinks import _mpv_ipc as ipc
from agent_media_core.sinks.music_local import SinkMusicLocal
from agent_media_core.sinks.music_router import SinkMusicRouter
from agent_media_core.types import Target


# ---- _mpv_ipc tcp endpoint parsing ------------------------------------------

def test_tcp_endpoint_bad_form_raises():
    with pytest.raises(ipc.MpvIpcError):
        ipc._open("tcp://nohostport", timeout=0.1)


def test_tcp_endpoint_uses_inet(monkeypatch):
    seen = {}

    class FakeSock:
        def settimeout(self, t): pass
        def connect(self, addr): seen["addr"] = addr

    import socket as _socket

    def fake_socket(fam, typ):
        seen["fam"] = fam
        return FakeSock()

    monkeypatch.setattr(_socket, "socket", fake_socket)
    ipc._open("tcp://10.0.0.5:6601", timeout=0.1)
    assert seen["addr"] == ("10.0.0.5", 6601)
    assert seen["fam"] == _socket.AF_INET


# ---- router backend resolution ----------------------------------------------

class _FakeBackend:
    def __init__(self, loaded=False, uri=None):
        self._loaded = loaded
        self._uri = uri
        self.calls = []

    def loaded(self, target=None): return self._loaded
    def now_playing_uri(self, target=None): return self._uri
    def duck(self, target=None, level=15): self.calls.append(("duck", level))
    def unduck(self, target=None, restore=100): self.calls.append(("unduck", restore))
    def pause(self, target=None): self.calls.append(("pause",))
    def resume(self, target=None): self.calls.append(("resume",))
    def stop(self, target=None): self.calls.append(("stop",))
    def position(self, target=None): return 123
    def seek_cur(self, target=None, position_ms=0): self.calls.append(("seek", position_ms))
    def play(self, uri, target=None, replace=True, **k): self.calls.append(("play", uri, replace))


def test_router_prefers_phone_when_live(monkeypatch):
    monkeypatch.setattr("agent_media_core.sinks.music_router._local_configured",
                        lambda: True)
    mopidy = _FakeBackend(loaded=False, uri="yt:mopidy")
    local = _FakeBackend(loaded=True, uri="/cache/track.webm")
    r = SinkMusicRouter(mopidy=mopidy, local=local)
    assert r.now_playing_uri() == "/cache/track.webm"
    r.duck(level=12)
    assert ("duck", 12) in local.calls and ("duck", 12) not in mopidy.calls


def test_router_falls_back_to_mopidy_when_phone_idle(monkeypatch):
    monkeypatch.setattr("agent_media_core.sinks.music_router._local_configured",
                        lambda: True)
    mopidy = _FakeBackend(loaded=False, uri="yt:mopidy")
    local = _FakeBackend(loaded=False, uri=None)
    r = SinkMusicRouter(mopidy=mopidy, local=local)
    assert r.now_playing_uri() == "yt:mopidy"
    r.duck(level=9)
    assert ("duck", 9) in mopidy.calls and not local.calls


def test_router_ignores_phone_when_unconfigured(monkeypatch):
    # No bridge probe should happen when the phone backend isn't configured.
    monkeypatch.setattr("agent_media_core.sinks.music_router._local_configured",
                        lambda: False)
    mopidy = _FakeBackend(loaded=False, uri="yt:mopidy")
    local = _FakeBackend(loaded=True, uri="/should/not/win")

    def boom(*a, **k):
        raise AssertionError("phone backend probed while unconfigured")

    local.loaded = boom
    r = SinkMusicRouter(mopidy=mopidy, local=local)
    assert r.now_playing_uri() == "yt:mopidy"


def test_router_play_routes_by_target():
    mopidy, local = _FakeBackend(), _FakeBackend()
    r = SinkMusicRouter(mopidy=mopidy, local=local)
    r.play("yt:x", Target(name="rooms"))
    r.play("yt:y", Target(name="phone"))
    assert ("play", "yt:x", True) in mopidy.calls
    assert ("play", "yt:y", True) in local.calls


# ---- phone cache seeding -----------------------------------------------------

def test_seed_from_rooms_cache_copies_when_phone_missing(monkeypatch):
    from agent_media_core.sinks import music_local

    monkeypatch.setattr(music_local, "_phone_cached_path", lambda vid: None)
    monkeypatch.setattr(music_local, "_rooms_cached_path",
                        lambda vid: f"/home/u/.cache/music-offline/{vid}.mka")
    copied = {}

    def copy(path):
        copied["path"] = path
        return "/data/data/com.termux/files/home/.cache/music-offline/a82hE1aupo8.mka"

    monkeypatch.setattr(music_local, "_copy_rooms_to_phone", copy)
    got = music_local.seed_from_rooms_cache("https://youtu.be/a82hE1aupo8")
    assert copied["path"] == "/home/u/.cache/music-offline/a82hE1aupo8.mka"
    assert got.endswith("a82hE1aupo8.mka")


def test_seed_from_rooms_cache_uses_phone_cache_first(monkeypatch):
    from agent_media_core.sinks import music_local

    monkeypatch.setattr(music_local, "_phone_cached_path",
                        lambda vid: "/phone/cache/a82hE1aupo8.mka")

    def no_rooms(*a, **k):
        raise AssertionError("rooms cache should not be probed")

    monkeypatch.setattr(music_local, "_rooms_cached_path", no_rooms)
    assert music_local.seed_from_rooms_cache("a82hE1aupo8") == "/phone/cache/a82hE1aupo8.mka"


def test_phone_play_loads_seeded_file_without_fetch(monkeypatch):
    from agent_media_core.sinks import music_local

    monkeypatch.setattr(music_local, "seed_from_rooms_cache",
                        lambda uri: "/phone/cache/a82hE1aupo8.mka")
    monkeypatch.setattr(music_local, "_watch_id", lambda uri: "a82hE1aupo8")
    monkeypatch.setattr(music_local, "_phone_title", lambda vid: "Cached Title")
    calls = []
    monkeypatch.setattr(music_local.ipc, "command",
                        lambda ep, *cmd: calls.append((ep, cmd)))

    def no_fetch(*a, **k):
        raise AssertionError("download helper should not run")

    monkeypatch.setattr(music_local.subprocess, "run", no_fetch)
    SinkMusicLocal(ep="tcp://phone:6601").play("https://youtu.be/a82hE1aupo8")
    assert calls[0][0] == "tcp://phone:6601"
    assert calls[0][1][:3] == ("loadfile", "/phone/cache/a82hE1aupo8.mka", "replace")
    assert "force-media-title" in calls[0][1][4]


# ---- auto routing (_resolve_music_where) ------------------------------------

def test_resolve_where_explicit(monkeypatch):
    from agent_media_core.cli import _resolve_music_where
    assert _resolve_music_where("rooms") == "rooms"
    assert _resolve_music_where("local") == "rooms"
    assert _resolve_music_where("phone") == "phone"


def test_resolve_where_auto_unconfigured(monkeypatch):
    from agent_media_core import cli
    monkeypatch.setattr("agent_media_core.sinks.music_local.configured",
                        lambda: False)
    assert cli._resolve_music_where("auto") == "rooms"


def test_resolve_where_auto_other_rooms_present(monkeypatch):
    from agent_media_core import cli, snapcast
    monkeypatch.setattr("agent_media_core.sinks.music_local.configured",
                        lambda: True)
    monkeypatch.setattr(snapcast, "connected_other_clients", lambda **k: ["sp4r"])
    assert cli._resolve_music_where("auto") == "rooms"


def test_resolve_where_auto_phone_only(monkeypatch):
    from agent_media_core import cli, snapcast
    monkeypatch.setattr("agent_media_core.sinks.music_local.configured",
                        lambda: True)
    monkeypatch.setattr(snapcast, "connected_other_clients", lambda **k: [])
    assert cli._resolve_music_where("auto") == "phone"


def test_resolve_where_auto_snapserver_down_uses_default(monkeypatch):
    from agent_media_core import cli, snapcast
    monkeypatch.setattr("agent_media_core.sinks.music_local.configured",
                        lambda: True)

    def down(**k):
        raise snapcast.SnapcastError("unreachable")

    monkeypatch.setattr(snapcast, "connected_other_clients", down)
    monkeypatch.setenv("MEDIA_MUSIC_AUTO_DEFAULT", "rooms")
    assert cli._resolve_music_where("auto") == "rooms"
    monkeypatch.setenv("MEDIA_MUSIC_AUTO_DEFAULT", "phone")
    assert cli._resolve_music_where("auto") == "phone"


# ---- volume ceiling ---------------------------------------------------------
#
# The phone mpv runs --volume-max=170 with a default --volume=130, because 100
# is below nominal on that device. These sets used to clamp at 100, so the
# first duck cycle after any speech permanently lowered the music: the restore
# wrote 100 over the captured 130, and `media music volume +N` could not lift it
# past 100 either. Regression tests, not API tests.

def _phone_sink(monkeypatch, sets):
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_ENDPOINT", "tcp://10.0.0.5:6601")
    from agent_media_core.sinks import music_local

    def fake_set(endpoint, name, value, **kw):
        sets.append((name, value))

    monkeypatch.setattr(music_local.ipc, "set_property", fake_set)
    return SinkMusicLocal()


def test_unduck_restores_above_100(monkeypatch):
    sets = []
    _phone_sink(monkeypatch, sets).unduck(restore=130)
    assert sets == [("volume", 130)]


def test_unduck_still_bounded_by_the_service_ceiling(monkeypatch):
    sets = []
    _phone_sink(monkeypatch, sets).unduck(restore=999)
    assert sets == [("volume", 170)]


def test_volume_delta_can_climb_above_100(monkeypatch):
    sets = []
    sink = _phone_sink(monkeypatch, sets)
    from agent_media_core.sinks import music_local
    monkeypatch.setattr(music_local.ipc, "get_property",
                        lambda endpoint, name, **kw: 100.0)
    sink.volume_delta(30)
    assert sets == [("volume", 130)]


def test_volume_ceiling_never_drops_below_100(monkeypatch):
    """A bad override must not make things quieter than the old behaviour."""
    from agent_media_core.sinks import music_local
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_VOLUME_MAX", "40")
    assert music_local.max_volume() == 100
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_VOLUME_MAX", "not-a-number")
    assert music_local.max_volume() == 170
