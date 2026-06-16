"""Skip a highlight turn while the user is actively typing.

tmux has no last-*input* timestamp, so `_pane_recent_keystrokes` uses
`#{window_activity}` (last output epoch) as a keystroke proxy: during the TTS
window the speaking agent is idle, so fresh output is the user typing. The skip
window is `MEDIA_HIGHLIGHT_KEYSTROKE_S` (default 5s, 0 disables). Fails open on
any tmux error so highlighting still happens when we can't tell.
"""

from agent_media_core.intake import submit as S


def _patch_activity(monkeypatch, *, epoch, now, rc=0, stdout=None):
    def fake_run(cmd, **kw):
        class _R:
            returncode = rc
            # stdout overrides epoch (e.g. to inject junk / empty output)
        _R.stdout = stdout if stdout is not None else f"{epoch}\n"
        return _R()
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    monkeypatch.setattr(S.time, "time", lambda: now)


def test_recent_activity_within_window_is_keystroke(monkeypatch):
    _patch_activity(monkeypatch, epoch=1000, now=1003)   # 3s ago
    assert S._pane_recent_keystrokes("%5", 5) is True


def test_old_activity_outside_window_is_not(monkeypatch):
    _patch_activity(monkeypatch, epoch=1000, now=1007)   # 7s ago
    assert S._pane_recent_keystrokes("%5", 5) is False


def test_zero_window_disables_skip(monkeypatch):
    # Even with activity right now, a 0 window means never skip.
    _patch_activity(monkeypatch, epoch=1000, now=1000)
    assert S._pane_recent_keystrokes("%5", 0) is False


def test_tmux_error_fails_open(monkeypatch):
    _patch_activity(monkeypatch, epoch=0, now=1000, rc=1)
    assert S._pane_recent_keystrokes("%5", 5) is False


def test_non_numeric_activity_fails_open(monkeypatch):
    _patch_activity(monkeypatch, epoch=0, now=1000, stdout="\n")
    assert S._pane_recent_keystrokes("%5", 5) is False
