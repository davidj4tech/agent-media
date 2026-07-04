"""Tests for the phone call guard (call_guard) — call detection + edge logic."""

import json
import socket
import threading

from agent_media_core import call_guard


def _telecom(title="Ongoing call", content="00:12"):
    return {"packageName": "com.android.server.telecom",
            "title": title, "content": content}


def _dialer(title="Steven Toshack", content="Calling"):
    return {"packageName": "com.google.android.dialer",
            "title": title, "content": content}


def test_telecom_notification_counts_as_active(monkeypatch):
    monkeypatch.delenv("MEDIA_CALL_GUARD_PACKAGES", raising=False)
    cfg = call_guard.Config()
    assert call_guard.call_active([_telecom()], cfg) is True


def test_dialer_call_counts_as_active_by_default(monkeypatch):
    # The Google/Pixel dialer is in the default package set (calibrated on the
    # target phone: an outgoing call showed pkg=dialer, content="Calling").
    monkeypatch.delenv("MEDIA_CALL_GUARD_PACKAGES", raising=False)
    cfg = call_guard.Config()
    assert call_guard.call_active([_dialer()], cfg) is True


def test_voicemail_is_excluded_by_default(monkeypatch):
    monkeypatch.delenv("MEDIA_CALL_GUARD_EXCLUDE_RE", raising=False)
    cfg = call_guard.Config()
    vm = _dialer(title="Voicemail", content="1 new voicemail")
    assert call_guard.call_active([vm], cfg) is False


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


def test_run_loop_reasserts_pause_each_active_cycle(monkeypatch):
    # A reply that starts mid-call clears its own pause, so the guard must
    # re-pause on every cycle the call is active — not just once on the ring.
    cfg = call_guard.Config()
    # Two active polls, then one clear poll, then stop.
    states = iter([[{"packageName": "com.android.server.telecom",
                     "title": "Ongoing call", "content": "00:01"}],
                   [{"packageName": "com.android.server.telecom",
                     "title": "Ongoing call", "content": "00:02"}],
                   []])

    def _next(_cfg):
        try:
            return next(states)
        except StopIteration:
            call_guard._stop = True
            return []
    monkeypatch.setattr(call_guard, "list_notifications", _next)
    monkeypatch.setattr(call_guard.time, "sleep", lambda _s: None)

    pauses = []
    monkeypatch.setattr(call_guard, "pause_sockets",
                        lambda cfg, dry_run=False, quiet=False: pauses.append(quiet))
    call_guard._stop = False
    try:
        call_guard._run_loop(cfg)
    finally:
        call_guard._stop = False
    # Paused on both active cycles (first loud, second quiet); none once cleared.
    assert pauses == [False, True]


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


# --- event hold -----------------------------------------------------------

def test_is_unpause_event():
    unpause = {"event": "property-change", "name": "pause", "data": False}
    assert call_guard._is_unpause_event(unpause) is True
    # A pause (data True) is not an un-pause — must not trigger a re-pause loop.
    assert call_guard._is_unpause_event(
        {"event": "property-change", "name": "pause", "data": True}) is False
    # Other properties / events / shapes are ignored.
    assert call_guard._is_unpause_event(
        {"event": "property-change", "name": "mute", "data": False}) is False
    assert call_guard._is_unpause_event(
        {"event": "start-file"}) is False
    assert call_guard._is_unpause_event("nonsense") is False


def test_hold_worker_repauses_on_unpause(tmp_path):
    # Stand up a fake mpv: accept a connection, expect an observe_property
    # subscription, push an un-pause event, and assert the worker re-pauses.
    sock_path = str(tmp_path / "sink-speech.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    srv.settimeout(5.0)

    received: list = []
    ready = threading.Event()

    def _fake_mpv():
        conn, _ = srv.accept()
        conn.settimeout(5.0)
        buf = b""
        # First line should be the observe_property subscription.
        while b"\n" not in buf:
            buf += conn.recv(4096)
        first, buf = buf.split(b"\n", 1)
        received.append(json.loads(first.decode()))
        # Simulate a mid-call reply un-pausing the broker.
        conn.sendall((json.dumps(
            {"event": "property-change", "name": "pause", "data": False})
            + "\n").encode())
        # The worker should now send set_property pause True back.
        while b"\n" not in buf:
            buf += conn.recv(4096)
        second, _ = buf.split(b"\n", 1)
        received.append(json.loads(second.decode()))
        ready.set()
        conn.close()

    server = threading.Thread(target=_fake_mpv, daemon=True)
    server.start()

    stop = threading.Event()
    worker = threading.Thread(
        target=call_guard._hold_worker, args=(sock_path, stop), daemon=True)
    worker.start()
    try:
        assert ready.wait(5.0), "worker did not re-pause in time"
    finally:
        stop.set()
        worker.join(timeout=3.0)
        srv.close()

    assert received[0]["command"] == ["observe_property", 1, "pause"]
    assert received[1]["command"] == ["set_property", "pause", True]


def test_pause_hold_start_stop_is_idempotent(tmp_path):
    # No real broker here; workers just fail to connect and back off. start()/
    # stop() must be safe to call repeatedly and must join cleanly.
    hold = call_guard.PauseHold([str(tmp_path / "absent.sock")])
    assert hold.active is False
    hold.start()
    assert hold.active is True
    hold.start()  # second start is a no-op
    hold.stop()
    assert hold.active is False
    hold.stop()  # second stop is a no-op
