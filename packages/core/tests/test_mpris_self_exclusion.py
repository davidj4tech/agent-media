"""agent-media must never pause its own players for speech.

Once the mpv sinks expose MPRIS (via mpv-mpris) they look like any other
non-Mopidy player to the pre-speech pause sweep, so the sweep could pause the
speech broker it is clearing the way for. These tests pin the identification
rules, all of which are name-independent: mpv-mpris bus names carry a RANDOM
suffix and cannot be matched on.
"""

from __future__ import annotations

import pytest

from agent_media_core.route import _mpris


@pytest.fixture
def fake_bus(monkeypatch):
    """Wire up a fake `name -> pid` bus and `pid -> cmdline` process table."""
    state: dict[str, dict] = {"pids": {}, "cmdlines": {}}

    def _bus_pid(name):
        return state["pids"].get(name)

    def _open_cmdline(pid):
        if pid not in state["cmdlines"]:
            raise OSError("no such process")
        return state["cmdlines"][pid]

    monkeypatch.setattr(_mpris, "_bus_pid", _bus_pid)

    real_open = open

    def fake_open(path, *a, **kw):
        if str(path).startswith("/proc/"):
            return _FakeFile(_open_cmdline(int(str(path).split("/")[2])))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    return state


class _FakeFile:
    def __init__(self, text): self._text = text.encode().replace(b" ", b"\0")
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._text


# --- identification ------------------------------------------------------

def test_speech_broker_is_recognised_as_ours(fake_bus):
    fake_bus["pids"]["mpv.instance-sLDnmIKJ"] = 4242
    fake_bus["cmdlines"][4242] = (
        "mpv --idle=yes --ao=pulse "
        "--input-ipc-server=/home/ryer/.local/state/agent-media/sink-speech.sock"
    )
    assert _mpris.is_own_player("mpv.instance-sLDnmIKJ") is True


def test_music_mpv_recognised_by_audio_client_name(fake_bus):
    fake_bus["pids"]["mpv"] = 7
    fake_bus["cmdlines"][7] = "mpv --idle=yes --audio-client-name=mopidy-mpv-music"
    assert _mpris.is_own_player("mpv") is True


def test_book_broker_is_recognised_as_ours(fake_bus):
    fake_bus["pids"]["mpv.instance-Qz9"] = 8
    fake_bus["cmdlines"][8] = "mpv --no-config --audio-client-name=agent-media-book"
    assert _mpris.is_own_player("mpv.instance-Qz9") is True


def test_foreign_mpv_is_not_ours(fake_bus):
    fake_bus["pids"]["mpv.instance-XYZ"] = 9
    fake_bus["cmdlines"][9] = "mpv /home/ryer/Videos/holiday.mkv"
    assert _mpris.is_own_player("mpv.instance-XYZ") is False


def test_non_mpv_players_are_never_ours(fake_bus):
    # No bus lookup should even be attempted for these.
    assert _mpris.is_own_player("chromium.instance12345") is False
    assert _mpris.is_own_player("firefox") is False
    assert _mpris.is_own_player("spotify") is False


# --- fail-closed ---------------------------------------------------------

def test_unidentifiable_mpv_is_left_alone(fake_bus):
    """No bus answer -> assume ours. Silencing our own speech is the worse
    failure than leaving a stranger's audio playing under the clip."""
    assert _mpris.is_own_player("mpv") is True


def test_dead_process_is_left_alone(fake_bus):
    fake_bus["pids"]["mpv"] = 999999          # owner vanished mid-sweep
    assert _mpris.is_own_player("mpv") is True


# --- the sweep itself ----------------------------------------------------

def test_playing_players_skips_mopidy_and_own_mpv(monkeypatch):
    monkeypatch.setattr(_mpris, "is_own_player",
                        lambda n: n == "mpv.instance-ours")

    calls = []

    def fake_run(*args):
        calls.append(args)
        if args == ("--list-all",):
            return "mopidy\nmpv.instance-ours\nmpv.instance-theirs\nfirefox"
        if args[:2] == ("--player",) + (args[1],) and args[2] == "status":
            return "Playing"
        return None

    monkeypatch.setattr(_mpris, "_run", fake_run)
    assert _mpris.playing_players() == ["mpv.instance-theirs", "firefox"]
    # Excluded players must not even be probed for status.
    assert ("--player", "mopidy", "status") not in calls
    assert ("--player", "mpv.instance-ours", "status") not in calls


def test_mopidy_exclusion_is_case_insensitive(monkeypatch):
    """Mopidy-MPRIS publishes lowercase `mopidy`; the old tuple said `Mopidy`
    and so silently matched nothing."""
    monkeypatch.setattr(_mpris, "is_own_player", lambda n: False)
    monkeypatch.setattr(_mpris, "_run", lambda *a: (
        "mopidy" if a == ("--list-all",) else "Playing"))
    assert _mpris.playing_players() == []


# --- resume targeting ----------------------------------------------------

def test_chromium_instance_rotation_still_resolves():
    current = ["chromium.instance999"]
    assert _mpris._find_by_prefix("chromium.instance123", current) == \
        "chromium.instance999"


def test_random_mpv_suffix_never_matches_a_different_instance():
    """The bug this guards: mpv suffixes are random, so collapsing them would
    resume an unrelated mpv that happened to be registered."""
    current = ["mpv.instance-DIFFERENT"]
    assert _mpris._find_by_prefix("mpv.instance-ORIGINAL", current) is None


def test_exact_mpv_match_still_resolves():
    current = ["mpv.instance-SAME"]
    assert _mpris._find_by_prefix("mpv.instance-SAME", current) == \
        "mpv.instance-SAME"


# --- env extension -------------------------------------------------------

def test_env_markers_extend_rather_than_replace(monkeypatch):
    monkeypatch.setenv("MEDIA_MPRIS_OWN_MARKERS", "sink-voice.sock, extra-thing")
    markers = _mpris.own_mpv_markers()
    assert "sink-voice.sock" in markers and "extra-thing" in markers
    assert "sink-speech.sock" in markers      # built-ins survive
