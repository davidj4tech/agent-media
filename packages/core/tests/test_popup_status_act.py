"""Tests for `popup-status --act` fusion.

The speech popup fuses a keypress's action and its redraw into ONE `media`
spawn: `popup-status --act VERB …` runs VERB in-process, prepends its stdout
as a leading line, then emits the usual four status fields. Without --act the
output is unchanged (backward compatible with the status bar / timed refresh).
"""

import argparse

import pytest

from agent_media_core import cli


@pytest.fixture(autouse=True)
def stub_status(monkeypatch):
    # Make the status fields deterministic and side-effect free.
    monkeypatch.setattr(cli, "_speech_display_state",
                        lambda: (True, None, None, False, False, None, False))
    monkeypatch.setattr(cli, "render_status", lambda **k: "STATUS")
    monkeypatch.setattr(cli, "_subject_label", lambda: ("", "the pane"))

    class FakeStore:
        speed = None            # the sticky speech rate, class-level so every
                                # instantiation inside cmd_popup_status shares it

        def list_mutes(self):
            return {"panes": {}, "sessions": {}}

        def get_speech_speed(self):
            return FakeStore.speed

        def set_speech_speed(self, rate):
            FakeStore.speed = rate
    monkeypatch.setattr(cli, "StateStore", FakeStore)
    return FakeStore


def _run(act=None):
    a = argparse.Namespace(width=12, show_idle=True, no_bar=True, act=act)
    return cli.cmd_popup_status(a)


def test_no_act_emits_four_lines(capsys):
    assert _run(None) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["STATUS", "the pane", "", ""]


def test_act_prepends_action_output(monkeypatch, capsys):
    # The fused action prints; that stdout becomes the leading line, before the
    # three status fields. Patch cmd_now — _build_parser() (called inside
    # cmd_popup_status) binds the module global at runtime, so the stub wins.
    def fake_now(a):
        print("spoken text here")
        return 0
    monkeypatch.setattr(cli, "cmd_now", fake_now)

    assert _run(["now"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["spoken text here", "STATUS", "the pane", "", ""]


def test_act_output_collapsed_to_one_line(monkeypatch, capsys):
    # A multi-line action stdout must collapse to a single leading line so the
    # popup's fixed 4-line read stays aligned.
    def fake_now(a):
        print("line one\nline two")
        return 0
    monkeypatch.setattr(cli, "cmd_now", fake_now)

    _run(["now"])
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "line one line two"
    assert out[1:] == ["STATUS", "the pane", "", ""]


def test_act_error_does_not_blank_the_redraw(monkeypatch, capsys):
    # An action that raises must not eat the status lines — the popup still
    # needs its redraw.
    def boom(a):
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "cmd_now", boom)

    assert _run(["now"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["", "STATUS", "the pane", "", ""]  # empty action line, then status


def test_speed_field_survives_playback_stopping(stub_status, capsys):
    """The point of the field: a 1.5× set mid-reply still applies to the next
    one, so the popup keeps showing it after the status line has gone idle."""
    stub_status.speed = 1.5
    _run(None)
    assert capsys.readouterr().out.splitlines()[-1] == "1.5"


def test_speed_field_empty_at_normal_rate(stub_status, capsys):
    stub_status.speed = 1.0
    _run(None)
    assert capsys.readouterr().out.splitlines()[-1] == ""


def test_live_speed_refreshes_the_sticky_copy(monkeypatch, stub_status, capsys):
    """A live reading wins — so a broker restarted back to 1.0 self-heals on the
    next clip instead of the popup insisting on a rate that no longer applies."""
    stub_status.speed = 1.5
    monkeypatch.setattr(cli, "_speech_display_state",
                        lambda: (False, 1.0, 10.0, False, False, 1.0, True))
    _run(None)
    assert capsys.readouterr().out.splitlines()[-1] == ""
    assert stub_status.speed in (None, 1.0)


def test_act_bad_verb_does_not_crash(capsys):
    # A malformed action (parse failure -> SystemExit) is swallowed; redraw stays.
    assert _run(["definitely-not-a-verb"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-4:] == ["STATUS", "the pane", "", ""]
