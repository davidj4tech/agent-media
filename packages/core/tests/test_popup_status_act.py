"""Tests for `popup-status --act` fusion.

The speech popup fuses a keypress's action and its redraw into ONE `media`
spawn: `popup-status --act VERB …` runs VERB in-process, prepends its stdout
as a leading line, then emits the usual three status fields. Without --act the
output is unchanged (backward compatible with the status bar / timed refresh).
"""

import argparse

import pytest

from agent_media_core import cli


@pytest.fixture(autouse=True)
def stub_status(monkeypatch):
    # Make the three status fields deterministic and side-effect free.
    monkeypatch.setattr(cli, "_speech_display_state",
                        lambda: (True, None, None, False, False, None, False))
    monkeypatch.setattr(cli, "render_status", lambda **k: "STATUS")
    monkeypatch.setattr(cli, "_subject_label", lambda: ("", "the pane"))

    class FakeStore:
        def list_mutes(self):
            return {"panes": {}, "sessions": {}}
    monkeypatch.setattr(cli, "StateStore", FakeStore)


def _run(act=None):
    a = argparse.Namespace(width=12, show_idle=True, no_bar=True, act=act)
    return cli.cmd_popup_status(a)


def test_no_act_emits_three_lines(capsys):
    assert _run(None) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["STATUS", "the pane", ""]


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
    assert out == ["spoken text here", "STATUS", "the pane", ""]


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
    assert out[1:] == ["STATUS", "the pane", ""]


def test_act_error_does_not_blank_the_redraw(monkeypatch, capsys):
    # An action that raises must not eat the status lines — the popup still
    # needs its redraw.
    def boom(a):
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "cmd_now", boom)

    assert _run(["now"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["", "STATUS", "the pane", ""]      # empty action line, then status


def test_act_bad_verb_does_not_crash(capsys):
    # A malformed action (parse failure -> SystemExit) is swallowed; redraw stays.
    assert _run(["definitely-not-a-verb"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-3:] == ["STATUS", "the pane", ""]
