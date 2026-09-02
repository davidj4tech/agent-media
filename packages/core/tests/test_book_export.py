"""The same conversations, laid out as books.

Audiobookshelf navigates chapters properly for books and badly for podcast
episodes — on the Android app, not at all — and a conversation is nothing but
chapters. So the episodes are mirrored into a tree a book library can scan.
"""

import os

import pytest

from agent_media_core import book_export, feed


@pytest.fixture(autouse=True)
def _spool(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setenv("MEDIA_BOOK_EXPORT_ROOT", str(tmp_path / "books"))
    return tmp_path


def _publish(tmp_path, feed_name, guid, title):
    src = tmp_path / f"{guid}.mp3"
    src.write_bytes(b"ID3" + guid.encode() + b"audio")
    return feed.publish(feed_name, src, guid=guid, title=title, duration_s=61.0)


def test_a_conversation_becomes_author_workspace_title_question(tmp_path):
    _publish(tmp_path, "p-agent-media", "s-1",
             "p-agent-media · why is the ringer loud")
    linked, removed = book_export.export()
    assert (linked, removed) == (1, 0)
    book = book_export.root() / "p-agent-media" / "why is the ringer loud"
    assert (book / "why is the ringer loud.mp3").is_file()


def test_the_workspace_is_not_repeated_in_the_title(tmp_path):
    """The feed name is already the folder; a title that says it again gives
    `p-agent-media/p-agent-media - why…`."""
    _publish(tmp_path, "scratch", "s-1", "scratch · update ftv")
    book_export.export()
    assert (book_export.root() / "scratch" / "update ftv").is_dir()


def test_it_is_a_hardlink_not_a_copy(tmp_path):
    """The spool holds the only durable copy; a second one on the same disk
    buys nothing and doubles what a long conversation costs."""
    ep = _publish(tmp_path, "talks", "s-1", "a talk")
    book_export.export()
    src = feed.feed_dir("talks") / ep.filename
    dest = book_export.root() / "talks" / "a talk" / "a talk.mp3"
    assert os.stat(src).st_ino == os.stat(dest).st_ino


def test_running_it_twice_relinks_nothing(tmp_path):
    _publish(tmp_path, "talks", "s-1", "a talk")
    assert book_export.export() == (1, 0)
    assert book_export.export() == (0, 0)


def test_an_episode_that_has_gone_takes_its_folder_with_it(tmp_path):
    """Retention prunes the feed; a library that kept everything it ever saw
    would be a scrapbook, not a mirror."""
    _publish(tmp_path, "talks", "s-1", "a talk")
    _publish(tmp_path, "talks", "s-2", "another talk")
    book_export.export()
    feed.remove("talks", "s-1")
    assert book_export.export() == (0, 1)
    assert not (book_export.root() / "talks" / "a talk").exists()
    assert (book_export.root() / "talks" / "another talk").is_dir()


def test_documents_are_not_conversations(tmp_path):
    """A document read aloud has no workspace and no turns; it belongs in the
    feed it already has."""
    _publish(tmp_path, "docs", "d-1", "A feed of what was said")
    _publish(tmp_path, "digest", "g-1", "Agenda 2026-09-02")
    assert book_export.export() == (0, 0)
    assert not any(book_export.root().iterdir())


@pytest.mark.parametrize("raw,want", [
    ("p-agent-media · why?", "p-agent-media - why"),
    ("a/b: c", "a b c"),
    ("   ", "conversation"),
    ("x" * 200, "x" * 110),
])
def test_a_title_is_made_safe_for_a_filesystem(raw, want):
    assert book_export.safe_name(raw) == want


def test_a_missing_audio_file_is_skipped_not_crashed_on(tmp_path):
    ep = _publish(tmp_path, "talks", "s-1", "a talk")
    (feed.feed_dir("talks") / ep.filename).unlink()
    assert book_export.export() == (0, 0)
