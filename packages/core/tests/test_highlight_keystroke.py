"""Skip a highlight turn while the user is actively typing — and the overrides.

Detection uses `#{client_activity}` (last *user input* on the pane's session),
NOT window/pane activity: a Claude Code / TUI pane redraws continuously so
output-activity is always ~now and would suppress the highlight every turn.
`client_activity` freezes while the user isn't typing.

Overrides that bypass the keystroke-skip:
- the `highlight-now` force flag (tmux `prefix V`), until the user types again;
- the control popup being open for the pane.

Skip window is `MEDIA_HIGHLIGHT_KEYSTROKE_S` (default 5s, 0 disables). All
checks fail open (highlight) when tmux can't tell.
"""

import pytest

from agent_media_core.intake import submit as S


# --- _last_client_activity (the underlying signal) -------------------------

def _patch_tmux(monkeypatch, *, session="$1", clients="1000\n", sess_rc=0,
                clients_rc=0):
    """Fake the two tmux calls _last_client_activity makes."""
    def fake_run(cmd, **kw):
        class _R:
            returncode = 0
            stdout = ""
        if "display-message" in cmd:
            _R.returncode = sess_rc
            _R.stdout = f"{session}\n"
        elif "list-clients" in cmd:
            _R.returncode = clients_rc
            _R.stdout = clients
        return _R()
    monkeypatch.setattr(S.subprocess, "run", fake_run)


def test_last_client_activity_takes_max(monkeypatch):
    _patch_tmux(monkeypatch, clients="1000\n1005\n1003\n")
    assert S._last_client_activity("%5") == 1005


def test_last_client_activity_none_when_no_clients(monkeypatch):
    _patch_tmux(monkeypatch, clients="\n")
    assert S._last_client_activity("%5") is None


def test_last_client_activity_none_on_tmux_error(monkeypatch):
    _patch_tmux(monkeypatch, clients_rc=1)
    assert S._last_client_activity("%5") is None


# --- _pane_recent_keystrokes ----------------------------------------------

def test_recent_input_within_window(monkeypatch):
    monkeypatch.setattr(S, "_last_client_activity", lambda p: 1000)
    monkeypatch.setattr(S.time, "time", lambda: 1003)   # 3s ago
    assert S._pane_recent_keystrokes("%5", 5) is True


def test_old_input_outside_window(monkeypatch):
    monkeypatch.setattr(S, "_last_client_activity", lambda p: 1000)
    monkeypatch.setattr(S.time, "time", lambda: 1007)   # 7s ago
    assert S._pane_recent_keystrokes("%5", 5) is False


def test_zero_window_disables_skip(monkeypatch):
    monkeypatch.setattr(S, "_last_client_activity", lambda p: 99999)
    assert S._pane_recent_keystrokes("%5", 0) is False


def test_cant_tell_fails_open(monkeypatch):
    monkeypatch.setattr(S, "_last_client_activity", lambda p: None)
    assert S._pane_recent_keystrokes("%5", 5) is False


# --- force-highlight flag (prefix V), "until you type again" ---------------

def test_force_active_until_typed_again(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(S.time, "time", lambda: 2000)
    S.set_force_highlight()                              # pressed at t=2000
    assert S._force_highlight_flag_path().read_text() == "2000"

    # No input since the press (client activity <= press) -> still active.
    monkeypatch.setattr(S, "_last_client_activity", lambda p: 2000)
    assert S._force_highlight_active("%5") is True

    # User types again (client activity past the press) -> expires + clears.
    monkeypatch.setattr(S, "_last_client_activity", lambda p: 2001)
    assert S._force_highlight_active("%5") is False
    assert not S._force_highlight_flag_path().exists()


def test_force_inactive_without_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(S, "_last_client_activity", lambda p: 2000)
    assert S._force_highlight_active("%5") is False


def test_force_inactive_when_cant_tell(monkeypatch, tmp_path):
    # Can't read client activity -> don't override the skip (so a stale flag
    # with no attached client can't suppress the keystroke-skip forever).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(S.time, "time", lambda: 2000)
    S.set_force_highlight()
    monkeypatch.setattr(S, "_last_client_activity", lambda p: None)
    assert S._force_highlight_active("%5") is False


def test_force_expires_after_max_age(monkeypatch, tmp_path):
    # A flag older than the backstop is ignored + cleared, even with no typing.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(S.time, "time", lambda: 2000)
    S.set_force_highlight()                              # pressed at t=2000
    monkeypatch.setattr(S, "_last_client_activity", lambda p: 2000)  # no typing
    monkeypatch.setattr(S.time, "time", lambda: 2000 + S._FORCE_MAX_AGE_S + 1)
    assert S._force_highlight_active("%5") is False
    assert not S._force_highlight_flag_path().exists()


# --- alternate-screen detection (transient pulse vs scroll-and-hold) --------

def test_pane_alternate_on_true(monkeypatch):
    def fake_run(cmd, **kw):
        class _R:
            returncode = 0
            stdout = "1\n"
        return _R()
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    assert S._pane_alternate_on("%5") is True


def test_pane_alternate_on_false(monkeypatch):
    def fake_run(cmd, **kw):
        class _R:
            returncode = 0
            stdout = "0\n"
        return _R()
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    assert S._pane_alternate_on("%5") is False


def test_pane_alternate_on_error(monkeypatch):
    def fake_run(cmd, **kw):
        raise RuntimeError("no tmux")
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    assert S._pane_alternate_on("%5") is False


# --- popup-open override ----------------------------------------------------

def test_popup_open_matches_pane(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    p = S._popup_open_flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("%5")
    assert S._popup_open_for("%5") is True
    assert S._popup_open_for("%9") is False             # different pane


def test_popup_closed_when_no_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert S._popup_open_for("%5") is False
