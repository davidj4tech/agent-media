"""sink-music: YouTube playlist → per-track mpv: expansion.

A shared YouTube playlist is enumerated with yt-dlp and each track queued as
mpv:, so playback is robust (mpv+yt-dlp) and ducks for speech — instead of
relying on Mopidy-YouTube's unreliable playlist expansion.
"""

import contextlib
import subprocess

import pytest

from agent_media_core.sinks import music as m


# ---- playlist detection ---------------------------------------------------

@pytest.mark.parametrize("uri,want", [
    ("yt:https://www.youtube.com/playlist?list=PL123",
     "https://www.youtube.com/playlist?list=PL123"),
    ("https://www.youtube.com/playlist?list=UUabc",
     "https://www.youtube.com/playlist?list=UUabc"),
    ("playlist:PLxyz", "https://www.youtube.com/playlist?list=PLxyz"),
    ("yt:playlist:PLxyz", "https://www.youtube.com/playlist?list=PLxyz"),
    # NOT playlists:
    ("yt:https://youtu.be/abc", None),               # single video
    ("https://www.youtube.com/watch?v=abc&list=PLx", None),  # video-in-playlist
    ("local:track:x", None),
    ("https://example.com/a.mp3", None),
])
def test_youtube_playlist_url(uri, want):
    assert m._youtube_playlist_url(uri) == want


# ---- expansion ------------------------------------------------------------

def _fake_run(stdout):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return run


def test_expand_returns_mpv_uris(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("id1\nid2\nNA\n\nid3\n"))
    out = m._expand_youtube_playlist("yt:https://www.youtube.com/playlist?list=PL1")
    assert out == [
        "mpv:https://www.youtube.com/watch?v=id1",
        "mpv:https://www.youtube.com/watch?v=id2",
        "mpv:https://www.youtube.com/watch?v=id3",
    ]


def test_expand_non_playlist_skips_subprocess(monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    assert m._expand_youtube_playlist("yt:https://youtu.be/single") is None
    assert called == []  # never shells out for a non-playlist


def test_expand_empty_output_falls_back(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("\n  \nNA\n"))
    assert m._expand_youtube_playlist("playlist:PLempty") is None


def test_expand_ytdlp_error_falls_back(monkeypatch):
    def boom(*a, **k):
        raise OSError("yt-dlp not found")
    monkeypatch.setattr(subprocess, "run", boom)
    assert m._expand_youtube_playlist("playlist:PLx") is None


# ---- play() wiring --------------------------------------------------------

def test_play_starts_first_track_and_defers_rest(monkeypatch):
    # Playlist contract: the first track plays immediately; the tail is handed
    # to the detached fetch-and-append helper (downloads happen off-process).
    from agent_media_core.sinks import music_fetch
    monkeypatch.setattr(m, "_expand_youtube_playlist", lambda uri: ["mpv:a", "mpv:b"])
    deferred = []
    monkeypatch.setattr(music_fetch, "spawn_append_fetched",
                        lambda urls: deferred.extend(urls))

    cmds = []

    @contextlib.contextmanager
    def fake_connect(target, timeout=5.0):
        yield None

    monkeypatch.setattr(m, "_connect", fake_connect)
    monkeypatch.setattr(m, "_cmd", lambda s, line: cmds.append(line))

    m.SinkMusic().play("yt:https://www.youtube.com/playlist?list=PL1")
    assert cmds == ['clear', 'add "mpv:a"', 'play']
    assert deferred == ["b"]


def test_play_single_track_unaffected(monkeypatch):
    # A YouTube URI is localised before it plays, and the fetcher for that is
    # a residential host reached over ssh — the phone, here. This test is
    # about what lands in the MPD queue, so the errand it never asserts on is
    # pure latency, and on a box that cannot reach the fetcher, pure waiting.
    monkeypatch.setenv("MEDIA_MUSIC_ROOMS_FETCH", "0")
    # non-playlist → expansion returns None → single add via _to_music_uri
    monkeypatch.setattr(m, "_expand_youtube_playlist", lambda uri: None)
    cmds = []

    @contextlib.contextmanager
    def fake_connect(target, timeout=5.0):
        yield None

    monkeypatch.setattr(m, "_connect", fake_connect)
    monkeypatch.setattr(m, "_cmd", lambda s, line: cmds.append(line))

    m.SinkMusic().play("yt:https://youtu.be/abc")
    assert cmds == ['clear', 'add "mpv:https://youtu.be/abc"', 'play']
