"""The external speech gate (MEDIA_SPEECH_GATE_CMD) — the one hold that fails
CLOSED. On red5 it is David's PHS-treatment rule: nothing spoken aloud during
a treatment unless it goes to his earbuds."""

import os
import stat

from agent_media_core.intake import submit
from agent_media_core.types import Target

PHONE = Target(name="phone")


def _gate(tmp_path, body: str) -> str:
    p = tmp_path / "gate"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_no_gate_speaks(monkeypatch):
    monkeypatch.delenv("MEDIA_SPEECH_GATE_CMD", raising=False)
    assert submit._gate_hold(PHONE, "hello") is False


def test_exit_zero_speaks(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_SPEECH_GATE_CMD", _gate(tmp_path, "exit 0"))
    assert submit._gate_hold(PHONE, "hello") is False


def test_nonzero_holds_and_gets_target_and_text(monkeypatch, tmp_path):
    seen = tmp_path / "seen"
    monkeypatch.setenv("MEDIA_SPEECH_GATE_CMD",
                       _gate(tmp_path, f'echo "$1" > {seen}; cat >> {seen}; exit 1'))
    assert submit._gate_hold(Target(name="rooms"), "health notes") is True
    assert seen.read_text().split("\n")[:2] == ["rooms", "health notes"]


def test_missing_gate_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_SPEECH_GATE_CMD", str(tmp_path / "nope"))
    assert submit._gate_hold(PHONE, "hello") is True


def test_hung_gate_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_SPEECH_GATE_CMD", _gate(tmp_path, "sleep 5"))
    monkeypatch.setenv("MEDIA_SPEECH_GATE_TIMEOUT_S", "0.5")
    assert submit._gate_hold(PHONE, "hello") is True


def test_submit_event_holds_before_rendering(monkeypatch, tmp_path):
    """A held event is written down and never rendered or played."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MEDIA_SPEECH_GATE_CMD", _gate(tmp_path, "cat >/dev/null; exit 1"))
    from agent_media_core.state import StateStore
    from agent_media_core.types import Event, Source

    def _no_render(*a, **k):
        raise AssertionError("rendered while held")

    monkeypatch.setattr(submit, "_engine_and_voice", _no_render)
    monkeypatch.setattr(submit, "_remote_say_cmd", _no_render)
    store = StateStore(tmp_path / "state.db")
    ev = Event(text="client notes", source=Source.CLI, target=PHONE)
    hid = submit.submit_event(ev, state=store, sink=object())
    assert hid is not None
    row = store.recent_history(limit=1)[0]
    assert (row.get("extras") or {}).get("silenced") == "gate"


def test_a_toast_hold_is_not_gated(monkeypatch, tmp_path):
    """--hold events are never played, so the gate must not queue them for a
    spoken release."""
    seen = tmp_path / "seen"
    monkeypatch.setenv("MEDIA_SPEECH_GATE_CMD", _gate(tmp_path, f"touch {seen}; exit 1"))
    from agent_media_core.types import Event, Source

    class Stop(Exception):
        pass

    def _stop(*a, **k):
        raise Stop

    monkeypatch.setattr(submit, "_ringer_hold", _stop)
    ev = Event(text="digest", source=Source.CLI, target=PHONE, metadata={"held": True})
    try:
        submit.submit_event(ev, state=object(), coordinator=object(), sink=object())
    except Stop:
        pass
    assert not seen.exists()
