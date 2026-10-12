"""media-sink-speech, the speech player's launcher where there is no runit or
systemd (entrypoints/sink_speech.py). mpv is faked here; on Windows CI the
launcher starts the runner's real mpv on the pipe and a client talks to it."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

from agent_media_core import audio_backend
from agent_media_core.entrypoints import sink_speech
from agent_media_core.sinks import _mpv_ipc as ipc


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("MEDIA_SPEECH_") or k == "MEDIA_MPV_BIN":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(audio_backend, "_backend", None)


def test_the_endpoint_is_the_local_targets(tmp_path):
    assert sink_speech.socket_path() == tmp_path / "state" / "agent-media" / "sink-speech.sock"


def test_a_bridge_override_is_not_where_the_player_listens(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_LOCAL", "tcp://p8a:1")
    assert sink_speech.socket_path().name == "sink-speech.sock"


def test_argv_is_the_run_scripts(monkeypatch):
    monkeypatch.setenv("MEDIA_AUDIO_BACKEND", "windows")
    argv = sink_speech.mpv_argv("mpv.exe", "\\\\.\\pipe\\x")
    assert argv[0] == "mpv.exe"
    assert "--input-ipc-server=\\\\.\\pipe\\x" in argv
    assert "--ao=wasapi" in argv and "--idle=yes" in argv
    assert "--volume=130" in argv and "--volume-max=200" in argv
    assert "--af=dynaudnorm=targetrms=0.95:maxgain=30:gausssize=15" in argv


def test_argv_follows_the_env(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_AO", "openal")
    monkeypatch.setenv("MEDIA_SPEECH_VOLUME", "90")
    monkeypatch.setenv("MEDIA_SPEECH_LOUDNORM", "")
    argv = sink_speech.mpv_argv("mpv", "/s")
    assert "--ao=openal" in argv and "--volume=90" in argv
    assert "--af=scaletempo2" in argv


def test_without_mpv_it_says_how(monkeypatch, capsys):
    monkeypatch.setattr(sink_speech, "find_mpv", lambda: None)
    assert sink_speech.run(once=True) == 2
    assert "winget install --id shinchiro.mpv -e" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="the socket-file side")
def test_once_clears_a_stale_socket_and_runs_mpv(monkeypatch, tmp_path):
    monkeypatch.setattr(sink_speech, "find_mpv", lambda: "/usr/bin/mpv")
    sock = sink_speech.socket_path()
    sock.parent.mkdir(parents=True)
    sock.write_text("")
    ran = []
    assert sink_speech.run(once=True, call=lambda a: ran.append(a) or 0) == 0
    assert not sock.exists()
    assert f"--input-ipc-server={sock}" in ran[0]


def test_it_restarts_mpv_and_backs_off(monkeypatch):
    monkeypatch.setattr(sink_speech, "find_mpv", lambda: "mpv")
    monkeypatch.setattr(ipc, "_use_pipes", lambda: False)
    slept, ran = [], []

    def sleep(s):
        slept.append(s)
        if len(slept) == 4:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        sink_speech.run(call=lambda a: ran.append(a) or 1, sleep=sleep)
    assert len(ran) == 4
    assert slept == [4.0, 8.0, 16.0, 32.0]


def test_a_player_already_on_the_pipe_is_watched_not_doubled(monkeypatch):
    monkeypatch.setattr(sink_speech, "find_mpv", lambda: "mpv")
    monkeypatch.setattr(ipc, "_use_pipes", lambda: True)
    there = iter([True, True, True, False])
    monkeypatch.setattr(ipc, "endpoint_exists", lambda ep: next(there))
    ran = []
    assert sink_speech.run(once=True, sleep=lambda s: None,
                           call=lambda a: ran.append(a) or 0) == 0
    assert ran == []


@pytest.mark.skipif(sys.platform != "win32", reason="a real pipe and mpv")
@pytest.mark.skipif(not shutil.which("mpv"), reason="mpv is not installed")
def test_the_launcher_puts_mpv_on_the_pipe_and_a_client_talks_to_it(tmp_path):
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / "state"),
               MEDIA_SPEECH_AO="null")
    proc = subprocess.Popen([sys.executable, "-m",
                             "agent_media_core.entrypoints.sink_speech", "--once"],
                            env=env)
    sock = tmp_path / "state" / "agent-media" / "sink-speech.sock"
    pipe = ipc.endpoint_name(sock)
    try:
        for _ in range(150):
            if ipc.endpoint_exists(pipe):
                break
            time.sleep(0.1)
        assert ipc.get_property(pipe, "idle-active") is True
        assert ipc.get_property(pipe, "volume") == 130
        ipc.command(pipe, "quit")
        assert proc.wait(15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
