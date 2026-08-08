"""Tests for the Android media pause/resume dispatch (route/_android)."""

import shlex

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

def _fake_ssh(state, sent):
    """Stand in for the phone: record the script, answer with `state`."""
    def run(host, script):
        sent.append(script)
        return state
    return run


def test_pause_for_speech_uses_a_single_round_trip(monkeypatch):
    # The whole point of the fused script: one ssh, not read-then-pause.
    sent = []
    monkeypatch.setattr(_android, "_ssh", _fake_ssh("playing", sent))
    _android.pause_for_speech("p8ar")
    assert len(sent) == 1
    assert "dumpsys media_session" in sent[0]
    assert _android.pause_cmd() in sent[0]


def test_pause_for_speech_playing_resumes(monkeypatch):
    sent = []
    monkeypatch.setattr(_android, "_ssh", _fake_ssh("playing", sent))
    assert _android.pause_for_speech("p8ar") is True


def test_pause_for_speech_stopped_does_not_resume(monkeypatch):
    sent = []
    monkeypatch.setattr(_android, "_ssh", _fake_ssh("stopped", sent))
    assert _android.pause_for_speech("p8ar") is False


def test_pause_for_speech_unknown_pauses_but_no_resume_by_default(monkeypatch):
    # Dispatch pause defensively (safe no-op if idle) but DON'T auto-resume —
    # `dispatch play` would start an idle session.
    sent = []
    monkeypatch.setattr(_android, "_ssh", _fake_ssh("unknown", sent))
    assert _android.pause_for_speech("p8ar") is False
    assert _android.pause_cmd() in sent[0]


def test_pause_for_speech_unknown_resumes_when_opted_in(monkeypatch):
    monkeypatch.setenv("MEDIA_ANDROID_RESUME_ON_UNKNOWN", "1")
    monkeypatch.setattr(_android, "_ssh", lambda h, s: None)
    assert _android.pause_for_speech("p8ar") is True


def test_pause_for_speech_unknown_skipped_when_detection_required(monkeypatch):
    monkeypatch.setenv("MEDIA_ANDROID_REQUIRE_PLAYING_DETECTION", "1")
    sent = []
    monkeypatch.setattr(_android, "_ssh", _fake_ssh("unknown", sent))
    assert _android.pause_for_speech("p8ar") is False


# --- the remote script actually runs under /bin/sh -------------------------
#
# The classify-and-pause decision now lives in hand-written POSIX sh executed
# on the phone, so exercise it for real rather than trusting the substring.

def _run_script(monkeypatch, dumpsys_output, *, require_detection=False):
    """Run the generated script under sh with dumpsys and pause stubbed out."""
    import subprocess as sp
    if require_detection:
        monkeypatch.setenv("MEDIA_ANDROID_REQUIRE_PLAYING_DETECTION", "1")
    else:
        monkeypatch.delenv("MEDIA_ANDROID_REQUIRE_PLAYING_DETECTION", raising=False)
    monkeypatch.setenv("MEDIA_ANDROID_PAUSE_CMD", "echo PAUSED")
    script = _android._pause_for_speech_script().replace(
        "/system/bin/dumpsys media_session 2>&1",
        f"printf '%s' {shlex.quote(dumpsys_output)}")
    r = sp.run(["/bin/sh", "-s"], input=script, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.strip().splitlines() if ln]
    return lines[-1], ("PAUSED" in r.stdout)


def test_script_classifies_playing_and_pauses(monkeypatch):
    state, paused = _run_script(
        monkeypatch, "  PlaybackState {state=3, position=12}")
    assert (state, paused) == ("playing", True)


def test_script_classifies_buffering_as_playing(monkeypatch):
    state, paused = _run_script(monkeypatch, "PlaybackState {state=8}")
    assert (state, paused) == ("playing", True)


def test_script_classifies_stopped_and_leaves_it_alone(monkeypatch):
    state, paused = _run_script(
        monkeypatch, "  PlaybackState {state=1, position=0}")
    assert (state, paused) == ("stopped", False)


def test_script_treats_permission_denial_as_unknown_and_pauses(monkeypatch):
    state, paused = _run_script(monkeypatch, "Permission Denial: can't dump")
    assert (state, paused) == ("unknown", True)


def test_script_treats_empty_output_as_unknown(monkeypatch):
    state, paused = _run_script(monkeypatch, "")
    assert (state, paused) == ("unknown", True)


def test_script_does_not_pause_unknown_when_detection_required(monkeypatch):
    state, paused = _run_script(monkeypatch, "Permission Denial",
                                require_detection=True)
    assert (state, paused) == ("unknown", False)


def test_script_still_pauses_playing_when_detection_required(monkeypatch):
    state, paused = _run_script(monkeypatch, "PlaybackState {state=3}",
                                require_detection=True)
    assert (state, paused) == ("playing", True)


def test_unreachable_host_reads_as_unknown(monkeypatch):
    monkeypatch.setattr(_android, "_ssh", lambda h, s: None)
    assert _android.pause_for_speech("p8ar") is False


def test_slow_host_is_skipped_on_the_next_call(monkeypatch, tmp_path):
    """A host that answers slowly is not asked again for the cool-off window."""
    import time as _t
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))   # never touch real state
    monkeypatch.setattr(_android, "_slow_until", None)
    monkeypatch.setenv("MEDIA_ANDROID_SLOW_MS", "10")
    monkeypatch.setenv("MEDIA_ANDROID_BREAKER_S", "30")
    calls = []

    def slow_run(cmd, **kw):
        calls.append(cmd)
        _t.sleep(0.05)
        class R:
            returncode = 0
            stdout = "playing"
            stderr = ""
        return R()

    monkeypatch.setattr(_android.subprocess, "run", slow_run)
    assert _android._ssh("p8ar", "x") == "playing"     # slow -> trips breaker
    assert _android._ssh("p8ar", "x") is None          # skipped, no subprocess
    assert len(calls) == 1

    # and it survives a fresh process: the deadline is on disk, not in memory
    monkeypatch.setattr(_android, "_slow_until", None)
    assert _android._ssh("p8ar", "x") is None
    assert len(calls) == 1
