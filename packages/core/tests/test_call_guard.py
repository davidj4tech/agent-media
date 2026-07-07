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


# --- external hold flag ----------------------------------------------------

def test_hold_flag_default_under_state_dir(monkeypatch):
    monkeypatch.delenv("MEDIA_CALL_GUARD_HOLD_FLAG", raising=False)
    cfg = call_guard.Config()
    assert cfg.hold_flag.endswith("/call-guard.hold")


def test_set_and_clear_hold(monkeypatch, tmp_path):
    flag = tmp_path / "sub" / "hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    assert call_guard.flag_present(cfg) is False
    call_guard._set_hold(cfg)            # creates parent dir + file
    assert call_guard.flag_present(cfg) is True
    call_guard._clear_hold(cfg)
    assert call_guard.flag_present(cfg) is False
    call_guard._clear_hold(cfg)          # idempotent when already gone


def test_resume_sockets_skips_absent_and_dry_run(monkeypatch, tmp_path):
    present = tmp_path / "sink-speech.sock"
    present.write_bytes(b"")
    absent = tmp_path / "mpv-music.sock"
    monkeypatch.setenv("MEDIA_CALL_GUARD_SOCKETS", f"{present},{absent}")
    cfg = call_guard.Config()
    resumed = []
    monkeypatch.setattr(call_guard.ipc, "set_property",
                        lambda s, n, v: resumed.append((str(s), n, v)))
    call_guard.resume_sockets(cfg)
    assert resumed == [(str(present), "pause", False)]
    resumed.clear()
    call_guard.resume_sockets(cfg, dry_run=True)
    assert resumed == []


def _drive_loop(monkeypatch, flag_states, call_states=None):
    """Run _run_loop over the given per-cycle (flag, call) states, capturing the
    pause/resume/hold side effects as an event list."""
    cfg = call_guard.Config()
    flags = iter(flag_states)
    calls = iter(call_states if call_states is not None
                 else [False] * len(flag_states))

    def _flag(_c):
        try:
            return next(flags)
        except StopIteration:
            call_guard._stop = True
            return False
    # list_notifications returns [] (no call) or [telecom] depending on calls[]
    def _notifs(_c):
        try:
            return [_telecom()] if next(calls) else []
        except StopIteration:
            return []
    monkeypatch.setattr(call_guard, "flag_present", _flag)
    monkeypatch.setattr(call_guard, "list_notifications", _notifs)
    monkeypatch.setattr(call_guard.time, "sleep", lambda _s: None)
    events = []
    monkeypatch.setattr(call_guard, "pause_sockets",
                        lambda cfg, dry_run=False, quiet=False: events.append("pause"))
    monkeypatch.setattr(call_guard, "resume_sockets",
                        lambda cfg, dry_run=False: events.append("resume"))
    monkeypatch.setattr(call_guard, "duck_sockets",
                        lambda cfg, saved, dry_run=False, quiet=False: events.append("duck"))
    monkeypatch.setattr(call_guard, "unduck_sockets",
                        lambda cfg, saved, dry_run=False: events.append("unduck"))
    monkeypatch.setattr(call_guard.PauseHold, "start",
                        lambda self: events.append("hold_start"))
    monkeypatch.setattr(call_guard.PauseHold, "stop",
                        lambda self: events.append("hold_stop"))
    call_guard._stop = False
    try:
        call_guard._run_loop(cfg)
    finally:
        call_guard._stop = False
    return events


def test_external_flag_pauses_then_auto_resumes(monkeypatch):
    # flag absent, present, present, absent -> pause+duck on rising edge,
    # unduck+resume on fall.
    events = _drive_loop(monkeypatch, [False, True, True, False])
    assert "hold_start" in events
    assert "pause" in events and "duck" in events
    assert "resume" in events and "unduck" in events
    # resume happens only after the hold is released
    assert events.index("hold_stop") < events.index("resume")


def test_external_flag_no_resume_if_call_overlapped(monkeypatch):
    # cycle1: flag+call, cycle2: flag only, cycle3: neither.
    # A call participated, so the fall of the flag must NOT auto-resume speech —
    # but the music volume is still restored (un-ducking is always safe).
    events = _drive_loop(monkeypatch,
                         flag_states=[True, True, False],
                         call_states=[True, False, False])
    assert "pause" in events and "duck" in events
    assert "hold_stop" in events
    assert "unduck" in events       # music volume restored even after a call
    assert "resume" not in events   # speech stays paused


def test_music_ducked_not_paused_by_default(monkeypatch):
    monkeypatch.delenv("MEDIA_CALL_GUARD_SOCKETS", raising=False)
    monkeypatch.delenv("MEDIA_CALL_GUARD_DUCK_SOCKETS", raising=False)
    cfg = call_guard.Config()
    assert any(s.endswith("mpv-music.sock") for s in cfg.duck_list)
    assert all(not s.endswith("mpv-music.sock") for s in cfg.pause_list)
    assert any(s.endswith("sink-speech.sock") for s in cfg.pause_list)


def test_duck_disabled_via_empty_env(monkeypatch):
    monkeypatch.setenv("MEDIA_CALL_GUARD_DUCK_SOCKETS", "")
    cfg = call_guard.Config()
    assert cfg.duck_list == []
    # with nothing ducked, music falls back into the paused set
    assert any(s.endswith("mpv-music.sock") for s in cfg.pause_list)


def test_duck_saves_and_unduck_restores_volume(monkeypatch, tmp_path):
    music = tmp_path / "mpv-music.sock"
    music.write_bytes(b"")
    monkeypatch.setenv("MEDIA_CALL_GUARD_SOCKETS", str(music))
    monkeypatch.setenv("MEDIA_CALL_GUARD_DUCK_SOCKETS", str(music))
    monkeypatch.setenv("MEDIA_CALL_GUARD_DUCK_VOLUME", "15")
    cfg = call_guard.Config()
    props = {"volume": 70.0}
    monkeypatch.setattr(call_guard.ipc, "get_property",
                        lambda s, n, **k: props[n])
    monkeypatch.setattr(call_guard.ipc, "set_property",
                        lambda s, n, v: props.__setitem__(n, v))
    saved = {}
    call_guard.duck_sockets(cfg, saved)
    assert props["volume"] == 15            # ducked
    assert saved[str(music)] == 70.0        # original remembered
    call_guard.duck_sockets(cfg, saved)     # re-assert: keeps duck, no re-save
    assert props["volume"] == 15 and saved[str(music)] == 70.0
    call_guard.unduck_sockets(cfg, saved)
    assert props["volume"] == 70.0          # restored
    assert saved == {}
