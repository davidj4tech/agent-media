"""The `app` music target: Sasonica on the phone, mpv when it declines.

`app` used to reach Mopidy, which does not implement it, so every untargeted
music call raised "sink-music target 'app' not yet supported" once the speech
lane moved to the app.
"""

from __future__ import annotations

import pytest

from agent_media_core.sinks import music_app, music_fetch
from agent_media_core.sinks.music_app import SinkMusicApp
from agent_media_core.sinks.music_router import SinkMusicRouter
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


def _router(monkeypatch, *, app, local, mopidy=None, app_on=True, local_on=True):
    monkeypatch.setattr("agent_media_core.sinks.music_router._app_configured",
                        lambda: app_on)
    monkeypatch.setattr("agent_media_core.sinks.music_router._local_configured",
                        lambda: local_on)
    return SinkMusicRouter(mopidy=mopidy or _Fake(), local=local, app=app)


def test_app_target_plays_in_the_app(monkeypatch):
    app, local = _Fake(takes=True), _Fake()
    _router(monkeypatch, app=app, local=local).play("yt:abc", Target("app"))
    assert app.calls == [("play", "yt:abc", True)]
    assert local.calls == []


def test_app_declines_so_the_phone_mpv_plays(monkeypatch):
    app, local = _Fake(takes=False), _Fake()
    _router(monkeypatch, app=app, local=local).play("yt:abc", Target("app"))
    assert ("play", "yt:abc", True) in local.calls


def test_untargeted_play_follows_the_app_default(monkeypatch):
    monkeypatch.setenv("MEDIA_MUSIC_DEFAULT_TARGET", "app")
    app, local = _Fake(takes=True), _Fake()
    _router(monkeypatch, app=app, local=local).play("yt:abc")
    assert app.calls and not local.calls


def test_app_target_reads_never_reach_mopidy(monkeypatch):
    class Mopidy(_Fake):
        def now_playing_uri(self, target=None):
            raise NotImplementedError("sink-music target 'app' not yet supported")
    app, local = _Fake(loaded=False), _Fake(loaded=False, uri=None)
    r = _router(monkeypatch, app=app, local=local, mopidy=Mopidy())
    assert r.now_playing_uri(Target("app")) is None


def test_observe_prefers_the_app_holding_music(monkeypatch):
    app = _Fake(loaded=True, uri="http://red5:8780/music/x.mka")
    local = _Fake(loaded=True, uri="/cache/x.mka")
    r = _router(monkeypatch, app=app, local=local)
    assert r.now_playing_uri() == "http://red5:8780/music/x.mka"
    r.duck(level=10)
    assert ("duck", 10) in app.calls and not local.calls


# ---- the backend ------------------------------------------------------------

def _state(monkeypatch, st):
    monkeypatch.setattr(music_app, "configured", lambda: True)
    sent = []

    def request(target, route, params=None, timeout=50.0):
        sent.append((route, params))
        return dict(st) if route == "/state" else {"ok": True}
    monkeypatch.setattr(music_app.phone_player, "request", request)
    return sent


def test_a_book_in_the_app_is_not_music(monkeypatch):
    sent = _state(monkeypatch, {"item": "li_1", "url": None, "closed": False})
    b = SinkMusicApp()
    assert not b.loaded()
    b.pause()
    assert [r for r, _ in sent if r != "/state"] == []


def test_music_in_the_app_is_paused(monkeypatch):
    sent = _state(monkeypatch, {"item": None, "url": "http://h/m.mka",
                                "closed": False, "paused": False, "t": 12.5})
    b = SinkMusicApp()
    assert b.loaded() and b.active() and b.position() == 12500
    b.pause()
    assert ("/pause", None) in sent


def test_play_sends_the_served_url_at_normal_speed(monkeypatch):
    sent = _state(monkeypatch, {})
    monkeypatch.setattr(music_app, "resolve", lambda uri: ("http://h/music/v.mka", "A Song"))
    monkeypatch.setattr("agent_media_core.sinks.music_local._note_title", lambda u, t: None)
    assert SinkMusicApp().play("yt:https://youtu.be/abcdefghijk")
    assert ("/play", {"url": "http://h/music/v.mka", "title": "A Song", "rate": 1.0}) in sent


def test_append_is_not_something_the_app_does(monkeypatch):
    _state(monkeypatch, {})
    assert SinkMusicApp().play("https://x/y.mp3", replace=False) is False


def test_resolve_links_a_fetched_track_under_the_clip_server(monkeypatch, tmp_path):
    cache = tmp_path / "music-offline"
    cache.mkdir()
    track = cache / "abcdefghijk.mka"
    track.write_bytes(b"x")
    (cache / "abcdefghijk.title").write_text("A Song\n")
    root = tmp_path / "agent-media"
    monkeypatch.setenv("MEDIA_BOOK_HTTP_ROOT", str(root))
    monkeypatch.setenv("MEDIA_BOOK_BASEURL_PHONE", "http://red5:8780")
    monkeypatch.delenv("MEDIA_MUSIC_APP_BASEURL", raising=False)
    monkeypatch.setattr(music_fetch, "rooms_ssh_host", lambda: None)
    monkeypatch.setattr(music_fetch, "ensure_local", lambda url: str(track))
    url, title = music_app.resolve("yt:https://www.youtube.com/watch?v=abcdefghijk")
    assert url == "http://red5:8780/music/abcdefghijk.mka"
    assert title == "A Song"
    assert (root / "music" / "abcdefghijk.mka").resolve() == track.resolve()


def test_resolve_passes_a_stream_url_through(monkeypatch):
    assert music_app.resolve("https://radio.example/stream.mp3") == (
        "https://radio.example/stream.mp3", "")


@pytest.mark.parametrize("uri", ["local:track:foo.mp3", "spotify:track:1"])
def test_resolve_declines_what_the_app_cannot_fetch(uri):
    assert music_app.resolve(uri) == (None, "")
