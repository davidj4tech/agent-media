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
