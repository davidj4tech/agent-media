"""The /tmp watcher (24 Oct 2026): levels from fullness, and open files under
a root found through a fake /proc. Nothing reads the real /proc or /tmp, and
nothing is reported."""

from __future__ import annotations

import os

from agent_media_core.entrypoints import tmp_watch as w


def test_levels():
    assert w.level_for(50) == "ok"
    assert w.level_for(w.WARN_PCT) == "warn"
    assert w.level_for(w.NEEDS_PCT) == "needs"


def test_a_deleted_file_held_open_is_found_and_other_paths_are_not(tmp_path):
    root = tmp_path / "tmp"
    root.mkdir()
    # The kernel names a deleted file's fd link "<path> (deleted)"; a file
    # named that way stands in for it, so stat() through the link sizes it.
    held = root / "gone.bin (deleted)"
    held.write_bytes(b"x" * 4096)
    live = root / "live.bin"
    live.write_bytes(b"z" * 10)
    other = tmp_path / "elsewhere.bin"
    other.write_bytes(b"y")
    fds = tmp_path / "proc" / "123" / "fd"
    fds.mkdir(parents=True)
    os.symlink(str(held), fds / "3")
    os.symlink(str(live), fds / "4")
    os.symlink(str(other), fds / "5")
    rows = w.holders(str(root), str(tmp_path / "proc"))
    assert [(r["pid"], r["path"].rsplit("/", 1)[1], r["deleted"], r["size"]) for r in rows] == [
        (123, "gone.bin", True, 4096), (123, "live.bin", False, 10)]


def test_the_report_names_the_pid_and_size():
    text = w.describe(98.0, [{"pid": os.getpid(), "path": "/tmp/x", "deleted": True,
                              "size": 213 * 2**20}], ["900 MB /tmp/big"])
    assert "98% full" in text and "213 MB deleted /tmp/x" in text
    assert f"pid {os.getpid()}" in text and "900 MB /tmp/big" in text
