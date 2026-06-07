"""sink-music: ducking/volume must reach mpv-routed (shared YouTube) tracks.

mpv's audio bypasses Mopidy's GStreamer mixer, so MPD setvol can't duck it —
SinkMusic mirrors volume onto the mpv backend's IPC socket too.
"""

import contextlib

from agent_media_core.sinks import music as m


def test_mpv_socket_env_override(monkeypatch):
    monkeypatch.setenv("MEDIA_MUSIC_MPV_SOCKET", "/tmp/x.sock")
    assert m._mpv_socket() == "/tmp/x.sock"


def test_mpv_socket_default_from_runtime(monkeypatch):
    monkeypatch.delenv("MEDIA_MUSIC_MPV_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert m._mpv_socket() == "/run/user/1000/mopidy-mpv.sock"


def test_set_mpv_volume_noop_when_socket_absent(monkeypatch):
    monkeypatch.setenv("MEDIA_MUSIC_MPV_SOCKET", "/nonexistent/nope.sock")
    calls = []
    monkeypatch.setattr(m.ipc, "set_property", lambda *a, **k: calls.append(a))
    m._set_mpv_volume(15)
    assert calls == []  # missing socket → never touches ipc


def test_set_mpv_volume_calls_ipc_and_clamps(monkeypatch, tmp_path):
    sock = tmp_path / "mpv.sock"
    sock.write_text("")  # just needs to exist
    monkeypatch.setenv("MEDIA_MUSIC_MPV_SOCKET", str(sock))
    calls = []
    monkeypatch.setattr(m.ipc, "set_property",
                        lambda s, n, v: calls.append((n, v)))
    m._set_mpv_volume(15)
    m._set_mpv_volume(250)   # clamps to 100
    m._set_mpv_volume(-5)    # clamps to 0
    assert calls == [("volume", 15.0), ("volume", 100.0), ("volume", 0.0)]


def test_set_mpv_volume_swallows_ipc_errors(monkeypatch, tmp_path):
    sock = tmp_path / "mpv.sock"
    sock.write_text("")
    monkeypatch.setenv("MEDIA_MUSIC_MPV_SOCKET", str(sock))

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(m.ipc, "set_property", boom)
    m._set_mpv_volume(15)  # must not raise — best effort


def test_duck_and_unduck_mirror_to_mpv(monkeypatch):
    @contextlib.contextmanager
    def fake_connect(target, timeout=5.0):
        yield None

    monkeypatch.setattr(m, "_connect", fake_connect)
    monkeypatch.setattr(m, "_cmd", lambda s, line: "OK")
    seen = []
    monkeypatch.setattr(m, "_set_mpv_volume", lambda lvl: seen.append(lvl))

    sink = m.SinkMusic()
    sink.duck(level=15)
    sink.unduck(restore=100)
    assert seen == [15, 100]
