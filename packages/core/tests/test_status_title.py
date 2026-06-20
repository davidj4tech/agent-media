"""Status-bar title-overlay progress bar (`media status --title`).

`_marquee` scrolls a title one column per call (state persisted, since each
status refresh is a fresh process). `_title_status_line` renders the whole
`▶ pos title dur` segment as ONE tmux background-progress bar — the times are
embedded in the colour fill, which sweeps left→right by overall progress.
"""

from agent_media_core import cli


# --- _marquee --------------------------------------------------------------

def test_marquee_advances_one_col(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    t = "Aria — refactor the speech sink"
    first = cli._marquee(t, 12, key="t")
    second = cli._marquee(t, 12, key="t")
    assert len(first) == 12 and len(second) == 12
    assert first != second                      # scrolled
    assert t.startswith(first)                  # starts at the head


def test_marquee_short_text_no_scroll(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert cli._marquee("short", 12, key="s") == "short"
    assert cli._marquee("short", 12, key="s") == "short"   # stable


def test_marquee_resets_on_text_change(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    long_a = "first long title that scrolls"
    cli._marquee(long_a, 10, key="r")
    cli._marquee(long_a, 10, key="r")           # advance the offset
    long_b = "second long title that scrolls"
    out = cli._marquee(long_b, 10, key="r")     # different text → offset resets
    assert long_b.startswith(out)


# --- _title_status_line ----------------------------------------------------

def _strip_tmux(s: str) -> str:
    import re
    return re.sub(r"#\[[^\]]*\]", "", s)


def test_title_line_is_one_fill_then_track(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    line = cli._title_status_line(30, 120, False, False, None, "Aria", 18, key="x")
    # Exactly one fill region, then one track region, then a reset.
    assert line.startswith("#[bg=colour24,fg=colour231]")
    assert "#[bg=colour236,fg=colour250]" in line
    assert line.endswith("#[default]")


def test_title_line_embeds_both_times(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    plain = _strip_tmux(cli._title_status_line(30, 120, False, False, None,
                                               "Aria", 18, key="x2"))
    assert "00:30" in plain and "02:00" in plain   # both times in the bar
    assert plain.startswith("▶")


def test_title_line_split_tracks_progress(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # At ~0% the fill region is empty; at 100% the track region is empty.
    near0 = cli._title_status_line(0, 120, False, False, None, "Aria", 18, key="a")
    full = cli._title_status_line(120, 120, False, False, None, "Aria", 18, key="b")
    assert near0.startswith("#[bg=colour24,fg=colour231]#[bg=colour236")  # nothing filled
    assert full.endswith("#[bg=colour236,fg=colour250]#[default]")        # nothing left


def test_title_line_muted_and_speed_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    line = cli._title_status_line(60, 120, True, True, 1.4, "Aria", 18, key="m")
    assert line.endswith("[M] ⏩1.4×") or "[M]" in line and "1.4×" in line


def test_title_line_paused_glyph(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    line = _strip_tmux(cli._title_status_line(60, 120, True, False, None,
                                              "Aria", 18, key="p"))
    assert line.startswith("⏸")
