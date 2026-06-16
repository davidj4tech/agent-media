"""CLI surface for durable per-pane / per-session mute (mute-pane, mute-status)."""

import pytest

from agent_media_core import cli
from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TTS_POPUP_PANE", raising=False)
    # Deterministic tmux: pretend %1/%2 are live, %9 is dead; sessions resolve.
    monkeypatch.setattr(cli, "_live_panes", lambda: ["%1", "%2"])
    monkeypatch.setattr(cli, "_spoken_pane", lambda: "%2")
    monkeypatch.setattr(cli, "_tmux_session_for_pane",
                        lambda pane: "work:" if pane else "")
    monkeypatch.setattr(S, "_tmux_session_for_pane",
                        lambda pane: "work:" if pane else "")
    return StateStore()


def test_explicit_pane_on_off(env, capsys):
    assert cli.main(["mute-pane", "--pane", "%1", "on"]) == 0
    assert env.get_mute("pane", "%1") is True
    assert cli.main(["mute-pane", "--pane", "%1", "off"]) == 0
    assert env.get_mute("pane", "%1") is False


def test_toggle_flips_effective_state(env, capsys):
    # First toggle on an unset pane → muted.
    cli.main(["mute-pane", "--pane", "%1", "toggle"])
    assert env.resolve_mute("%1", "work:") is True
    # Toggle again → unmuted.
    cli.main(["mute-pane", "--pane", "%1", "toggle"])
    assert env.resolve_mute("%1", "work:") is False


def test_toggle_in_muted_session_writes_explicit_pane_unmute(env):
    env.set_mute("session", "work:", True)
    # Pane is effectively muted via session; toggling flips to audible.
    cli.main(["mute-pane", "--pane", "%1", "toggle"])
    assert env.get_mute("pane", "%1") is False          # explicit unmute
    assert env.resolve_mute("%1", "work:") is False


def test_current_targets_speaking_pane(env):
    cli.main(["mute-pane", "--current", "on"])
    assert env.get_mute("pane", "%2") is True            # _spoken_pane() → %2


def test_session_scope(env):
    assert cli.main(["mute-pane", "--pane", "%1", "--session", "on"]) == 0
    assert env.get_mute("session", "work:") is True
    assert env.get_mute("pane", "%1") is None            # session row, not pane


def test_no_target_pane_errors(env, capsys, monkeypatch):
    # No --pane, no TMUX_PANE, and nothing speaking.
    monkeypatch.setattr(cli, "_spoken_pane", lambda: "")
    rc = cli.main(["mute-pane", "on"])
    assert rc == 1
    assert "no target pane" in capsys.readouterr().err


def test_mute_stops_in_flight_clip(env, monkeypatch, capsys):
    env.set_now_playing("speech", uri="/x.mp3", started_at=1.0,
                        extras={"source_pane": "%1", "source_tmux_session": "work:"})
    stops = []
    monkeypatch.setattr(cli, "SinkSpeech",
                        lambda: type("S", (), {"stop": lambda self, t: stops.append(t)})())
    cli.main(["mute-pane", "--pane", "%1", "on"])
    assert stops                                   # current clip stopped
    assert "(stopped current)" in capsys.readouterr().out
    # Unmuting never stops anything.
    stops.clear()
    cli.main(["mute-pane", "--pane", "%1", "off"])
    assert not stops


def test_subject_idle_is_caller_pane(env, monkeypatch):
    monkeypatch.setenv("TTS_POPUP_PANE", "%2")
    assert cli._subject() == ("%2", "work:", False)   # idle → caller, not following


def test_subject_follows_now_playing(env, monkeypatch):
    monkeypatch.setenv("TTS_POPUP_PANE", "%2")
    env.set_now_playing("speech", uri="/x", started_at=1.0,
                        extras={"source_pane": "%30", "source_tmux_session": "ts"})
    assert cli._subject() == ("%30", "ts", True)       # other pane → following
    env.set_now_playing("speech", uri="/x", started_at=1.0,
                        extras={"source_pane": "%2", "source_tmux_session": "ts"})
    assert cli._subject() == ("%2", "ts", False)       # your pane playing → not following


def test_pane_muted_reflects_subject(env, monkeypatch, capsys):
    monkeypatch.setenv("TTS_POPUP_PANE", "%2")         # idle → subject = caller %2
    cli.main(["pane-muted"])
    assert capsys.readouterr().out == ""
    env.set_mute("pane", "%2", True)
    cli.main(["pane-muted"])
    assert capsys.readouterr().out.strip() == "1"
    # An explicit --pane still overrides the subject.
    cli.main(["pane-muted", "--pane", "%1"])           # %1 unmuted
    assert capsys.readouterr().out == ""


def test_pane_muted_follows_now_playing_not_caller(env, monkeypatch, capsys):
    # Subject is the now-playing pane (%30), even though the popup was opened
    # from %2 — the glyph must track what M will act on.
    monkeypatch.setenv("TTS_POPUP_PANE", "%2")
    env.set_now_playing("speech", uri="/x", started_at=1.0,
                        extras={"source_pane": "%30", "source_tmux_session": "ts"})
    env.set_mute("pane", "%2", True)                   # caller muted, subject not
    cli.main(["pane-muted"])
    assert capsys.readouterr().out == ""               # subject %30 is unmuted
    env.set_mute("pane", "%30", True)
    cli.main(["pane-muted"])
    assert capsys.readouterr().out.strip() == "1"


def test_mute_pane_subject_targets_now_playing(env, monkeypatch):
    monkeypatch.setenv("TTS_POPUP_PANE", "%2")
    env.set_now_playing("speech", uri="/x", started_at=1.0,
                        extras={"source_pane": "%30", "source_tmux_session": "ts"})
    cli.main(["mute-pane", "--subject", "on"])
    assert env.get_mute("pane", "%30") is True         # the subject, not the caller
    assert env.get_mute("pane", "%2") is None


def test_mute_count(env, capsys):
    cli.main(["mute-count"])
    assert capsys.readouterr().out == ""               # nothing when zero
    env.set_mute("pane", "%1", True)
    env.set_mute("session", "work:", True)
    env.set_mute("pane", "%2", False)                  # explicit-unmute doesn't count
    cli.main(["mute-count"])
    assert capsys.readouterr().out.strip() == "2"


def test_status_prunes_dead_and_tags(env, capsys):
    env.set_mute("pane", "%1", True)     # live
    env.set_mute("pane", "%9", True)     # dead → pruned on status
    env.set_mute("session", "work:", True)
    cli.main(["mute-status"])
    out = capsys.readouterr().out
    assert "pane    %1: muted" in out
    assert "%9" not in out               # pruned
    assert "session work:: muted" in out
    assert env.get_mute("pane", "%9") is None
