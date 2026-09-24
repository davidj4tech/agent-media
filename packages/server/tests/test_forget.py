"""Deleting a thread (forget.py): everything this host keeps of it, backed up
first, and kept deleted so nothing rebuilds it."""
import json

from agent_media_core import book_tracks, deleted
from agent_media_core.state.store import StateStore, default_db_path
from agent_media_server import forget

A = "01a0d3d4-9c5f-7ea0-a7d6-060e7d7fcc2f"
B = "01a0d3d5-7e07-7bd1-b24d-b33de540e3ef"
KEEP = "3d1b4a2c-1111-4222-8333-444455556666"


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_BOOK_TRACKS_ROOT", str(tmp_path / "shelf"))
    monkeypatch.setattr(forget, "backup_root", lambda: tmp_path / "backups")
    deleted_items = []
    monkeypatch.setattr(book_tracks, "_abs_ready_all", lambda: [("http://abs", "tok", [{"id": "lib"}])])
    monkeypatch.setattr(book_tracks, "_find_item",
                        lambda url, token, libs, folder: {"id": "item-" + folder.name[:4], "path": str(folder)})
    monkeypatch.setattr(forget, "_abs_delete", lambda url, token, i: deleted_items.append(i) or 200)
    st = StateStore(default_db_path())
    for s in (A, A, B, KEEP):
        st.add_history(sink="speech", uri="x.mp3", started_at=1.0, text="hi", extras={"source_session": s})
    shared = tmp_path / "shelf" / "scratch" / "Same question"
    own = tmp_path / "shelf" / "scratch" / "Kept one"
    for folder in (shared, own):
        folder.mkdir(parents=True)
        (folder / "0001 - hi.mp3").write_text("x")
    # A and B asked the same question, so they share a folder; KEEP has its own.
    for s, folder in ((A, shared), (B, shared), (KEEP, own)):
        path = book_tracks._manifest_path(s)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"folder": str(folder), "turns": []}))
    return st, shared, own, deleted_items


def _count(st, s):
    return sum(1 for r in st.recent_history(sink="speech", limit=100)
               if (r.get("extras") or {}).get("source_session") == s)


def test_a_dry_run_changes_nothing(tmp_path, monkeypatch):
    st, shared, _own, items = _setup(tmp_path, monkeypatch)
    assert forget.run([A], apply_=False) == 0
    got = forget.plan([A])
    # B still uses the folder: it stays.
    assert got["sessions"][A]["folder_kept_for"] == [B] and got["abs_items"] == []
    assert _count(st, A) == 2 and shared.exists() and not deleted.is_deleted(A) and items == []


def test_delete_takes_everything_and_keeps_a_backup(tmp_path, monkeypatch):
    st, shared, own, items = _setup(tmp_path, monkeypatch)
    report = forget.apply([A, B], abs_items=("missing-1",))
    assert _count(st, A) == 0 and _count(st, B) == 0 and _count(st, KEEP) == 1
    assert not shared.exists() and own.exists()
    assert not book_tracks._manifest_path(A).exists() and book_tracks._manifest_path(KEEP).exists()
    assert deleted.is_deleted(A) and deleted.is_deleted(B) and not deleted.is_deleted(KEEP)
    assert items == ["item-Same", "missing-1"]
    backup = tmp_path / "backups"
    kept = [p for p in backup.rglob("history.json")]
    assert len(kept) == 2 and len(json.loads(kept[0].read_text())) >= 1
    assert list(backup.rglob("folder/0001 - hi.mp3"))
    assert report["sessions"][A]["backup"]
    # Nothing rebuilds it: the shelf export leaves a deleted session alone.
    assert book_tracks.export_session(A) == (None, 0)


def test_not_a_session_is_refused(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert forget.run(["../etc"], apply_=True) == 2
