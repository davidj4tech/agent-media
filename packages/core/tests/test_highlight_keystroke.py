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


# --- transcript dump / restore (MEDIA_HIGHLIGHT_DUMP) -----------------------

class _RunRec:
    """Record tmux invocations; `alt_after` is what #{alternate_on} reports
    (used to simulate the dump taking effect or not)."""
    def __init__(self, alt_after="0"):
        self.calls = []
        self.alt_after = alt_after

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        class _R:
            returncode = 0
            stdout = self.alt_after + "\n"
        return _R()

    def keys(self):
        """The send-keys key args, in order."""
        return [c[c.index("send-keys") + 3] for c in self.calls
                if "send-keys" in c and "-X" not in c]


def test_dump_transcript_success(monkeypatch):
    monkeypatch.setenv("MEDIA_HIGHLIGHT_DUMP_SETTLE_MS", "0")
    S._dumped_pane = None
    rec = _RunRec(alt_after="0")            # pane left the alt-screen -> took
    monkeypatch.setattr(S.subprocess, "run", rec)
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)

    assert S._dump_transcript("%5") is True
    assert S._dumped_pane == "%5"
    # Cleared history, then sent Ctrl+O then '[' in order.
    assert any("clear-history" in c for c in rec.calls)
    assert rec.keys() == ["C-o", "["]
    S._dumped_pane = None


def test_dump_transcript_noop_when_not_alt(monkeypatch):
    """If the pane is still on the alt-screen afterward, the dump didn't take
    (not Claude / keys ignored): report failure and don't arm a restore."""
    monkeypatch.setenv("MEDIA_HIGHLIGHT_DUMP_SETTLE_MS", "0")
    S._dumped_pane = None
    rec = _RunRec(alt_after="1")            # still fullscreen
    monkeypatch.setattr(S.subprocess, "run", rec)
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)

    assert S._dump_transcript("%5") is False
    assert S._dumped_pane is None
    S._dumped_pane = None


def test_restore_fullscreen_sends_escape(monkeypatch):
    S._dumped_pane = "%5"
    rec = _RunRec()
    monkeypatch.setattr(S.subprocess, "run", rec)

    S._restore_fullscreen()
    # Leaves copy-mode, then Escape back into Claude's fullscreen renderer.
    assert ["tmux", "send-keys", "-t", "%5", "-X", "cancel"] in rec.calls
    assert rec.keys() == ["Escape"]
    assert S._dumped_pane is None


def test_restore_fullscreen_noop_without_dump(monkeypatch):
    S._dumped_pane = None
    rec = _RunRec()
    monkeypatch.setattr(S.subprocess, "run", rec)
    S._restore_fullscreen()
    assert rec.calls == []                  # nothing dumped -> nothing to undo


def test_highlight_dump_first_dumps_on_fullscreen(monkeypatch):
    """First sentence, dump mode on, fullscreen pane: dump the transcript before
    the copy-mode follow-along runs."""
    monkeypatch.setenv("TMUX", "1")
    monkeypatch.setenv("TMUX_PANE", "%5")
    monkeypatch.setenv("MEDIA_HIGHLIGHT_DUMP", "1")
    monkeypatch.setattr(S, "_is_auto_highlight_enabled", lambda: True)
    monkeypatch.setattr(S, "_pane_alternate_on", lambda p: True)  # fullscreen
    monkeypatch.setattr(S, "_pane_recent_keystrokes", lambda p, w: False)
    dumped = {"n": 0}
    def _fake_dump(pane):
        dumped["n"] += 1
        S._dumped_pane = pane
        return True
    monkeypatch.setattr(S, "_dump_transcript", _fake_dump)
    monkeypatch.setattr(S, "_anchor_for", lambda t, max_len=50: "an anchor line")
    monkeypatch.setattr(S, "_pane_anchor_width", lambda p: 50)
    monkeypatch.setattr(S, "_pane_scroll_pos", lambda p: (False, ""))
    # Make the copy-mode search "find nothing" (cursor unchanged) so the call
    # returns right after the dump without spawning the flash timer.
    monkeypatch.setattr(S, "_cursor_sig", lambda p: "same")
    monkeypatch.setattr(S.subprocess, "run", _RunRec())

    S._dumped_pane = None
    S._tmux_highlight_text("Some spoken sentence here.", first=True)
    assert dumped["n"] == 1
    assert S._dumped_pane == "%5"
    S._dumped_pane = None


def _dump_mode_stubs(monkeypatch, *, alt, typing):
    """Common stubs for the (re)dump branch: dump mode on, auto-highlight on,
    pane alt-screen state = `alt`, keystroke state = `typing`. Returns a dict
    counting _dump_transcript calls."""
    monkeypatch.setenv("TMUX", "1")
    monkeypatch.setenv("TMUX_PANE", "%5")
    monkeypatch.setenv("MEDIA_HIGHLIGHT_DUMP", "1")
    monkeypatch.setattr(S, "_is_auto_highlight_enabled", lambda: True)
    monkeypatch.setattr(S, "_pane_alternate_on", lambda p: alt)
    monkeypatch.setattr(S, "_pane_recent_keystrokes", lambda p, w: typing)
    monkeypatch.setattr(S, "_force_highlight_active", lambda p: False)
    monkeypatch.setattr(S, "_popup_open_for", lambda p: False)
    monkeypatch.setattr(S, "_anchor_for", lambda t, max_len=50: "an anchor line")
    monkeypatch.setattr(S, "_pane_anchor_width", lambda p: 50)
    monkeypatch.setattr(S, "_pane_scroll_pos", lambda p: (False, ""))
    monkeypatch.setattr(S, "_cursor_sig", lambda p: "same")
    monkeypatch.setattr(S.subprocess, "run", _RunRec())
    dumped = {"n": 0}
    def _fake_dump(pane):
        dumped["n"] += 1
        S._dumped_pane = pane
        return True
    monkeypatch.setattr(S, "_dump_transcript", _fake_dump)
    return dumped


def test_highlight_dump_redumps_when_pane_restaled(monkeypatch):
    """A later sentence whose pane flipped back to fullscreen (Claude redrew)
    re-dumps to refresh the staled scrollback — the "refresh once in a while"."""
    dumped = _dump_mode_stubs(monkeypatch, alt=True, typing=False)
    S._dumped_pane = "%5"
    S._tmux_highlight_text("A later sentence.", first=False)
    assert dumped["n"] == 1                  # re-dumped despite not being first
    S._dumped_pane = None


def test_highlight_dump_no_redump_while_holding(monkeypatch):
    """A later sentence while the dump still holds (pane on normal screen):
    no re-dump — search the existing scrollback, don't churn Ctrl+O/[."""
    dumped = _dump_mode_stubs(monkeypatch, alt=False, typing=False)
    S._dumped_pane = "%5"
    S._tmux_highlight_text("A later sentence.", first=False)
    assert dumped["n"] == 0
    S._dumped_pane = None


def test_highlight_dump_keystroke_restores_midresponse(monkeypatch):
    """A later sentence, dumped pane, user typing: restore fullscreen and skip
    (their keys must reach the input box, not copy-mode nav)."""
    monkeypatch.setenv("TMUX", "1")
    monkeypatch.setenv("TMUX_PANE", "%5")
    monkeypatch.setenv("MEDIA_HIGHLIGHT_DUMP", "1")
    monkeypatch.setattr(S, "_is_auto_highlight_enabled", lambda: True)
    monkeypatch.setattr(S, "_pane_recent_keystrokes", lambda p, w: True)
    monkeypatch.setattr(S, "_force_highlight_active", lambda p: False)
    monkeypatch.setattr(S, "_popup_open_for", lambda p: False)
    restored = {"n": 0}
    monkeypatch.setattr(S, "_restore_fullscreen",
                        lambda: restored.update(n=restored["n"] + 1))
    # If it fails to bail, it would hit the anchor/copy-mode path — blow up loud.
    monkeypatch.setattr(S, "_anchor_for",
                        lambda *a, **k: pytest.fail("should have bailed"))

    S._dumped_pane = "%5"
    S._tmux_highlight_text("A later sentence.", first=False)
    assert restored["n"] == 1
    S._dumped_pane = None
