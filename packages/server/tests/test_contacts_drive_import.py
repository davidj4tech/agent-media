"""Meta's exports from Google Drive (contacts_exports.drive_import, §6.25
"The export cycle"): each new facebook-/instagram-/meta- zip copied, imported
and only then deleted on Drive. Drive is a fake rclone script over a tmp
folder; HOME, the config and state dirs are throwaway and no MEDIA_*,
MATRIX_* or RELAY_* setting of this host leaks in. Never the real Drive."""

from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

from agent_media_server import alerts, contacts, contacts_exports, matrix, timeline

FAKE_RCLONE = r'''#!/usr/bin/env python3
"""rclone, as far as drive_import uses it, over $FAKE_DRIVE (remote "fake:")."""
import datetime, json, os, shutil, sys
root = os.environ["FAKE_DRIVE"]
def local(p):
    assert p.startswith("fake:"), p
    return os.path.join(root, p[len("fake:"):].lstrip("/"))
args = sys.argv[1:]
with open(os.environ["FAKE_DRIVE_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")
cmd = args[0]
if cmd == "lsjson":
    base = local(args[-1]); out = []
    for d, _, files in os.walk(base):
        for f in files:
            full = os.path.join(d, f); st = os.stat(full)
            out.append({"Path": os.path.relpath(full, base), "Name": f, "Size": st.st_size,
                        "ModTime": datetime.datetime.fromtimestamp(
                            st.st_mtime, datetime.timezone.utc).isoformat(),
                        "ID": "id-" + f})
    print(json.dumps(out))
elif cmd == "copyto":
    if os.environ.get("FAKE_DRIVE_COPY_FAILS"):
        print("ERROR : copy refused", file=sys.stderr); sys.exit(1)
    shutil.copyfile(local(args[1]), args[2])
elif cmd == "deletefile":
    os.remove(local(args[1]))
else:
    print("unknown " + cmd, file=sys.stderr); sys.exit(2)
'''


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_ALERTS_INBOX", "0")
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    monkeypatch.setattr(timeline, "_imap", lambda cfg: (_ for _ in ()).throw(
        AssertionError("a real IMAP connection")))
    yield
    contacts._reset_for_tests()


@pytest.fixture
def drive(tmp_path, monkeypatch):
    folder = tmp_path / "drive"
    (folder / "Meta exports").mkdir(parents=True)
    script = tmp_path / "fake-rclone"
    script.write_text(FAKE_RCLONE)
    script.chmod(0o755)
    monkeypatch.setenv("MEDIA_RCLONE", str(script))
    monkeypatch.setenv("FAKE_DRIVE", str(folder))
    monkeypatch.setenv("FAKE_DRIVE_LOG", str(tmp_path / "rclone.log"))
    monkeypatch.setenv("MEDIA_CONTACTS_DRIVE_REMOTE", "fake:")
    monkeypatch.setenv("MEDIA_CONTACTS_DRIVE_DIR", "Meta exports")
    return folder / "Meta exports"


def _friends_zip(*names) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("connections/friends/your_friends.json", json.dumps(
            {"friends_v2": [{"name": n, "timestamp": 1600000000 + i}
                            for i, n in enumerate(names)]}))
    return buf.getvalue()


def _people():
    conn = contacts._connect()
    try:
        return sorted(json.loads(c)["name"] for (c,) in conn.execute("SELECT card FROM contacts"))
    finally:
        conn.close()


def _log(tmp_path):
    p = tmp_path / "rclone.log"
    return p.read_text().splitlines() if p.exists() else []


def test_an_export_is_imported_then_deleted_on_drive(drive, tmp_path):
    (drive / "facebook-davidj4-19_10_2026-abc.zip").write_bytes(
        _friends_zip("Alex Chen", "Bea Ruiz"))
    (drive / "notes.txt").write_text("not an export")
    got = contacts_exports.drive_import()
    assert [f["name"] for f in got["imported"]] == ["facebook-davidj4-19_10_2026-abc.zip"]
    assert got["imported"][0]["imported"] == {"facebook_friends": 2}
    assert got["errors"] == {} and got["kept"] == {}
    assert _people() == ["Alex Chen", "Bea Ruiz"]
    # Deleted on Drive only after the import, and the other file untouched.
    assert not (drive / "facebook-davidj4-19_10_2026-abc.zip").exists()
    assert (drive / "notes.txt").exists()
    log = _log(tmp_path)
    assert [line.split()[0] for line in log] == ["lsjson", "copyto", "deletefile"]
    # Nothing left behind here, and the digest said counts, never a name.
    tmp = contacts.account_state_dir() / "drive-import"
    assert list(tmp.iterdir()) == [] and oct(tmp.stat().st_mode & 0o777) == "0o700"
    digest = next(a for a in alerts.listing()["alerts"] if a["id"] == "contacts.import")
    text = json.dumps(digest)
    assert "Facebook friends 2" in text and "Google Drive" in text
    assert "Alex" not in text and "Bea" not in text


def test_a_file_that_is_not_an_export_stays_on_drive_and_is_said_once(drive, tmp_path):
    bad = drive / "meta-2026-10-19-xyz.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("index.html", "<html>an HTML export</html>")
    bad.write_bytes(buf.getvalue())
    got = contacts_exports.drive_import()
    assert got["imported"] == [] and "meta-2026-10-19-xyz.zip" in got["kept"]
    assert "in that zip" in got["kept"]["meta-2026-10-19-xyz.zip"]
    assert bad.exists()                                   # never deleted
    assert not any(line.startswith("deletefile") for line in _log(tmp_path))
    again = contacts_exports.drive_import()               # said once, not every day
    assert again == {"imported": [], "kept": {}, "skipped": {}, "errors": {}}


def test_a_copy_that_fails_is_tried_again_next_run(drive, monkeypatch):
    f = drive / "instagram-davidjtwo-2026-10-19-q.zip"
    f.write_bytes(_friends_zip("Cam Lee"))
    monkeypatch.setenv("FAKE_DRIVE_COPY_FAILS", "1")
    got = contacts_exports.drive_import()
    assert "copy failed" in got["errors"]["instagram-davidjtwo-2026-10-19-q.zip"]
    assert f.exists()
    monkeypatch.delenv("FAKE_DRIVE_COPY_FAILS")
    got = contacts_exports.drive_import()
    assert [x["name"] for x in got["imported"]] == ["instagram-davidjtwo-2026-10-19-q.zip"]
    assert not f.exists()


def test_a_file_seen_before_is_skipped(drive, tmp_path):
    (drive / "facebook-a.zip").write_bytes(_friends_zip("Dee Moss"))
    contacts_exports.drive_import()
    # The same name back (say Drive kept a copy we could not delete): by id,
    # size and time it is the one already imported.
    state = contacts_exports.load()
    (drive / "facebook-a.zip").write_bytes(_friends_zip("Dee Moss"))
    mark = next(iter(state["drive"].values()))["mark"]
    st = (drive / "facebook-a.zip").stat()
    os.utime(drive / "facebook-a.zip", (st.st_atime, int(mark.split(":")[0])))
    n = len(_log(tmp_path))
    got = contacts_exports.drive_import()
    assert got["imported"] == [] and (drive / "facebook-a.zip").exists()
    assert [line.split()[0] for line in _log(tmp_path)[n:]] == ["lsjson"]


def test_a_file_too_big_for_the_disk_is_skipped_and_left(drive, monkeypatch, tmp_path):
    (drive / "facebook-big.zip").write_bytes(_friends_zip("Eli Ward"))
    monkeypatch.setattr(contacts_exports, "_free",
                        lambda folder: contacts_exports.DRIVE_SPARE + 10)
    got = contacts_exports.drive_import()
    assert "will not fit" in got["skipped"]["facebook-big.zip"]
    assert (drive / "facebook-big.zip").exists()
    assert [line.split()[0] for line in _log(tmp_path)] == ["lsjson"]   # never copied


def test_newest_first_and_only_meta_names(drive):
    import time as _t
    for i, n in enumerate(("facebook-old.zip", "facebook-new.zip")):
        p = drive / n
        p.write_bytes(_friends_zip(f"Person {i}"))
        os.utime(p, (_t.time(), 1_700_000_000 + i * 1000))
    (drive / "takeout-1.zip").write_bytes(b"PK\x03\x04")
    names = [f["name"] for f in contacts_exports.drive_files()]
    assert names == ["facebook-new.zip", "facebook-old.zip"]


def test_another_book_gets_nothing(drive, monkeypatch):
    (drive / "facebook-x.zip").write_bytes(_friends_zip("Fay Cole"))
    monkeypatch.setattr(contacts, "is_owner", lambda: False)
    got = contacts_exports.drive_import()
    assert got["imported"] == [] and "owner" in got["errors"]["book"]
    assert (drive / "facebook-x.zip").exists()


def test_the_command(drive, capsys):
    (drive / "facebook-cmd.zip").write_bytes(_friends_zip("Gus Hall"))
    assert contacts.main(["drive-import"]) == 0
    out = capsys.readouterr().out
    assert "facebook-cmd.zip: imported 1 facebook friends" in out and "Gus" not in out
    assert contacts.main(["drive-import"]) == 0
    assert "nothing new under fake:Meta exports" in capsys.readouterr().out
