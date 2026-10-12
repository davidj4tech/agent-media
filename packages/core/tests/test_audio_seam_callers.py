"""The callers of the audio seam: they ask audio_backend, not Linux tools.

Linux behaviour itself is pinned by the existing tests (test_speech_targets,
test_setup, the coordinator's); these check what changes with the backend.
"""

from __future__ import annotations

import argparse
import os

import pytest

from agent_media_core import audio_backend, setup
from agent_media_core.route import coordinator as coord_mod
from agent_media_core.sinks import book, speech
from agent_media_core.types import Target


class _Fake(audio_backend.AudioBackend):
    name = "fake"

    def __init__(self, *, sink=None, ao=None, plumbing=False):
        self.calls = []
        self._sink, self._ao = sink, ao
        self.sink_plumbing = plumbing

    def playing_players(self):
        self.calls.append("playing")
        return ["Spotify.exe"]

    def pause_players(self, names):
        self.calls.append(("pause", names))

    def resume_players(self, names):
        self.calls.append(("resume", names))

    def default_ao(self):
        return self._ao

    def named_sink_device(self, sink):
        return self._sink

    def device(self, spec):
        return f"resolved:{spec}"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("MEDIA_SPEECH_", "MEDIA_BOOK_")) or k == "MEDIA_ROOMS_SINK":
            monkeypatch.delenv(k, raising=False)
    audio_backend.reset()
    yield
    audio_backend.reset()


def _use(monkeypatch, fake):
    monkeypatch.setattr(audio_backend, "_backend", fake)
    return fake


# ---- devices -------------------------------------------------------------------

def test_rooms_without_named_sinks_is_not_configured(monkeypatch):
    _use(monkeypatch, _Fake(sink=None))
    with pytest.raises(NotImplementedError):
        speech._device_for(Target("rooms"))
    with pytest.raises(NotImplementedError):
        book._device_for(Target("rooms"))


def test_rooms_with_named_sinks_takes_the_backends_name(monkeypatch):
    _use(monkeypatch, _Fake(sink="x/am"))
    assert speech._device_for(Target("rooms")) == "x/am"
    assert book._device_for(Target("rooms")) == "x/am"


def test_a_local_device_goes_through_the_backend(monkeypatch):
    _use(monkeypatch, _Fake())
    monkeypatch.setenv("MEDIA_SPEECH_LOCAL_DEVICE", "Headphones")
    monkeypatch.setenv("MEDIA_BOOK_DEVICE", "Speakers")
    assert speech._device_for(Target("local")) == "resolved:Headphones"
    assert book._device_for(Target("local")) == "resolved:Speakers"
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_ROOMS", "HDMI")
    assert speech._device_for(Target("rooms")) == "resolved:HDMI"


def test_a_remote_brokers_device_is_left_alone(monkeypatch):
    """A tcp:// broker is another host's mpv: its devices are its own."""
    _use(monkeypatch, _Fake())
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://p8a:6600")
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_PHONE", "opensles")
    assert speech._device_for(Target("phone")) == "opensles"
    assert book._device_for(Target("phone")) == "opensles"


def test_linux_devices_are_unchanged(monkeypatch):
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", "linux")
    monkeypatch.setenv("MEDIA_SPEECH_LOCAL_DEVICE", "pulse/am")
    assert speech._device_for(Target("local")) == "pulse/am"
    assert speech._device_for(Target("rooms")) == "pulse/am"
    assert book._device_for(Target("rooms")) == "pulse/am"


# ---- the book broker's AO ----------------------------------------------------------

class _Spawned(Exception):
    pass


def _book_argv(monkeypatch, tmp_path):
    seen = {}

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        raise _Spawned()

    b = book.SinkBook()
    b._sock = tmp_path / "sink-book.sock"
    monkeypatch.setattr(b, "_running", lambda: False)
    monkeypatch.setattr(book.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("MEDIA_BOOK_WEBUI", "")
    with pytest.raises(_Spawned):
        b._ensure_broker()
    return seen["argv"]


@pytest.mark.parametrize("backend,want", [("linux", "--ao=pulse"),
                                          ("windows", "--ao=wasapi")])
def test_book_ao_is_the_backends(monkeypatch, tmp_path, backend, want):
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", backend)
    argv = _book_argv(monkeypatch, tmp_path)
    assert [a for a in argv if a.startswith("--ao")] == [want]


def test_book_ao_none_lets_mpv_choose(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", "none")
    argv = _book_argv(monkeypatch, tmp_path)
    assert not [a for a in argv if a.startswith("--ao")]


def test_book_ao_env_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", "windows")
    monkeypatch.setenv("MEDIA_BOOK_AO", "openal")
    argv = _book_argv(monkeypatch, tmp_path)
    assert [a for a in argv if a.startswith("--ao")] == ["--ao=openal"]


# ---- media-setup server ---------------------------------------------------------------

def test_server_is_skipped_without_sink_plumbing(monkeypatch, capsys):
    _use(monkeypatch, _Fake(plumbing=False))
    called = []
    monkeypatch.setattr(setup, "_service_backend",
                        lambda *a, **k: called.append(1) or "systemd")
    args = argparse.Namespace(backend="auto", music=False, now=False, dry_run=True)
    assert setup.cmd_server(args) == 0
    assert called == []
    assert "skipped" in capsys.readouterr().err


# ---- the coordinator -------------------------------------------------------------------

class _NoState:
    def get_now_playing(self, sink):
        return None


def test_after_speech_resumes_through_the_backend(monkeypatch):
    """Fakes all round: nothing here may reach this host's real players."""
    fake = _use(monkeypatch, _Fake())
    resumed_mpris = []
    monkeypatch.setattr(coord_mod._mpris, "resume_players",
                        lambda n: resumed_mpris.append(n))
    c = coord_mod.Coordinator(music=object(), state=_NoState(), book=object())
    for quiet in ("_speaking", "_rooms_unduck"):
        monkeypatch.setattr(c, quiet, lambda *a, **k: None)
    monkeypatch.setattr(c, "_music_duck_marker", lambda: None)
    c._mpris_paused = ["Spotify.exe"]
    c.after_speech()
    assert fake.calls == [("resume", ["Spotify.exe"])]
    assert resumed_mpris == []
    assert c._mpris_paused == []
