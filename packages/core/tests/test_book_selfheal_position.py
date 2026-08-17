"""Regression: the book self-heal must not reload a book at another book's
position.

The watcher remembers the live `time-pos` on every heartbeat so that an error
end-file — an expired YouTube media URL, a network drop — reloads where you
were rather than at the start. That memory outlived the file it was read from.
On 2026-08-17 a shared 61-minute set was loaded onto the book channel, errored,
and was rehealed at 1h19m: where the podcast playing before it had got to. Past
the end of the file, so it errored again immediately, three times, and the share
looked like it had simply not played.

So the position now carries the URI it was read from, and is ignored for
anything else.
"""

import agent_media_core.mcp_server as M


class _Book:
    def __init__(self):
        self.loaded = []

    def play(self, uri, target, start_ms=0):
        self.loaded.append((uri, start_ms))


class _State:
    def __init__(self, last, bookmarks=None):
        self.last = last
        self.bookmarks = bookmarks or {}

    def get_book_last(self):
        return self.last

    def get_resume_pos(self, uri):
        return self.bookmarks.get(uri)

    def get_now_playing(self, sink):
        return None


def _wire(monkeypatch, state, book):
    monkeypatch.setattr(M, "_state", lambda: state)
    monkeypatch.setattr(M, "_book", lambda: book)
    monkeypatch.setattr(M, "_book_target", lambda name: M.Target("local"))


FRESH = "https://m.youtube.com/watch?v=CqrmscGUUA8"
BEFORE = "https://traffic.libsyn.com/sacstudio/td565.mp3"


def test_position_from_another_book_is_not_used(monkeypatch):
    book = _Book()
    _wire(monkeypatch, _State(FRESH), book)

    assert M._reheal_after_error(4782805, BEFORE) is True
    assert book.loaded == [(FRESH, 0)]  # its own bookmark: none, so from the top


def test_position_from_this_book_is_used(monkeypatch):
    book = _Book()
    _wire(monkeypatch, _State(FRESH), book)

    assert M._reheal_after_error(90_000, FRESH) is True
    assert book.loaded == [(FRESH, 90_000)]


def test_bookmark_when_the_position_belongs_elsewhere(monkeypatch):
    """Falling back is to *this* book's bookmark, not to zero."""
    book = _Book()
    _wire(monkeypatch, _State(FRESH, {FRESH: 12_000}), book)

    assert M._reheal_after_error(4782805, BEFORE) is True
    assert book.loaded == [(FRESH, 12_000)]


def test_unknown_origin_still_trusts_the_position(monkeypatch):
    """No URI means an older caller, which behaved this way — keep it."""
    book = _Book()
    _wire(monkeypatch, _State(FRESH), book)

    assert M._reheal_after_error(90_000) is True
    assert book.loaded == [(FRESH, 90_000)]
