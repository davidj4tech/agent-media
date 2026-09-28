"""The `sasonica` music target: Sasonica's own media player, mpv when it declines.

The app answers mpv's IPC on its own port (sasonica-app Media3Music), so the
backend is the phone-mpv one with a different endpoint and a play that hands
the app a URL it can fetch (docs/proposals/2026-09-28-music-tab.md).
"""

from __future__ import annotations

from agent_media_core import audio_targets
from agent_media_core.sinks import music_sasonica
from agent_media_core.sinks.music_router import SinkMusicRouter
from agent_media_core.sinks.music_sasonica import SinkMusicSasonica
from agent_media_core.types import Target


class _Fake:
    def __init__(self, loaded=False, uri=None, takes=True):
        self._loaded, self._uri, self._takes = loaded, uri, takes
        self.calls = []

    def loaded(self, target=None): return self._loaded
    def now_playing_uri(self, target=None): return self._uri
    def pause(self, target=None): self.calls.append(("pause",))
    def duck(self, target=None, level=15): self.calls.append(("duck", level))

    def play(self, uri, target=None, replace=True, **k):
        self.calls.append(("play", uri, replace))
        return self._takes


def _router(monkeypatch, *, sasonica, local, app=None, on=True):
    monkeypatch.setattr("agent_media_core.sinks.music_router._sasonica_configured", lambda: on)
    monkeypatch.setattr("agent_media_core.sinks.music_router._app_configured", lambda: False)
    monkeypatch.setattr("agent_media_core.sinks.music_router._local_configured", lambda: True)
    return SinkMusicRouter(mopidy=_Fake(), local=local, app=app or _Fake(), sasonica=sasonica)


def test_the_target_plays_in_sasonica(monkeypatch):
    s, local = _Fake(), _Fake()
    _router(monkeypatch, sasonica=s, local=local).play("yt:abc", Target("sasonica"))
    assert s.calls == [("play", "yt:abc", True)] and local.calls == []


def test_declined_so_the_phone_mpv_plays(monkeypatch):
    s, local = _Fake(takes=False), _Fake()
    _router(monkeypatch, sasonica=s, local=local).play("yt:abc", Target("sasonica"))
    assert ("play", "yt:abc", True) in local.calls


def test_the_coordinator_follows_sasonica_when_it_holds_music(monkeypatch):
    s = _Fake(loaded=True, uri="http://red5:8780/music/x.mka")
    local = _Fake(loaded=True, uri="/cache/x.mka")
    r = _router(monkeypatch, sasonica=s, local=local)
    assert r.now_playing_uri() == "http://red5:8780/music/x.mka"
    r.duck(level=10)
    assert ("duck", 10) in s.calls and not local.calls


def test_unconfigured_is_never_asked(monkeypatch):
    s = _Fake(loaded=True, uri="http://x")
    local = _Fake(loaded=True, uri="/cache/x.mka")
    r = _router(monkeypatch, sasonica=s, local=local, on=False)
    assert r.now_playing_uri() == "/cache/x.mka"


# ---- the backend ------------------------------------------------------------

def _wire(monkeypatch):
    sent = []
    monkeypatch.setattr(music_sasonica.ipc, "command", lambda ep, *a: sent.append((ep, "cmd", *a)))
    monkeypatch.setattr(music_sasonica.ipc, "set_property",
                        lambda ep, n, v: sent.append((ep, "set", n, v)))
    monkeypatch.setattr(music_sasonica, "_note_title", lambda uri, title: None)
    return sent


def test_play_hands_the_app_a_served_url(monkeypatch):
    sent = _wire(monkeypatch)
    monkeypatch.setattr(music_sasonica.music_app, "resolve",
                        lambda uri: ("http://red5:8780/audio/music/x.mka", "A Song"))
    assert SinkMusicSasonica("tcp://p8a:6615").play("yt:https://youtu.be/aaaaaaaaaaa")
    assert sent == [
        ("tcp://p8a:6615", "cmd", "loadfile", "http://red5:8780/audio/music/x.mka", "replace"),
        ("tcp://p8a:6615", "set", "force-media-title", "A Song"),
        ("tcp://p8a:6615", "set", "pause", False),
    ]


def test_add_appends_and_leaves_pause_alone(monkeypatch):
    sent = _wire(monkeypatch)
    monkeypatch.setattr(music_sasonica.music_app, "resolve", lambda uri: ("http://x/a.mka", ""))
    assert SinkMusicSasonica("tcp://p8a:6615").play("http://x/a.mka", replace=False)
    assert sent == [("tcp://p8a:6615", "cmd", "loadfile", "http://x/a.mka", "append-play")]


def test_nothing_playable_declines(monkeypatch):
    sent = _wire(monkeypatch)
    monkeypatch.setattr(music_sasonica.music_app, "resolve", lambda uri: (None, ""))
    assert SinkMusicSasonica("tcp://p8a:6615").play("spotify:track:x") is False
    assert sent == []


def test_an_unreachable_app_declines(monkeypatch):
    monkeypatch.setattr(music_sasonica.music_app, "resolve", lambda uri: ("http://x/a.mka", ""))

    def refuse(*a):
        raise OSError("connection refused")
    monkeypatch.setattr(music_sasonica.ipc, "command", refuse)
    assert SinkMusicSasonica("tcp://p8a:6615").play("http://x/a.mka") is False


def test_restore_is_on_the_apps_scale(monkeypatch):
    sent = _wire(monkeypatch)
    SinkMusicSasonica("tcp://p8a:6615").unduck(restore=130)
    assert sent == [("tcp://p8a:6615", "set", "volume", 100)]


def test_offered_in_the_picker_only_when_configured(monkeypatch):
    names = {o["name"]: o for o in audio_targets.music_options()}
    assert names["sasonica"]["available"] is False
    monkeypatch.setenv("MEDIA_MUSIC_SASONICA_ENDPOINT", "tcp://p8a:6615")
    names = {o["name"]: o for o in audio_targets.music_options()}
    assert names["sasonica"] == {"name": "sasonica", "label": "Phone (Sasonica)",
                                 "available": True, "why": None}
