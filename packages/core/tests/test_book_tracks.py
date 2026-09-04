"""A conversation laid out as an item that grows.

The property everything else rests on: **nothing already written is ever
rewritten**. That is what lets Audiobookshelf keep the item's id, keep the
existing files' inodes, and keep the listener's position while the conversation
carries on — measured against 2.35.1 in
`docs/proposals/2026-09-02-growing-item-experiment.md`, which is what this
module was written against.
"""

import json

import pytest

from agent_media_core import book_export, book_tracks, session_feed


@pytest.fixture(autouse=True)
def tree(tmp_path, monkeypatch):
    """A book tree and a state dir of our own."""
    monkeypatch.setenv("MEDIA_BOOK_TRACKS_ROOT", str(tmp_path / "books"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def _clip(tmp_path, name: str, body: bytes = b"audio") -> str:
    p = tmp_path / "clips" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return str(p)


def _turn(tmp_path, at: float, text: str, clips: int = 1):
    return session_feed.Turn(
        at=at, text=text, workspace="p-agent-media",
        clips=[_clip(tmp_path, f"{at}-{i}.mp3", f"clip {at}.{i}".encode())
               for i in range(clips)],
        durations=[3.0] * clips)


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    """Three turns that can be added to, one clip each (no ffmpeg needed)."""
    turns = [_turn(tmp_path, 100.0, "First thing said."),
             _turn(tmp_path, 200.0, "Second thing said."),
             _turn(tmp_path, 300.0, "Third thing said.")]
    state = {"turns": turns[:2]}
    monkeypatch.setattr(session_feed, "turns",
                        lambda session, store=None: list(state["turns"]))
    monkeypatch.setattr(session_feed, "workspace_for",
                        lambda session, ts: "p-agent-media")
    monkeypatch.setattr(session_feed, "title_for",
                        lambda session, ts: "How the growing item works")
    return state, turns


def test_a_conversation_becomes_one_track_per_turn(conversation):
    state, _ = conversation
    folder, added = book_tracks.export_session("sess-1")
    assert added == 2
    assert sorted(p.name for p in folder.iterdir()) == [
        "001 - First thing said.mp3", "002 - Second thing said.mp3"]


def test_the_author_says_these_are_the_growing_ones(conversation):
    folder, _ = book_tracks.export_session("sess-1")
    assert folder.parent.name == "p-agent-media (live)"
    assert folder.name == "How the growing item works"


def test_a_new_turn_appends_and_touches_nothing_else(conversation):
    state, turns = conversation
    folder, _ = book_tracks.export_session("sess-1")
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns)
              for p in folder.iterdir()}

    state["turns"] = turns                      # the conversation goes on
    folder2, added = book_tracks.export_session("sess-1")

    assert (folder2, added) == (folder, 1)
    after = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns)
             for p in folder.iterdir()}
    assert "003 - Third thing said.mp3" in after
    for name, was in before.items():
        assert after[name] == was, f"{name} was rewritten"


def test_running_again_with_nothing_new_writes_nothing(conversation):
    book_tracks.export_session("sess-1")
    folder, added = book_tracks.export_session("sess-1")
    assert added == 0


def test_the_folder_survives_a_better_title(conversation, monkeypatch):
    """A conversation's title comes from what was asked and can improve as it
    goes. Renaming the folder would hand ABS a new item — new id, no progress,
    the old one left behind — so the first answer is the one that is kept."""
    folder, _ = book_tracks.export_session("sess-1")
    monkeypatch.setattr(session_feed, "title_for",
                        lambda session, ts: "A much better title")
    folder2, _ = book_tracks.export_session("sess-1")
    assert folder2 == folder


def test_a_lost_manifest_adopts_what_is_on_disk(conversation, tmp_path):
    """The manifest is a record of the tree, not its owner. If it goes and the
    tree stays, the export must recognise its own work — not write every turn
    again beside itself, which is an item that plays the conversation twice."""
    folder, _ = book_tracks.export_session("sess-1")
    book_tracks._manifest_path("sess-1").unlink()

    folder2, added = book_tracks.export_session("sess-1")
    assert (folder2, added) == (folder, 0)
    assert len(list(folder.iterdir())) == 2
    kept = json.loads(book_tracks._manifest_path("sess-1").read_text())
    assert [t["file"] for t in kept["turns"]] == [
        "001 - First thing said.mp3", "002 - Second thing said.mp3"]


def test_a_turn_that_will_not_join_stops_the_run(conversation, monkeypatch):
    """Turns are ordered, so skipping a failure would file the next turn's
    audio under this one's number — a conversation quietly out of sequence."""
    state, turns = conversation
    state["turns"] = turns
    monkeypatch.setattr(book_tracks, "join_clips",
                        lambda clips, out, expected=0.0: None)
    folder, added = book_tracks.export_session("sess-1")
    assert added == 0
    assert not folder.exists() or not list(folder.iterdir())


def test_track_numbers_are_padded_so_ten_follows_nine():
    class T:
        title = "A turn"
    names = [book_tracks.track_name(i, T()) for i in (2, 10)]
    assert names == ["002 - A turn.mp3", "010 - A turn.mp3"]
    assert sorted(names) == names          # what a scanner will do with them


def test_a_single_clip_turn_is_linked_not_copied(tmp_path):
    src = tmp_path / "one.mp3"
    src.write_bytes(b"the only copy")
    out = tmp_path / "item" / "001 - one.mp3"
    assert book_tracks.join_clips([str(src)], out) == out
    assert out.stat().st_ino == src.stat().st_ino


# --- the two trees share a shelf -------------------------------------------

def test_the_prune_leaves_the_growing_items_alone(tmp_path, monkeypatch):
    """`book_export` deletes any book folder the feeds do not account for, and
    nothing here comes from a feed. Without the guard, every growing item is
    swept on the next publish — mid-listen, for tidiness."""
    monkeypatch.setattr(book_export, "root", lambda: tmp_path / "books")
    monkeypatch.setattr(book_export.feedmod, "feeds", lambda: [])
    live = tmp_path / "books" / f"p-agent-media{book_tracks.LIVE_SUFFIX}" / "Item"
    live.mkdir(parents=True)
    (live / "001 - turn.mp3").write_bytes(b"x")
    finished = tmp_path / "books" / "p-agent-media" / "Old Episode"
    finished.mkdir(parents=True)
    (finished / "Old Episode.mp3").write_bytes(b"x")

    linked, removed = book_export.export(where=tmp_path / "books")

    assert live.exists() and (live / "001 - turn.mp3").exists()
    assert not finished.exists()
    assert removed == 1


def test_both_modules_agree_on_the_suffix():
    assert book_export.LIVE_SUFFIX == book_tracks.LIVE_SUFFIX
