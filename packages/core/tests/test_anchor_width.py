"""Width-aware search anchor: highlight must fit one visual row on narrow panes.

tmux's search-backward is row-bound, so on a narrow pane (e.g. a 32-col phone)
a 50-char anchor wraps and matches nothing. `_anchor_for` takes a `max_len`
(the pane width) and `_pane_anchor_width` derives it from the target pane.
"""

from agent_media_core.intake import submit as S

_LONG = ("The quick brown fox jumps over the lazy dog and then keeps "
         "on running across the whole field forever and ever")


def test_anchor_default_caps_at_50():
    a = S._anchor_for(_LONG)
    assert a is not None
    assert len(a) <= 50
    assert _LONG.startswith(a)          # a prefix of the line
    assert not a.endswith(" ")          # trimmed at a word boundary


def test_anchor_narrow_pane_caps_to_width():
    a = S._anchor_for(_LONG, max_len=31)
    assert a is not None
    assert 15 <= len(a) <= 31
    assert _LONG.startswith(a)
    assert " " not in a[-1:]            # ends on a word, not mid-space


def test_anchor_too_narrow_returns_none():
    # A pane so narrow no unique (>=15 char) single-row anchor fits.
    assert S._anchor_for(_LONG, max_len=10) is None


def test_anchor_short_line_still_none_regardless_of_width():
    # Lines under 15 chars are never unique enough, even with a wide cap.
    assert S._anchor_for("too short", max_len=50) is None


def test_pane_anchor_width_clamps(monkeypatch):
    def fake_run(cmd, **kw):
        class _R:
            returncode = 0
            stdout = "32\n"
        return _R()
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    assert S._pane_anchor_width("%5") == 31          # min(50, max(15, 32-1))


def test_pane_anchor_width_wide_pane_stays_50(monkeypatch):
    def fake_run(cmd, **kw):
        class _R:
            returncode = 0
            stdout = "200\n"
        return _R()
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    assert S._pane_anchor_width("%5") == 50          # capped at the old fixed 50


def test_pane_anchor_width_falls_back_on_error(monkeypatch):
    def fake_run(cmd, **kw):
        raise RuntimeError("no tmux")
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    assert S._pane_anchor_width("%5") == 50          # safe default
