"""The popup's book row: the phone's player first, mpv as the fallback."""

import pytest

from agent_media_visual import canvas


@pytest.fixture(autouse=True)
def no_cache():
    canvas._PHONE_BOOK.update(at=0.0, state=None)
    yield
    canvas._PHONE_BOOK.update(at=0.0, state=None)


def test_the_clock_grows_an_hour_column_only_when_it_needs_one():
    assert canvas._clock(716) == "11:56"
    assert canvas._clock(14313) == "3:58:33"
    assert canvas._clock(None) == "--:--"


def test_the_book_row_is_the_phones_book_when_the_phone_has_one(monkeypatch):
    monkeypatch.setattr(canvas, "_phone_book", lambda: {
        "title": "Conversation  Skills", "t": 716.0, "dur": 14313.5, "paused": False})
    row = canvas.channel_status("book")
    assert row["label"] == "Conversation Skills"        # whitespace tidied
    assert row["status"] == "▶ 11:56 / 3:58:33"


def test_a_paused_phone_says_so(monkeypatch):
    monkeypatch.setattr(canvas, "_phone_book", lambda: {
        "title": "A book", "t": 1.0, "dur": 2.0, "paused": True})
    assert canvas.channel_status("book")["status"].startswith("❙❙")


def test_without_the_phone_it_is_the_mpv_socket_as_before(monkeypatch):
    monkeypatch.setattr(canvas, "_phone_book", lambda: None)
    monkeypatch.setattr(canvas, "_book_title", lambda: "On the shelf")
    monkeypatch.setattr(canvas, "_media", lambda args: "▶ 00:10 / 01:00")
    row = canvas.channel_status("book")
    assert (row["label"], row["status"]) == ("On the shelf", "▶ 00:10 / 01:00")


def test_the_phone_is_asked_once_and_remembered(monkeypatch):
    asked = []

    class _Player:
        @staticmethod
        def state(target):
            asked.append(target)
            return {"item": "i1", "closed": False, "title": "A book"}

    monkeypatch.setitem(__import__("sys").modules, "agent_media_core.phone_player", _Player)
    monkeypatch.setattr(canvas, "_PHONE_BOOK_TTL_S", 60.0)
    assert canvas._phone_book()["title"] == "A book"
    assert canvas._phone_book()["title"] == "A book"
    assert len(asked) == 1


def test_a_phone_with_nothing_loaded_is_not_the_book_row(monkeypatch):
    class _Player:
        @staticmethod
        def state(target):
            return {"item": None, "closed": True}

    monkeypatch.setitem(__import__("sys").modules, "agent_media_core.phone_player", _Player)
    assert canvas._phone_book() is None
