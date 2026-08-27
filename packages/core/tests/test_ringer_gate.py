"""The silent ringer holds alerts — and, far more often, does not.

Two failures live here and they are not symmetric. Speaking into a silenced
phone is the complaint that started this: annoying, obvious, self-reporting.
Withholding an alert that should have been spoken is worse — silent by
construction, indistinguishable from a broken TTS stack, and this project has
lost whole afternoons to that exact shape (a reverted mic block, a media stream
at 0/25, every part of the pipeline reporting healthy).

So the bulk of what follows is the fail-open half: every way the answer can go
missing must end in speech.
"""

import json
import time

import pytest

from agent_media_core import ringer
from agent_media_core.intake import submit
from agent_media_core.sinks import speech
from agent_media_core.types import Event, Source, Target


PHONE = Target(name="phone")


def _alert(**meta) -> Event:
    return Event(text="the agenda", source=Source.CLI, target=PHONE,
                 metadata={"alert": True, **meta})


def _quiet(age_s: float = 0.0) -> dict:
    return {"quiet": True, "mode": "silent", "dnd": "unknown",
            "granted": "0", "answered": True,
            "checked_at": time.time() - age_s}


# --- the reading itself ----------------------------------------------------

@pytest.mark.parametrize("line,quiet", [
    ("silent dnd=all granted=1", True),
    ("vibrate dnd=all granted=1", True),
    ("normal dnd=all granted=1", False),
    # The DND half, and the reason it exists: ringer mode stays `normal` right
    # through Do Not Disturb on modern Android.
    ("normal dnd=priority granted=1", True),
    ("normal dnd=none granted=1", True),
    # Ungranted is "we were not allowed to look", never "not in DND".
    ("normal dnd=none granted=0", False),
    ("normal dnd=unknown granted=1", False),
    # The switch still decides alone — an install without the grant keeps the
    # half of the feature that needs no permission.
    ("silent dnd=unknown granted=0", True),
])
def test_parses_and_decides(line, quiet):
    assert ringer.is_quiet(ringer.parse(line)) is quiet


def test_a_non_answer_is_not_an_answer():
    # An error page, an empty body, a key=value where the mode should be. None
    # of these is a phone saying anything, and none may read as quiet.
    for body in ("", "   ", "<html>404</html>\n", "dnd=none granted=1"):
        reading = ringer.parse(body)
        assert ringer.is_quiet(reading) is False, body
    assert ringer.parse("") is None


def test_unknown_fields_are_ignored_not_fatal():
    # The APK and the Python package update on entirely different days. A field
    # one end has never heard of must not cost the other the whole reading.
    reading = ringer.parse("silent dnd=priority granted=1 zen=deep bells=3")
    assert reading["mode"] == "silent"
    assert ringer.is_quiet(reading) is True


# --- the gate --------------------------------------------------------------

def test_holds_an_alert_when_the_phone_is_quiet(monkeypatch):
    monkeypatch.setattr(speech, "read_ringer", lambda *a, **k: _quiet())
    assert submit._ringer_hold(PHONE, _alert()) is not None


def test_lets_an_unmarked_reply_through(monkeypatch):
    """The whole scope decision, in one test.

    A reply asked for mid-conversation is not an alert and is never gated —
    including on a phone that is very definitely on silent. Note the sentinel:
    an unmarked event must not even *ask* the broker, so the ordinary reply
    never pays the bridge round-trip.
    """
    def _never(*a, **k):
        raise AssertionError("an unmarked reply must not consult the ringer")

    monkeypatch.setattr(speech, "read_ringer", _never)
    plain = Event(text="hello", source=Source.CLAUDE_CODE, target=PHONE)
    assert submit._ringer_hold(PHONE, plain) is None


def test_leaves_other_targets_alone(monkeypatch):
    """The phone's ringer says nothing about the lounge speakers.

    Without this, "silence my phone" quietly becomes "silence the house" — and
    that is a bug nobody would attribute to a ringer for a long time.
    """
    def _never(*a, **k):
        raise AssertionError("the local sink must not consult the phone")

    monkeypatch.setattr(speech, "read_ringer", _never)
    local = Event(text="x", source=Source.CLI, target=Target(name="local"),
                  metadata={"alert": True})
    assert submit._ringer_hold(Target(name="local"), local) is None


@pytest.mark.parametrize("verdict", [
    None,                                            # nothing published
    {"quiet": False, "mode": "normal", "checked_at": time.time()},
    {},                                              # a broker with no answer
])
def test_speaks_whenever_the_answer_is_not_a_clear_yes(monkeypatch, verdict):
    monkeypatch.setattr(speech, "read_ringer", lambda *a, **k: verdict)
    assert submit._ringer_hold(PHONE, _alert()) is None


def test_a_broker_that_raises_still_speaks(monkeypatch):
    # read_ringer swallows its own IPC errors, but a target with no socket at
    # all, a bridge mid-reconnect, or an mpv too old for user-data must not
    # take the alert down with them.
    def _boom(*a, **k):
        raise OSError("no such socket")

    monkeypatch.setattr(speech, "read_ringer", _boom)
    with pytest.raises(OSError):
        submit._ringer_hold(PHONE, _alert())
    # ^ documents that the sink, not the gate, owns this. The sink's own test
    #   below is what guarantees callers never see it.


# --- the sink's read: every unknown is None --------------------------------

def _fake_ipc(monkeypatch, value=None, raises=None):
    from agent_media_core.sinks import _mpv_ipc as ipc

    def _get(sock, name, timeout=2.0, critical=False):
        if raises is not None:
            raise raises
        return value

    monkeypatch.setattr(ipc, "get_property", _get)


def test_read_ringer_ages_out_a_stale_verdict(monkeypatch):
    """A verdict from a dead publisher is not evidence about a live phone.

    This is the one that would otherwise fail silently for as long as it took
    someone to notice their alerts had stopped: the phone comes off silent, the
    ringer service is dead, and the last thing it ever said keeps holding
    everything back.
    """
    _fake_ipc(monkeypatch, _quiet(age_s=speech.RINGER_MAX_AGE_S + 60))
    assert speech.read_ringer(PHONE) is None
    _fake_ipc(monkeypatch, _quiet(age_s=5))
    assert speech.read_ringer(PHONE) is not None


@pytest.mark.parametrize("value", [
    None,                       # user-data unset
    "quiet",                    # not a snapshot
    {"quiet": True},            # no checked_at to age
    {"quiet": True, "checked_at": "recently"},
])
def test_read_ringer_returns_none_for_anything_it_cannot_trust(monkeypatch, value):
    _fake_ipc(monkeypatch, value)
    assert speech.read_ringer(PHONE) is None


def test_read_ringer_swallows_a_dead_broker(monkeypatch):
    from agent_media_core.sinks import _mpv_ipc as ipc

    _fake_ipc(monkeypatch, raises=ipc.MpvIpcError("connection refused"))
    assert speech.read_ringer(PHONE) is None
    _fake_ipc(monkeypatch, raises=OSError("no such file"))
    assert speech.read_ringer(PHONE) is None


# --- what a held alert leaves behind ---------------------------------------

def test_a_held_alert_is_still_written_down(tmp_path, monkeypatch):
    """Held, not lost. The words are the only part still worth having by
    morning, and a row nobody can find is the same as no row."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from agent_media_core.state import StateStore

    store = StateStore(tmp_path / "state.db")
    hid = submit._record_silenced(store, _alert(), PHONE, "the agenda",
                                  {"mode": "silent", "dnd": "priority"})
    assert hid is not None
    row = store.recent_history(limit=1)[0]
    assert row["text"] == "the agenda"
    assert (row.get("extras") or {}).get("silenced") == "ringer"
    # And the breadcrumb `media doctor` counts, without which this is
    # indistinguishable from the TTS stack being broken.
    errs = store.recent_errors(component="intake", limit=5)
    assert any((e.get("extras") or {}).get("kind") == "alert-silenced"
               for e in errs)


# --- the publisher ---------------------------------------------------------

def test_snapshot_records_the_unanswered_case(monkeypatch):
    snap = ringer.snapshot(None)
    assert snap["quiet"] is False        # never quiet on no answer
    assert snap["answered"] is False
    assert snap["checked_at"] > 0


def test_publish_survives_a_broker_that_is_down(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(speech, "set_ringer",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(ringer, "read", lambda *a, **k: {"mode": "silent"})
    snap = ringer.tick()
    # The file still landed even though the broker did not.
    assert json.loads(ringer.state_path().read_text())["quiet"] is True
    assert snap["quiet"] is True
