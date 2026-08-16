"""`media recent`, and `media music resume` after a stop.

The store-level behaviour is in test_recently_played.py; this is the part the
listener actually touches — the listing, and the transport key that used to
produce silence with no explanation.
"""

import json

import pytest

from agent_media_core import cli
from agent_media_core.state import StateStore

A = "mpv:https://www.youtube.com/watch?v=aaaaaaaaaaa"
B = "local:track:Some%20Album/02%20Second.mp3"


@pytest.fixture()
def store(tmp_path, monkeypatch) -> StateStore:
    """A state db of our own — the CLI resolves it from XDG_STATE_HOME."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return StateStore()


# Held before any test patches `cli.main` — the reopen path re-enters main to
# run the play command, so those tests patch it and still need a way in.
_main = cli.main


def _run(argv, capsys):
    rc = _main(argv)
    return rc, capsys.readouterr()


def test_recent_lists_both_channels_newest_first(store, capsys):
    store.set_music_intent(A, "dj-set")
    store.set_book_last(B)
    rc, out = _run(["recent"], capsys)
    assert rc == 0
    lines = out.out.strip().splitlines()
    assert len(lines) == 2
    assert "book" in lines[0] and "music" in lines[1]
    # The URI is unreadable as-is, so a YouTube row shows its id.
    assert "youtube:aaaaaaaaaaa" in lines[1]
    assert "dj-set" in lines[1]


def test_recent_filters_by_channel(store, capsys):
    store.set_music_intent(A, "music")
    store.set_book_last(B)
    _, out = _run(["recent", "--channel", "book"], capsys)
    assert len(out.out.strip().splitlines()) == 1
    assert "book" in out.out


def test_recent_honours_the_count(store, capsys):
    for i in range(5):
        store.set_music_intent(f"{A}{i}", "music")
    _, out = _run(["recent", "2"], capsys)
    assert len(out.out.strip().splitlines()) == 2


def test_recent_lines_are_pickable(store, capsys):
    # display<TAB>uri: the second field is what a picker will play.
    store.set_music_intent(A, "music")
    _, out = _run(["recent", "--lines"], capsys)
    label, uri = out.out.strip().split("\t")
    assert uri == A and "youtube:aaaaaaaaaaa" in label


def test_recent_json(store, capsys):
    store.set_music_intent(A, "music")
    _, out = _run(["recent", "--json"], capsys)
    rows = json.loads(out.out)
    assert rows[0]["uri"] == A and rows[0]["sink"] == "music"


def test_recent_on_an_empty_store_says_so(store, capsys):
    rc, out = _run(["recent"], capsys)
    assert rc == 0 and "nothing played yet" in out.out


def test_a_shared_title_shows_instead_of_the_id(store, capsys):
    store.set_music_intent(A, "music", "Me at the zoo")
    _, out = _run(["recent"], capsys)
    assert "Me at the zoo" in out.out
    assert "youtube:aaaaaaaaaaa" not in out.out


def test_recent_survives_a_row_with_no_title(store, capsys):
    store.note_play("music", "https://example.com/stream")
    _, out = _run(["recent"], capsys)
    assert "stream" in out.out


# ---- music resume after a stop -------------------------------------------

class _Idle:
    """A music backend with nothing loaded."""

    def now_playing_uri(self, *a, **kw):
        return None

    def resume(self, *a, **kw):
        raise AssertionError("resume() called on an idle backend")


class _Loaded(_Idle):
    """...and one that is playing. The transport dict cmd_music builds names
    every action, so all of them have to exist even when one is under test."""

    def __init__(self):
        self.resumed = False

    def now_playing_uri(self, *a, **kw):
        return A

    def resume(self, *a, **kw):
        self.resumed = True

    def pause(self, *a, **kw): pass
    def toggle(self, *a, **kw): pass
    def next(self, *a, **kw): pass
    def previous(self, *a, **kw): pass


def test_resume_reopens_the_last_thing_after_a_stop(store, capsys, monkeypatch):
    store.set_music_intent(A, "dj-set")
    store.clear_music_intent()  # music stop
    monkeypatch.setattr(cli, "_music_live_backend", lambda m: _Idle())
    played = []
    monkeypatch.setattr(cli, "main", lambda argv: played.append(argv) or 0)

    rc, out = _run(["music", "resume"], capsys)
    assert rc == 0
    # The content type is carried back, so a re-opened audiobook still pauses
    # under speech rather than ducking.
    assert played == [["music", "play", A, "--as", "dj-set"]]
    assert "resuming last played" in out.out


def test_resume_with_something_loaded_just_resumes(store, capsys, monkeypatch):
    store.set_music_intent(A, "music")
    backend = _Loaded()
    monkeypatch.setattr(cli, "_music_live_backend", lambda m: backend)
    monkeypatch.setattr(cli, "main", lambda argv: pytest.fail("reopened a live channel"))

    rc, _ = _run(["music", "resume"], capsys)
    assert rc == 0 and backend.resumed


def test_resume_with_nothing_ever_played_explains_itself(store, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_music_live_backend", lambda m: _Idle())
    rc, out = _run(["music", "resume"], capsys)
    assert rc == 1
    assert "nothing played yet" in out.err


def test_an_unreadable_backend_is_not_treated_as_idle(monkeypatch):
    # Conservative on the unknown: a transport key must not start music just
    # because Mopidy was unreachable for one probe.
    class Broken:
        def now_playing_uri(self, *a, **kw):
            raise OSError("connection refused")

    assert cli._music_idle(Broken()) is False
