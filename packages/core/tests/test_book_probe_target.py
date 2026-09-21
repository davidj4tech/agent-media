"""The book probe must not ask the speech player whether it is playing a book.

With no book socket of its own, a target's book falls back to its speech
socket. For `app` that is Sasonica's speech player — books there play through
ExoPlayer on another listener — so before_speech spent 1.9s per reply (21 Sep,
its longest step) asking our own speech player about a book it could never be
playing, and would have "paused the book" by pausing speech had a reply still
been sounding there.
"""

import pytest

from agent_media_core.route.coordinator import Coordinator
from agent_media_core.state import StateStore
from agent_media_core.types import Target


class _Book:
    def __init__(self):
        self.asked = 0

    def active(self, target):
        self.asked += 1
        return True


class _Music:
    pass


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_APP", "tcp://phone.example:6613")
    monkeypatch.delenv("MEDIA_BOOK_SOCKET_APP", raising=False)


def _coord(book):
    return Coordinator(music=_Music(), state=StateStore(), book=book,
                       book_target=Target(name="app"))


def test_a_book_that_is_only_the_speech_player_is_not_asked():
    book = _Book()
    assert _coord(book)._probe_book_active() is False
    assert book.asked == 0, "the probe went to the speech player"


def test_a_book_with_its_own_player_is_still_asked(monkeypatch):
    monkeypatch.setenv("MEDIA_BOOK_SOCKET_APP", "tcp://phone.example:6603")
    book = _Book()
    assert _coord(book)._probe_book_active() is True
    assert book.asked == 1
