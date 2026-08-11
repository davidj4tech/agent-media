"""Tests for the phone call guard (call_guard) — call detection + edge logic."""

import json
import os
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


def _percycle_env(monkeypatch):
    # Make one loop iteration == one notification check + immediate flag
    # engage/release, so the orchestration tests can reason per-cycle.
    monkeypatch.setenv("MEDIA_CALL_GUARD_POLL_S", "1")
    monkeypatch.setenv("MEDIA_CALL_GUARD_FLAG_POLL_S", "1")
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_ENGAGE_S", "0")
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_RELEASE_S", "0")


def test_run_loop_reasserts_pause_each_active_cycle(monkeypatch):
    # A reply that starts mid-call clears its own pause, so the guard must
    # re-pause on every cycle the call is active — not just once on the ring.
    _percycle_env(monkeypatch)
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
    _percycle_env(monkeypatch)
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


def test_debounce_config_defaults(monkeypatch):
    for v in ("MEDIA_CALL_GUARD_FLAG_POLL_S", "MEDIA_CALL_GUARD_HOLD_ENGAGE_S",
              "MEDIA_CALL_GUARD_HOLD_RELEASE_S"):
        monkeypatch.delenv(v, raising=False)
    cfg = call_guard.Config()
    assert cfg.flag_poll_s == 0.3
    assert cfg.hold_engage_s == 1.5
    assert cfg.hold_release_s == 2.0


def test_flaghold_ignores_short_flag_engages_on_sustained():
    fh = call_guard.FlagHold(engage_s=1.5, release_s=2.0)
    # A short blip (< engage) never engages — this is the "short utterance" case.
    assert fh.update(True, 0.0) is False
    assert fh.update(True, 1.0) is False
    assert fh.update(False, 1.2) is False
    # Sustained presence engages exactly at engage_s.
    assert fh.update(True, 10.0) is False
    assert fh.update(True, 11.4) is False   # 1.4s in — not yet
    assert fh.update(True, 11.5) is True    # 1.5s — engaged


def test_flaghold_release_grace_bridges_blips():
    fh = call_guard.FlagHold(engage_s=1.0, release_s=2.0)
    assert fh.update(True, 0.0) is False
    assert fh.update(True, 1.0) is True     # engaged
    # A gap shorter than release_s keeps it held (flag flicking between words).
    assert fh.update(False, 1.5) is True
    assert fh.update(False, 3.4) is True    # absent 1.9s < 2.0
    assert fh.update(True, 3.5) is True     # back before release — still held
    # A sustained gap finally releases.
    assert fh.update(False, 10.0) is True
    assert fh.update(False, 12.0) is False  # absent 2.0s — released


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


# --- hold-flag advertisement -------------------------------------------------
#
# The failure these guard against is silent in the direction that matters: a
# trigger writes a flag nothing polls, prints "hold flag set", and ducks
# nothing. Seen for real on `ssh phone media-call-guard --hold`, where the
# phone's MEDIA_CALL_GUARD_HOLD_FLAG lives in an env file only its service
# manager sources.

def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_CALL_GUARD_HOLD_FLAG", raising=False)


def test_guard_publishes_the_flag_path_it_polls(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", "/shared/call-guard.hold")
    call_guard.publish_flag_path(call_guard.Config())
    assert call_guard.advertised_flag_path() == "/shared/call-guard.hold"
    # The advert itself must sit at the DEFAULT location — a trigger that knew
    # where to look for it would not have needed it.
    assert call_guard.advert_path().parent == tmp_path / "agent-media"


def test_stop_removes_the_advert(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    call_guard.publish_flag_path(call_guard.Config())
    call_guard.unpublish_flag_path()
    assert call_guard.advertised_flag_path() is None
    call_guard.unpublish_flag_path()            # idempotent


def test_trigger_follows_a_running_guard(monkeypatch, tmp_path):
    """The fix: a trigger that cannot see the guard's env still reaches it."""
    _state(monkeypatch, tmp_path)
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", "/shared/call-guard.hold")
    call_guard.publish_flag_path(call_guard.Config())
    monkeypatch.delenv("MEDIA_CALL_GUARD_HOLD_FLAG")   # as a bare ssh sees it
    cfg = call_guard.Config()
    assert cfg.hold_flag.endswith("agent-media/call-guard.hold")   # the default
    assert call_guard.resolve_trigger_flag(cfg) == "/shared/call-guard.hold"


def test_explicit_beats_the_advert_but_warns(monkeypatch, tmp_path, capsys):
    """Overriding is how you drive a guard that isn't up yet — but say so."""
    _state(monkeypatch, tmp_path)
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", "/shared/call-guard.hold")
    call_guard.publish_flag_path(call_guard.Config())
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", "/elsewhere/call-guard.hold")
    cfg = call_guard.Config()
    assert call_guard.resolve_trigger_flag(cfg) == "/elsewhere/call-guard.hold"
    assert "will not reach it" in capsys.readouterr().err


def test_no_advert_leaves_resolution_alone(monkeypatch, tmp_path):
    """No guard here, or an older one: behave exactly as before."""
    _state(monkeypatch, tmp_path)
    cfg = call_guard.Config()
    assert call_guard.resolve_trigger_flag(cfg) == cfg.hold_flag


def test_agreeing_advert_is_silent(monkeypatch, tmp_path, capsys):
    _state(monkeypatch, tmp_path)
    cfg = call_guard.Config()
    call_guard.publish_flag_path(cfg)
    assert call_guard.resolve_trigger_flag(cfg) == cfg.hold_flag
    assert capsys.readouterr().err == ""


# --- self-expiring hold ------------------------------------------------------
#
# A trigger that fires --hold and then dies leaves music quiet indefinitely.
# Cece asked for this directly: she has no automatic hook for "conversation
# ended", so a chat closing abruptly must not strand the duck.
#
# These AGE THE FLAG on disk (os.utime) rather than patching the guard's clock.
# The first version patched _now(), passed, and shipped a hold that could never
# expire: _now() is time.monotonic() and st_mtime is epoch, so the subtraction
# was nonsense. Patching the clock made the test agree with the bug. Touching
# the real file is the only version that would have failed.


def _age(path, seconds):
    """Backdate the flag's mtime, as if it were written `seconds` ago."""
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime - seconds))


def test_hold_without_ttl_outlasts_any_real_hold(monkeypatch, tmp_path):
    """Dictation lasts as long as it lasts, and must not be cut off. Ten
    minutes is already far longer than anyone dictates or any spoken turn."""
    flag = tmp_path / "call-guard.hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    call_guard._set_hold(cfg)
    assert call_guard._flag_ttl(cfg.hold_flag) is None   # no ttl of its own
    _age(flag, 600)
    assert call_guard.flag_present(cfg) is True


def test_an_empty_flag_from_the_bridge_outlasts_any_real_hold(monkeypatch, tmp_path):
    """The Automate mic-detect bridge writes the flag itself, with no contents
    at all — it never goes through _set_hold, so the guarantee has to hold for
    a body this code did not write."""
    flag = tmp_path / "call-guard.hold"
    flag.write_text("")
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    _age(flag, 600)
    assert call_guard.flag_present(cfg) is True


# --- the release that never arrives ----------------------------------------

def test_a_hold_nobody_released_is_eventually_released(monkeypatch, tmp_path):
    """The silent failure this backstop exists for: a --hold reaches the phone,
    the ssh call carrying --release dies, and the flag stays. Music stays
    ducked and speech stays paused for as long as the phone runs, with every
    service up and every health check reporting well. Nothing else in the
    system notices, because a hold looks exactly like a hold."""
    flag = tmp_path / "call-guard.hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    call_guard._set_hold(cfg, source="cece")           # no ttl, as callers do

    _age(flag, 1_500)
    assert call_guard.flag_present(cfg) is True        # still inside the backstop

    _age(flag, 400)                                    # now 1900s, past 1800
    assert call_guard.flag_present(cfg) is False
    # Deleted, so the ordinary release path runs and audio comes back on its
    # own — the point is that nobody has to notice.
    assert not flag.exists()


def test_the_backstop_is_tunable_and_can_be_turned_off(monkeypatch, tmp_path):
    flag = tmp_path / "call-guard.hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_MAX_S", "60")
    call_guard._set_hold(call_guard.Config())
    _age(flag, 61)
    assert call_guard.flag_present(call_guard.Config()) is False

    flag.write_text("")
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_MAX_S", "0")   # as it used to be
    _age(flag, 86_400)
    assert call_guard.flag_present(call_guard.Config()) is True


def test_an_explicit_ttl_still_wins_over_the_backstop(monkeypatch, tmp_path):
    """A caller that passes --ttl has said what it means; the backstop is for
    the callers that say nothing, and must not shorten or extend a stated one."""
    flag = tmp_path / "call-guard.hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_MAX_S", "1800")
    cfg = call_guard.Config()
    call_guard._set_hold(cfg, ttl=120)
    _age(flag, 200)                                    # past its ttl, inside the backstop
    assert call_guard.flag_present(cfg) is False

    call_guard._set_hold(cfg, ttl=7200)                # longer than the backstop
    _age(flag, 3_600)
    assert call_guard.flag_present(cfg) is True


def test_a_heartbeat_keeps_a_long_hold_alive(monkeypatch, tmp_path):
    """A caller that genuinely needs longer than the backstop re-holds, exactly
    as the TTL path already documents. This must not become a reason to make
    the backstop long enough to be useless."""
    flag = tmp_path / "call-guard.hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    call_guard._set_hold(cfg)
    _age(flag, 1_700)
    call_guard._set_hold(cfg)                          # still going
    _age(flag, 1_700)
    assert call_guard.flag_present(cfg) is True


def test_hold_with_ttl_expires_and_clears_the_flag(monkeypatch, tmp_path):
    flag = tmp_path / "call-guard.hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    call_guard._set_hold(cfg, ttl=120)
    assert "ttl=120" in flag.read_text()

    _age(flag, 60)
    assert call_guard.flag_present(cfg) is True          # still inside the window

    _age(flag, 61)                                       # now 121s old
    assert call_guard.flag_present(cfg) is False
    # Deleted, not merely ignored: the ordinary release path then runs and
    # auto-resume happens without anyone noticing the trigger died.
    assert not flag.exists()


def test_heartbeat_extends_the_hold(monkeypatch, tmp_path):
    """Re-running --hold refreshes mtime, so a caller who is still talking
    extends the hold rather than stacking a second one."""
    flag = tmp_path / "call-guard.hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    call_guard._set_hold(cfg, ttl=60)

    _age(flag, 59)                                       # nearly expired
    call_guard._set_hold(cfg, ttl=60)                    # heartbeat
    _age(flag, 30)
    assert call_guard.flag_present(cfg) is True
    assert flag.exists()


def test_unreadable_ttl_is_treated_as_no_ttl(monkeypatch, tmp_path):
    """Garbage in the flag must not expire a hold early — failing open keeps
    the duck, which is recoverable by hand; failing closed un-ducks someone
    mid-conversation, which is not. It gets the same backstop as any hold that
    named no TTL, which is what "treated as no ttl" now means."""
    flag = tmp_path / "call-guard.hold"
    flag.write_text("ttl=banana\n")
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    _age(flag, 600)
    assert call_guard.flag_present(cfg) is True
