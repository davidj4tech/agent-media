"""A reply rendered on the far side must stay replayable.

The phone lane rendered every reply to one fixed path that the next reply
overwrote, and recorded `remote-say:phone` in history — a pseudo-URI, not a
file. Replay handed that straight to mpv, which cannot open it, and nothing
checked, so the keypress did nothing. There was also nothing to replay in
principle: by the time you wanted a reply again, the file was whatever had
been said since.

The renderer now names each clip and reports it back, in the dir this target's
clips already resolve against. The audio is already beside the player, so
replay is a local loadfile there rather than a transfer from here.
"""

import json

import pytest

from agent_media_core.intake import submit


class _Proc:
    def __init__(self, lines):
        import io
        self.stdout = io.BytesIO(b"".join(lines))


class _Store:
    def __init__(self):
        self.now_playing = None

    def set_now_playing(self, sink, **kw):
        self.now_playing = kw


def test_watcher_reports_clip_and_duration():
    store = _Store()
    report = {}
    submit._watch_remote_progress(
        _Proc([b"CLIP remote-20260809T034512-1234.mp3\n", b"DURATION 12.5\n"]),
        store, "phone", 1000.0, report)

    assert report["clip"] == "remote-20260809T034512-1234.mp3"
    assert report["duration"] == 12.5
    ex = store.now_playing["extras"]
    assert ex["clip_uris"] == ["remote-20260809T034512-1234.mp3"]
    assert ex["clips_remote"] is True
    assert ex["total_duration_s"] == 12.5


def test_duration_without_clip_still_works():
    """An older renderer, or one that only measures. Must not regress."""
    store = _Store()
    report = {}
    submit._watch_remote_progress(
        _Proc([b"DURATION 3.0\n"]), store, "phone", 1000.0, report)

    assert "clip" not in report
    ex = store.now_playing["extras"]
    assert "clip_uris" not in ex and "clips_remote" not in ex


def test_renderer_that_says_nothing_is_silent_in_the_row():
    store = _Store()
    report = {}
    submit._watch_remote_progress(
        _Proc([b"some unrelated chatter\n"]), store, "phone", 1000.0, report)
    assert report == {} and store.now_playing is None


def test_replay_does_not_ship_a_clip_that_is_already_there(monkeypatch):
    """prefetch would look for the file on *this* host, where it never was."""
    from agent_media_core import cli

    calls = []
    monkeypatch.setattr(cli, "SinkSpeech", lambda: _FakeSink(calls))
    monkeypatch.setattr(cli, "_replay_visual", lambda ex: None)
    monkeypatch.setattr(cli.ipc, "set_property", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_sock", lambda: "tcp://phone.example:6602")

    row = {"uri": "remote-say:phone", "text": "hi",
           "extras": {"clip_uris": ["remote-1.mp3"], "clips_remote": True}}
    cli._replay_row(row)

    assert ("prefetch",) not in calls, (
        "replay tried to push a clip that the far side rendered itself — it "
        "fails, and the whole replay drops to the HTTP fallback")
    assert ("play", "remote-1.mp3") in calls


def test_replay_still_prefetches_locally_rendered_clips(monkeypatch):
    from agent_media_core import cli

    calls = []
    monkeypatch.setattr(cli, "SinkSpeech", lambda: _FakeSink(calls))
    monkeypatch.setattr(cli, "_replay_visual", lambda ex: None)
    monkeypatch.setattr(cli.ipc, "set_property", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_sock", lambda: "tcp://phone.example:6602")

    row = {"uri": "/home/x/clip.mp3", "text": "hi",
           "extras": {"clip_uris": ["/home/x/clip.mp3"]}}
    cli._replay_row(row)
    assert ("prefetch",) in calls


class _FakeSink:
    def __init__(self, calls):
        self.calls = calls

    def prefetch(self, paths, target=None):
        self.calls.append(("prefetch",))
        return True

    def play(self, uri, target=None, **kw):
        self.calls.append(("play", uri))

    def play_playlist(self, uris, target=None, **kw):
        self.calls.append(("playlist", tuple(uris)))
