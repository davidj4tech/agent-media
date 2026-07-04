"""Tests for the phone call guard (call_guard) — call detection + edge logic."""

from agent_media_core import call_guard


def _telecom(title="Ongoing call", content="00:12"):
    return {"packageName": "com.android.server.telecom",
            "title": title, "content": content}


def test_telecom_notification_counts_as_active(monkeypatch):
    monkeypatch.delenv("MEDIA_CALL_GUARD_PACKAGES", raising=False)
    cfg = call_guard.Config()
    assert call_guard.call_active([_telecom()], cfg) is True


def test_no_notifications_is_not_active(monkeypatch):
    cfg = call_guard.Config()
    assert call_guard.call_active([], cfg) is False


def test_unrelated_package_ignored(monkeypatch):
    cfg = call_guard.Config()
    other = {"packageName": "com.spotify.music",
             "title": "Now playing", "content": "A song"}
    assert call_guard.call_active([other], cfg) is False


def test_missed_call_is_excluded_by_default(monkeypatch):
    # A lingering "Missed call" must NOT read as a live call, or the rising-edge
    # detector would wedge and never pause a real future call.
    cfg = call_guard.Config()
    missed = _telecom(title="Missed call", content="Mum")
    assert call_guard.call_active([missed], cfg) is False


def test_packages_configurable(monkeypatch):
    monkeypatch.setenv("MEDIA_CALL_GUARD_PACKAGES",
                       "com.google.android.dialer, com.android.incallui ,")
    cfg = call_guard.Config()
    assert cfg.packages == {"com.google.android.dialer", "com.android.incallui"}
    dialer = {"packageName": "com.google.android.dialer",
              "title": "Incoming call", "content": "Unknown"}
    assert call_guard.call_active([dialer], cfg) is True


def test_include_re_narrows_match(monkeypatch):
    monkeypatch.setenv("MEDIA_CALL_GUARD_INCLUDE_RE", r"(?i)call")
    cfg = call_guard.Config()
    assert call_guard.call_active([_telecom(title="Incoming call")], cfg) is True
    # Same package but nothing call-ish in the text → filtered out.
    assert call_guard.call_active(
        [_telecom(title="Voicemail", content="1 new")], cfg) is False


def test_sockets_default_to_state_dir(monkeypatch):
    monkeypatch.delenv("MEDIA_CALL_GUARD_SOCKETS", raising=False)
    cfg = call_guard.Config()
    names = {s.rsplit("/", 1)[-1] for s in cfg.sockets}
    assert names == {"sink-speech.sock", "mpv-voice.sock", "mpv-music.sock"}


def test_sockets_overridable(monkeypatch):
    monkeypatch.setenv("MEDIA_CALL_GUARD_SOCKETS", "/a/x.sock, /b/y.sock ,")
    cfg = call_guard.Config()
    assert cfg.sockets == ["/a/x.sock", "/b/y.sock"]


def test_list_notifications_bad_json_returns_none(monkeypatch):
    cfg = call_guard.Config()

    class _R:
        returncode = 0
        stdout = "not json{"
    monkeypatch.setattr(call_guard.subprocess, "run", lambda *a, **k: _R())
    assert call_guard.list_notifications(cfg) is None


def test_list_notifications_empty_returns_empty_list(monkeypatch):
    cfg = call_guard.Config()

    class _R:
        returncode = 0
        stdout = "   "
    monkeypatch.setattr(call_guard.subprocess, "run", lambda *a, **k: _R())
    assert call_guard.list_notifications(cfg) == []


def test_list_notifications_missing_binary_returns_none(monkeypatch):
    cfg = call_guard.Config()

    def _boom(*a, **k):
        raise FileNotFoundError("termux-notification-list")
    monkeypatch.setattr(call_guard.subprocess, "run", _boom)
    assert call_guard.list_notifications(cfg) is None


def test_pause_sockets_skips_absent_and_pauses_present(monkeypatch, tmp_path):
    present = tmp_path / "sink-speech.sock"
    present.write_bytes(b"")  # exists on disk; the IPC call is stubbed below
    absent = tmp_path / "mpv-music.sock"
    monkeypatch.setenv("MEDIA_CALL_GUARD_SOCKETS", f"{present},{absent}")
    cfg = call_guard.Config()

    paused = []
    monkeypatch.setattr(call_guard.ipc, "set_property",
                        lambda sock, name, value: paused.append((str(sock), name, value)))
    call_guard.pause_sockets(cfg)
    assert paused == [(str(present), "pause", True)]


def test_pause_sockets_dry_run_touches_nothing(monkeypatch, tmp_path):
    present = tmp_path / "sink-speech.sock"
    present.write_bytes(b"")
    monkeypatch.setenv("MEDIA_CALL_GUARD_SOCKETS", str(present))
    cfg = call_guard.Config()

    called = []
    monkeypatch.setattr(call_guard.ipc, "set_property",
                        lambda *a, **k: called.append(a))
    call_guard.pause_sockets(cfg, dry_run=True)
    assert called == []
