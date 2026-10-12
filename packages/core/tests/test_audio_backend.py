"""The audio seam (agent_media_core.audio_backend) and its three backends.

Runs on Linux: playerctl and mpv are mocked through subprocess.run, WinRT
through a fake session manager (the real one is only importable on Windows,
which is itself the "winrt missing" case below).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_media_core import audio_backend as ab
from agent_media_core.audio_backend import _windows as win
from agent_media_core.audio_backend._linux import LinuxBackend
from agent_media_core.audio_backend._windows import WindowsBackend
from agent_media_core.route import _mpris


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("MEDIA_AUDIO_BACKEND", raising=False)
    monkeypatch.delenv("MEDIA_SMTC_EXCLUDE", raising=False)
    ab.reset()
    yield
    ab.reset()


# ---- selection ---------------------------------------------------------------

@pytest.mark.parametrize("plat,name", [("linux", "linux"), ("win32", "windows"),
                                       ("darwin", "none"), ("android", "linux")])
def test_backend_follows_the_platform(monkeypatch, plat, name):
    monkeypatch.setattr(sys, "platform", plat)
    assert ab.backend().name == name


@pytest.mark.parametrize("forced", ["linux", "windows", "none"])
def test_backend_can_be_forced(monkeypatch, forced):
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", forced)
    assert ab.backend().name == forced


def test_an_unknown_backend_warns_once_and_keeps_the_platforms(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", "pulse")
    with caplog.at_level(logging.WARNING, logger="agent_media_core.audio_backend"):
        assert ab.backend().name == "linux"
        ab._backend = None
        assert ab.backend().name == "linux"
    assert sum("MEDIA_AUDIO_BACKEND" in r.getMessage() for r in caplog.records) == 1
    ab.reset()
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", "none")
    assert ab.backend().name == "none"


def test_backend_is_chosen_once(monkeypatch):
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", "none")
    first = ab.backend()
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", "linux")
    assert ab.backend() is first


def test_pause_enabled_reads_the_old_knob(monkeypatch):
    monkeypatch.setenv("MEDIA_MPRIS_PAUSE", "0")
    assert not ab.pause_enabled()
    monkeypatch.setenv("MEDIA_MPRIS_PAUSE", "1")
    assert ab.pause_enabled()


# ---- the none backend ----------------------------------------------------------

def test_stub_is_silent_and_logs_once(caplog):
    b = ab.AudioBackend()
    with caplog.at_level(logging.INFO, logger="agent_media_core.audio_backend"):
        assert b.playing_players() == []
        assert b.playing_players() == []
        b.pause_players(["x"])
        b.resume_players(["x"])
        assert b.named_sink_device("am") is None
        assert b.named_sink_device("am") is None
    assert len(caplog.records) == 2          # one for players, one for sinks
    assert b.default_ao() is None
    assert b.device("coreaudio/x") == "coreaudio/x"
    assert b.sink_plumbing is False


# ---- linux ------------------------------------------------------------------------

def test_linux_keeps_todays_names():
    b = LinuxBackend()
    assert b.default_ao() == "pulse"
    assert b.named_sink_device("am") == "pulse/am"
    assert b.device("pulse/am-music") == "pulse/am-music"
    assert b.device("Speakers") == "Speakers"      # no matching on Linux
    assert b.sink_plumbing is True


def test_linux_delegates_to_mpris_at_call_time(monkeypatch):
    calls = []
    monkeypatch.setattr(_mpris, "playing_players", lambda: ["firefox"])
    monkeypatch.setattr(_mpris, "pause_players", lambda n: calls.append(("pause", n)))
    monkeypatch.setattr(_mpris, "resume_players", lambda n: calls.append(("resume", n)))
    b = LinuxBackend()
    assert b.playing_players() == ["firefox"]
    b.pause_players(["firefox"])
    b.resume_players(["firefox"])
    assert calls == [("pause", ["firefox"]), ("resume", ["firefox"])]


def test_linux_end_to_end_through_playerctl(monkeypatch):
    """The seam changes nothing about which commands run on Linux."""
    status = {"firefox": "Playing", "spotify": "Paused"}
    ran = []

    def fake_run(argv, **kw):
        ran.append(argv)
        out = ""
        if argv[:2] == ["playerctl", "--list-all"]:
            out = "mopidy\nfirefox\nspotify\n"
        elif argv[0] == "playerctl" and argv[-1] == "status":
            out = status[argv[2]]
        elif argv[0] == "playerctl" and argv[-1] == "pause":
            status[argv[2]] = "Paused"
        elif argv[0] == "playerctl" and argv[-1] == "play-pause":
            status[argv[2]] = "Playing"
        return SimpleNamespace(returncode=0, stdout=out)

    monkeypatch.setattr(subprocess, "run", fake_run)
    b = LinuxBackend()
    names = b.playing_players()
    assert names == ["firefox"]
    b.pause_players(names)
    assert status["firefox"] == "Paused"
    b.resume_players(names)
    assert status == {"firefox": "Playing", "spotify": "Paused"}
    assert all(a[0] == "playerctl" for a in ran)


# ---- mpv's device list ------------------------------------------------------------

_HELP_WIN = """List of detected audio devices:
  'auto' (Autoselect device)
  'wasapi/{0.0.0.00000000}.{a1}' (Speakers (Realtek(R) Audio))
  'wasapi/{0.0.0.00000000}.{b2}' (Headphones (WH-1000XM4 Stereo))
"""


def _fake_mpv(monkeypatch, text, seen=None):
    def fake_run(argv, **kw):
        if seen is not None:
            seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=text)
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_output_devices_parses_mpvs_list(monkeypatch):
    seen = []
    _fake_mpv(monkeypatch, _HELP_WIN, seen)
    assert ab.output_devices() == [
        ("auto", "Autoselect device"),
        ("wasapi/{0.0.0.00000000}.{a1}", "Speakers (Realtek(R) Audio)"),
        ("wasapi/{0.0.0.00000000}.{b2}", "Headphones (WH-1000XM4 Stereo)"),
    ]
    assert seen[0][-1] == "--audio-device=help"


def test_output_devices_without_mpv(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("mpv")
    monkeypatch.setattr(subprocess, "run", boom)
    assert ab.output_devices() == []


# ---- windows: a fake media session manager ----------------------------------------

class _Op:
    def __init__(self, result, status=1):
        self.result, self.status = result, status

    def wait(self, timeout):
        return self.status

    def get_results(self):
        return self.result


class _Session:
    def __init__(self, app, status, ok=True):
        self.source_app_user_model_id = app
        self.status = status
        self.ok = ok
        self.calls = []

    def get_playback_info(self):
        return SimpleNamespace(playback_status=self.status)

    def try_pause_async(self):
        self.calls.append("pause")
        if self.ok:
            self.status = win._PAUSED
        return _Op(self.ok)

    def try_play_async(self):
        self.calls.append("play")
        if self.ok:
            self.status = win._PLAYING
        return _Op(self.ok)


class _Manager:
    def __init__(self, sessions):
        self.sessions = sessions

    def get_sessions(self):
        return list(self.sessions)


P, Z = win._PLAYING, win._PAUSED


def _backend(*sessions):
    mgr = _Manager(list(sessions))
    return WindowsBackend(manager_factory=lambda: mgr), mgr


def test_windows_lists_playing_sessions_but_never_mpv():
    b, _ = _backend(_Session("Spotify.exe", P), _Session("MSEdge", Z),
                    _Session("io.mpv.mpv", P), _Session("mpv.exe", P))
    assert b.playing_players() == ["Spotify.exe"]


def test_windows_names_a_second_session_of_one_app():
    b, _ = _backend(_Session("MSEdge", Z), _Session("MSEdge", P))
    assert b.playing_players() == ["MSEdge#2"]


def test_windows_extra_exclusions(monkeypatch):
    monkeypatch.setenv("MEDIA_SMTC_EXCLUDE", "spotify, VLC")
    b, _ = _backend(_Session("Spotify.exe", P), _Session("VideoLAN.VLC", P),
                    _Session("MSEdge", P))
    assert b.playing_players() == ["MSEdge"]


def test_windows_pause_then_resume_the_same_session():
    s = _Session("Spotify.exe", P)
    b, mgr = _backend(s)
    names = b.playing_players()
    b.pause_players(names)
    assert s.status == Z
    mgr.sessions = []          # gone from the list: the kept object still resumes
    b.resume_players(names)
    assert s.calls == ["pause", "play"] and s.status == P


def test_windows_resume_leaves_a_session_the_listener_restarted():
    s = _Session("Spotify.exe", P)
    b, _ = _backend(s)
    names = b.playing_players()
    b.pause_players(names)
    s.status = P               # the listener pressed play during the reply
    b.resume_players(names)
    assert s.calls == ["pause"]


def test_windows_resume_finds_a_session_by_name_in_a_new_backend():
    s = _Session("Spotify.exe", Z)
    b, _ = _backend(s)
    b.resume_players(["Spotify.exe", "Gone.exe"])
    assert s.calls == ["play"]


def test_windows_refused_pause_is_not_resumed():
    s = _Session("Spotify.exe", P, ok=False)
    b, _ = _backend(s)
    b.pause_players(b.playing_players())
    assert b._paused == {}


def test_windows_an_op_that_never_finishes_is_skipped():
    s = _Session("Spotify.exe", P)
    s.try_pause_async = lambda: _Op(None, status=0)     # still Started
    b, _ = _backend(s)
    b.pause_players(b.playing_players())               # no raise
    assert b._paused == {}


def _no_winrt(monkeypatch):
    """The real factory, with its import made to fail on any platform."""
    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", None)


def test_windows_without_winrt_is_silent_and_logs_once(monkeypatch, caplog):
    _no_winrt(monkeypatch)
    b = WindowsBackend()
    with caplog.at_level(logging.INFO, logger="agent_media_core.audio_backend"):
        assert b.playing_players() == []
        b._failed_at = None                             # skip the retry wait
        assert b.playing_players() == []
        b.pause_players(["Spotify.exe"])
        b.resume_players(["Spotify.exe"])
    assert sum("winrt" in r.getMessage() for r in caplog.records) == 1


def test_windows_manager_failure_is_retried_after_a_while(monkeypatch):
    tries = []

    def factory():
        tries.append(1)
        raise OSError("no desktop session")

    b = WindowsBackend(manager_factory=factory)
    assert b.playing_players() == [] and b.playing_players() == []
    assert len(tries) == 1
    clock = [win.time.monotonic() + win._RETRY_S + 1]
    monkeypatch.setattr(win.time, "monotonic", lambda: clock[0])
    b.playing_players()
    assert len(tries) == 2


def test_windows_output_names():
    b = WindowsBackend(manager_factory=lambda: None)
    assert b.default_ao() == "wasapi"
    assert b.named_sink_device("am") is None
    assert b.sink_plumbing is False


def test_windows_device_by_description(monkeypatch):
    seen = []
    _fake_mpv(monkeypatch, _HELP_WIN, seen)
    b = WindowsBackend(manager_factory=lambda: None)
    assert b.device("headphones") == "wasapi/{0.0.0.00000000}.{b2}"
    assert b.device("Speakers (Realtek(R) Audio)") == "wasapi/{0.0.0.00000000}.{a1}"
    assert b.device("headphones") == "wasapi/{0.0.0.00000000}.{b2}"
    assert len(seen) == 2                       # a match is remembered
    assert b.device("wasapi/{x}") == "wasapi/{x}"
    assert b.device("auto") == "auto"
    assert b.device("HDMI") == "HDMI"           # no match: passed through
    assert b.device("HDMI") == "HDMI"
    assert len(seen) == 3                       # ...and remembered too


# ---- media-audio -------------------------------------------------------------------

def test_report_and_main(monkeypatch, capsys):
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", "windows")
    monkeypatch.setattr(win, "_winrt_manager",
                        lambda: _Manager([_Session("Spotify.exe", P)]))
    _fake_mpv(monkeypatch, _HELP_WIN)
    monkeypatch.setattr(ab.backend(), "_factory",
                        lambda: _Manager([_Session("Spotify.exe", P)]))
    r = ab.report()
    assert r["backend"] == "windows" and r["default_ao"] == "wasapi"
    assert r["winrt"] == "ok (1 media session)"
    assert r["devices"][1] == {"id": "wasapi/{0.0.0.00000000}.{a1}",
                               "description": "Speakers (Realtek(R) Audio)"}
    assert r["playing"] == ["Spotify.exe"]
    assert ab.main([]) == 0
    out = capsys.readouterr().out
    assert "backend: windows" in out and "wasapi/{0.0.0.00000000}.{b2}" in out


def test_winrt_status_without_winrt(monkeypatch):
    _no_winrt(monkeypatch)
    assert win.winrt_status().startswith("not importable")


@pytest.mark.skipif(sys.platform != "win32", reason="the real media controls")
def test_winrt_is_real_on_windows():
    """On Windows core's win32 dependencies are installed: the projection
    must import. Whether a session manager is handed out depends on the
    session the tests run in, so that part is reported, not asserted."""
    status = win.winrt_status()
    print("winrt:", status)
    assert not status.startswith("not importable"), status
