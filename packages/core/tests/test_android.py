"""Tests for the Android media pause/resume dispatch (route/_android)."""

from agent_media_core.route import _android


def test_default_commands_use_media_session_dispatch():
    # The reliable path is `cmd media_session dispatch`, NOT am broadcast.
    assert "media_session dispatch pause" in _android.pause_cmd()
    assert "media_session dispatch play" in _android.resume_cmd()
    assert "am broadcast" not in _android.pause_cmd()


def test_default_commands_use_absolute_cmd_path():
    # Non-interactive Termux SSH usually lacks /system/bin on PATH, so the
    # dispatch binary must be addressed absolutely (same as dumpsys).
    assert _android.pause_cmd().startswith("/system/bin/cmd ")
    assert _android.resume_cmd().startswith("/system/bin/cmd ")


def test_commands_overridable_via_env(monkeypatch):
    monkeypatch.setenv("MEDIA_ANDROID_PAUSE_CMD", "input keyevent 127")
    monkeypatch.setenv("MEDIA_ANDROID_RESUME_CMD", "input keyevent 126")
    assert _android.pause_cmd() == "input keyevent 127"
    assert _android.resume_cmd() == "input keyevent 126"


def test_pause_hosts_parsing(monkeypatch):
    monkeypatch.setenv("MEDIA_ANDROID_PAUSE_HOSTS", "p8ar, phone2 ,")
    assert _android.pause_hosts() == ["p8ar", "phone2"]


def test_pause_and_resume_dispatch_expected_script(monkeypatch):
    sent = []
    monkeypatch.setattr(_android, "_ssh", lambda host, script: sent.append((host, script)))
    _android.pause("p8ar")
    _android.resume("p8ar")
    assert sent[0] == ("p8ar", _android.pause_cmd())
    assert sent[1] == ("p8ar", _android.resume_cmd())


def test_resume_disabled_skips_dispatch(monkeypatch):
    monkeypatch.setenv("MEDIA_ANDROID_RESUME", "0")
    sent = []
    monkeypatch.setattr(_android, "_ssh", lambda host, script: sent.append(script))
    _android.resume("p8ar")
    assert sent == []


# --- playback_state classification ----------------------------------------

def test_playback_state_playing(monkeypatch):
    monkeypatch.setattr(_android, "_ssh", lambda h, s: "  PlaybackState {state=3, ...}")
    assert _android.playback_state("p8ar") == "playing"


def test_playback_state_stopped(monkeypatch):
    monkeypatch.setattr(_android, "_ssh", lambda h, s: "  PlaybackState {state=1, ...}")
    assert _android.playback_state("p8ar") == "stopped"


def test_playback_state_unknown_on_permission_denial(monkeypatch):
    monkeypatch.setattr(_android, "_ssh", lambda h, s: "Permission Denial: ...")
    assert _android.playback_state("p8ar") == "unknown"


def test_playback_state_unknown_on_ssh_failure(monkeypatch):
    monkeypatch.setattr(_android, "_ssh", lambda h, s: None)
    assert _android.playback_state("p8ar") == "unknown"


# --- pause_for_speech: resume only on confirmed playback ------------------

def test_pause_for_speech_playing_pauses_and_resumes(monkeypatch):
    monkeypatch.setattr(_android, "playback_state", lambda h: "playing")
    sent = []
    monkeypatch.setattr(_android, "_ssh", lambda h, s: sent.append(s))
    assert _android.pause_for_speech("p8ar") is True
    assert sent == [_android.pause_cmd()]


def test_pause_for_speech_stopped_does_nothing(monkeypatch):
    monkeypatch.setattr(_android, "playback_state", lambda h: "stopped")
    sent = []
    monkeypatch.setattr(_android, "_ssh", lambda h, s: sent.append(s))
    assert _android.pause_for_speech("p8ar") is False
    assert sent == []


def test_pause_for_speech_unknown_pauses_but_no_resume_by_default(monkeypatch):
    # The key fix: dispatch pause defensively (safe no-op if idle) but DON'T
    # auto-resume — `dispatch play` would start an idle session.
    monkeypatch.setattr(_android, "playback_state", lambda h: "unknown")
    sent = []
    monkeypatch.setattr(_android, "_ssh", lambda h, s: sent.append(s))
    assert _android.pause_for_speech("p8ar") is False
    assert sent == [_android.pause_cmd()]


def test_pause_for_speech_unknown_resumes_when_opted_in(monkeypatch):
    monkeypatch.setenv("MEDIA_ANDROID_RESUME_ON_UNKNOWN", "1")
    monkeypatch.setattr(_android, "playback_state", lambda h: "unknown")
    monkeypatch.setattr(_android, "_ssh", lambda h, s: None)
    assert _android.pause_for_speech("p8ar") is True


def test_pause_for_speech_unknown_skipped_when_detection_required(monkeypatch):
    monkeypatch.setenv("MEDIA_ANDROID_REQUIRE_PLAYING_DETECTION", "1")
    monkeypatch.setattr(_android, "playback_state", lambda h: "unknown")
    sent = []
    monkeypatch.setattr(_android, "_ssh", lambda h, s: sent.append(s))
    assert _android.pause_for_speech("p8ar") is False
    assert sent == []
