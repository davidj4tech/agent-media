"""Tests for the Android media pause/resume dispatch (route/_android)."""

from agent_media_core.route import _android


def test_default_commands_use_media_session_dispatch():
    # The reliable path is `cmd media_session dispatch`, NOT am broadcast.
    assert "media_session dispatch pause" in _android.pause_cmd()
    assert "media_session dispatch play" in _android.resume_cmd()
    assert "am broadcast" not in _android.pause_cmd()


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
