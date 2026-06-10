"""Tests for the audiobook library resolver and the book-seek timecode parser."""

from __future__ import annotations

import pytest

from agent_media_core import library
from agent_media_core import cli
from agent_media_core.cli import _parse_timecode


# --- YouTube detection + id extraction ------------------------------------

@pytest.mark.parametrize("uri", [
    "https://www.youtube.com/watch?v=6Pm736hLECw",
    "https://youtube.com/watch?v=6Pm736hLECw&t=10",
    "https://youtu.be/6Pm736hLECw",
    "https://www.youtube.com/shorts/6Pm736hLECw",
    "https://m.youtube.com/watch?v=6Pm736hLECw",
])
def test_is_youtube_and_id(uri):
    assert library.is_youtube(uri)
    assert library.video_id(uri) == "6Pm736hLECw"


@pytest.mark.parametrize("uri", [
    "/home/mel/media/audiobooks/book.webm",
    "https://example.com/stream.mp3",
    "file:///x.opus",
])
def test_not_youtube(uri):
    assert not library.is_youtube(uri)


def test_cached_path_matches_bracketed_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AUDIOBOOK_LIB", str(tmp_path))
    want = tmp_path / "KingsOftheWyld - Nicholas Eames 1⧸2 [6Pm736hLECw].webm"
    want.write_bytes(b"x")
    (tmp_path / "Some Other Book [abcDEFghijk].webm").write_bytes(b"y")
    assert library.cached_path("6Pm736hLECw") == want
    assert library.cached_path("missing00000") is None


def test_cached_path_no_library_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AUDIOBOOK_LIB", str(tmp_path / "nope"))
    assert library.cached_path("6Pm736hLECw") is None


def test_start_fetch_without_helper(monkeypatch):
    monkeypatch.setenv("MEDIA_AUDIOBOOK_FETCH", "")
    monkeypatch.setattr(library.shutil, "which", lambda _: None)
    assert library.start_fetch("https://youtu.be/x") is False


# --- timecode parsing ------------------------------------------------------

@pytest.mark.parametrize("text,secs,relative", [
    ("1:33:35", 5615.0, False),
    ("93:35", 5615.0, False),
    ("5615", 5615.0, False),
    ("0:30", 30.0, False),
    ("+90", 90.0, True),
    ("+1:00", 60.0, True),
    ("-5:00", -300.0, True),
    ("-30", -30.0, True),
    ("1:02:03.5", 3723.5, False),
])
def test_parse_timecode(text, secs, relative):
    assert _parse_timecode(text) == (secs, relative)


@pytest.mark.parametrize("bad", ["", ":", "1::2", "abc", "1:xx"])
def test_parse_timecode_rejects_garbage(bad):
    with pytest.raises(ValueError):
        _parse_timecode(bad)


# --- unified seek/skip action ---------------------------------------------

class _FakeSrv:
    """Records which book transport method the action chose."""
    def __init__(self):
        self.calls = []

    def book_skip(self, *, seconds, target):
        self.calls.append(("skip", seconds, target))
        return {"ok": True}

    def book_seek(self, *, position_secs, target):
        self.calls.append(("seek", position_secs, target))
        return {"position_ms": position_secs * 1000}


def test_seek_absolute_jumps(capsys):
    srv = _FakeSrv()
    assert cli._book_seek_action(srv, "1:33:35", "") == 0
    assert srv.calls == [("seek", 5615.0, "local")]
    assert "⏱" in capsys.readouterr().out


def test_seek_signed_is_relative(capsys):
    srv = _FakeSrv()
    assert cli._book_seek_action(srv, "-5:00", "") == 0
    assert srv.calls == [("skip", -300.0, "local")]


def test_skip_forces_relative_on_bare_number(capsys):
    """`book skip 30` (bare, unsigned) must offset +30, not jump to 0:30."""
    srv = _FakeSrv()
    assert cli._book_seek_action(srv, "30", "", force_relative=True) == 0
    assert srv.calls == [("skip", 30.0, "local")]


def test_seek_bad_timecode_returns_2(capsys):
    srv = _FakeSrv()
    assert cli._book_seek_action(srv, "abc", "") == 2
    assert srv.calls == []


@pytest.mark.parametrize("argv,expect", [
    (["book", "seek", "-5:00"], ["book", "seek", "--", "-5:00"]),
    (["book", "skip", "-1:30"], ["book", "skip", "--", "-1:30"]),
    # Bare negative numbers already parse — left untouched is fine either way,
    # but a `--` is harmless; absolute and `+` forms are never rewritten.
    (["book", "seek", "1:33:35"], ["book", "seek", "1:33:35"]),
    (["book", "seek", "+90"], ["book", "seek", "+90"]),
    (["book", "seek", "--", "-5:00"], ["book", "seek", "--", "-5:00"]),
    (["music", "seek", "-5"], ["music", "seek", "-5"]),  # not book → untouched
])
def test_end_opts_before_book_time(argv, expect):
    assert cli._end_opts_before_book_time(argv) == expect
