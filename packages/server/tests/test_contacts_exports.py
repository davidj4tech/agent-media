"""The export cycle (contacts_exports.py, §6.25 "Exports"): export zips
picked up from the phone's Downloads or a folder, the reminder, and Meta's
"ready" mail. The phone is a fake adb script running over a tmp folder, the
mailbox a fake object; HOME, the config and state dirs are throwaway and no
MEDIA_* or MATRIX_* setting of this host leaks in."""

from __future__ import annotations

import email.message
import io
import json
import os
import time
import zipfile

import pytest

from agent_media_server import alerts, contacts, contacts_exports, matrix, timeline

DAY = 86400


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

    def no_mail(cfg):
        raise AssertionError("a real IMAP connection")
    monkeypatch.setattr(timeline, "_imap", no_mail)
    yield
    contacts._reset_for_tests()


def _friends_zip(*names) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("connections/friends/your_friends.json", json.dumps(
            {"friends_v2": [{"name": n, "timestamp": 1600000000 + i}
                            for i, n in enumerate(names)]}))
    return buf.getvalue()


def _phone(tmp_path, monkeypatch, fail: bool = False):
    """A fake adb whose shell runs the script over a tmp "phone" folder."""
    folder = tmp_path / "phone-download"
    folder.mkdir()
    script = tmp_path / "fake-adb"
    script.write_text("#!/bin/sh\nshift\n" + ("echo 'device offline' >&2; exit 1\n" if fail
                                              else 'exec sh -c "$1"\n'))
    script.chmod(0o755)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", f"{script} shell")
    monkeypatch.setattr(contacts_exports, "PHONE_DIR", str(folder))
    return folder


def _alert(aid):
    return next((a for a in alerts.listing()["alerts"] if a["id"] == aid), None)


def _people():
    conn = contacts._connect()
    try:
        return [json.loads(c)["name"] for (c,) in conn.execute("SELECT card FROM contacts")]
    finally:
        conn.close()


# --- files ---------------------------------------------------------------------------

def test_an_export_in_the_phones_downloads_is_imported_once_and_left_there(tmp_path, monkeypatch):
    folder = _phone(tmp_path, monkeypatch)
    f = folder / "facebook-david-2026-10-19-abc.zip"
    f.write_bytes(_friends_zip("Alex Chen", "Bo Li"))
    (folder / "holiday.zip").write_bytes(_friends_zip("Not Read"))

    got = contacts_exports.watch()
    assert got["imported"] == [f.name] and not got["errors"]
    assert sorted(_people()) == ["Alex Chen", "Bo Li"]
    assert f.exists()                       # the phone's copy stays
    said = _alert("contacts.import")
    assert said["kind"] == "digest" and "Facebook friends 2" in said["title"]
    assert "phone" in said["detail"] and "Alex" not in said["detail"] + said["title"]

    assert contacts_exports.watch()["imported"] == []     # seen: not again
    # A newer file (another day's export) is.
    g = folder / "facebook-david-2026-12-01-def.zip"
    g.write_bytes(_friends_zip("Alex Chen", "Bo Li", "Cy Ng"))
    assert contacts_exports.watch()["imported"] == [g.name]
    assert sorted(_people()) == ["Alex Chen", "Bo Li", "Cy Ng"]


def test_the_phone_off_wifi_is_skipped_and_said_once(tmp_path, monkeypatch):
    _phone(tmp_path, monkeypatch, fail=True)
    got = contacts_exports.watch()
    assert got["imported"] == [] and "offline" in got["errors"]["phone"]
    assert contacts_exports.load().get("files") is None


def test_no_adb_tool_and_no_folder_is_nothing(tmp_path):
    assert contacts_exports.watch() == {"imported": [], "skipped": 0, "errors": {}}


def test_a_folder_too_and_a_bad_file_is_tried_once(tmp_path, monkeypatch):
    d = tmp_path / "drop"
    d.mkdir()
    monkeypatch.setenv("MEDIA_CONTACTS_IMPORT_DIR", str(d))
    (d / "instagram-x-2026.zip").write_bytes(b"PK\x03\x04 not really")
    (d / "notes.txt").write_text("hi")
    got = contacts_exports.watch()
    assert got["imported"] == [] and got["skipped"] == 1
    assert "zip" in got["errors"]["instagram-x-2026.zip"]
    assert contacts_exports.watch()["errors"] == {}       # not retried
    (d / "your_friends.json").write_text(json.dumps({"friends_v2": [{"name": "Di Ho",
                                                                     "timestamp": 1}]}))
    assert contacts_exports.watch()["imported"] == ["your_friends.json"]
    assert _people() == ["Di Ho"]


def test_switched_off_and_never_for_another_book(tmp_path, monkeypatch):
    folder = _phone(tmp_path, monkeypatch)
    (folder / "facebook-a.zip").write_bytes(_friends_zip("Alex Chen"))
    monkeypatch.setenv("MEDIA_CONTACTS_EXPORT_WATCH", "0")
    assert contacts_exports.watch()["imported"] == []
    monkeypatch.delenv("MEDIA_CONTACTS_EXPORT_WATCH")
    with contacts.as_account("acct-" + "0" * 20):
        assert contacts_exports.run() == {"reminded": [], "mail": [], "watch": {}}
    assert _people() == []


def test_the_sync_loop_runs_it_for_the_owner_only(monkeypatch):
    calls = []
    monkeypatch.setattr(contacts_exports, "run", lambda: calls.append(contacts.account()) or {})
    monkeypatch.setattr(contacts, "sync", lambda: {"synced": [], "errors": {}})
    monkeypatch.setenv("MEDIA_TIMELINE", "0")
    with contacts.as_account(""):
        contacts._sync_book("")
    with contacts.as_account("acct-" + "1" * 20):
        contacts._sync_book("acct-" + "1" * 20)
    assert calls == [""]


# --- the reminder ----------------------------------------------------------------------

def test_the_reminder_waits_for_a_first_import_then_90_days(tmp_path):
    now = time.time()
    assert contacts_exports.remind(now=now) == []          # never imported: off
    contacts_exports.note_import(["facebook"], {}, now=now - 89 * DAY)
    assert contacts_exports.remind(now=now) == []
    contacts_exports.note_import(["facebook"], {}, now=now - 91 * DAY)
    assert contacts_exports.remind(now=now) == ["contacts.export.facebook"]
    a = _alert("contacts.export.facebook")
    assert a["open"] and a["level"] == "warn" and a["fix"] == contacts_exports.DYI_URL
    assert "JSON" in a["detail"] and "Friends and followers" in a["detail"]
    assert contacts_exports.remind(now=now) == []          # raised once
    contacts.import_export(_friends_zip("Alex Chen"), "your_friends.zip")
    assert not _alert("contacts.export.facebook")["open"]  # the import clears it


def test_the_reminder_interval_and_off(monkeypatch):
    now = time.time()
    contacts_exports.note_import(["instagram"], {}, now=now - 8 * DAY)
    monkeypatch.setenv("MEDIA_CONTACTS_EXPORT_REMIND_DAYS", "0")
    assert contacts_exports.remind(now=now) == []
    monkeypatch.setenv("MEDIA_CONTACTS_EXPORT_REMIND_DAYS", "7")
    assert contacts_exports.remind(now=now) == ["contacts.export.instagram"]


# --- the "ready" mail ----------------------------------------------------------------

def _mail(sender, subject, body, mid):
    m = email.message.EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = sender, subject, mid
    m["Date"] = "Sun, 19 Oct 2026 10:00:00 +1100"
    m.set_content(body)
    return m.as_bytes()


class Box:
    def __init__(self, mails):
        self.mails = mails            # {domain: [bytes]}
        self.fetched_bodies = 0

    def login(self, user, password):
        return "OK", [b""]

    def select(self, folder, readonly=False):
        assert readonly
        return "OK", [b"1"]

    def search(self, charset, *crit):
        dom = crit[1].strip('"')
        self._dom = dom
        n = len(self.mails.get(dom, []))
        return "OK", [" ".join(f"{dom}:{i}" for i in range(n)).encode()]

    def fetch(self, mid, what):
        dom, i = mid.decode().rsplit(":", 1)
        raw = self.mails[dom][int(i)]
        if "RFC822.SIZE" in what:
            head = raw.split(b"\n\n", 1)[0] + b"\n\n"
            return "OK", [(f"1 (RFC822.SIZE {len(raw)} BODY[HEADER] {{1}}".encode(), head)]
        self.fetched_bodies += 1
        return "OK", [(b"1 (BODY[] {1}", raw)]

    def logout(self):
        return "BYE", [b""]


@pytest.fixture
def mailbox(monkeypatch):
    monkeypatch.setenv("MEDIA_TIMELINE_IMAP", json.dumps(
        {"host": "imap.example", "user": "me@example.com", "password": "x"}))
    box = Box({})
    monkeypatch.setattr(timeline, "_imap", lambda cfg: box)
    return box


def test_the_mail_watch_is_off_until_asked(mailbox):
    mailbox.mails["facebookmail.com"] = [_mail(
        "Facebook <security@facebookmail.com>", "Your information is ready to download",
        "Get it: https://www.facebook.com/dyi/?x=1", "<a@fb>")]
    assert contacts_exports.mail() == []
    assert contacts_exports.load() == {}


def test_a_ready_mail_raises_with_metas_own_link(mailbox, monkeypatch):
    monkeypatch.setenv("MEDIA_CONTACTS_EXPORT_MAIL", "1")
    mailbox.mails["facebookmail.com"] = [
        _mail("Facebook <notification@facebookmail.com>", "Your Facebook information is ready",
              "Download here https://accountscenter.facebook.com/info_and_permissions/dyi/?id=9"
              " or see https://evil.example/download", "<1@fb>"),
        _mail("Facebook <notification@facebookmail.com>", "Someone liked your post",
              "https://www.facebook.com/x", "<2@fb>")]
    mailbox.mails["mail.instagram.com"] = [
        _mail("Instagram <no-reply@mail.instagram.com>", "Your Instagram data is ready",
              "Visit https://evil.example/dyi", "<3@ig>")]
    got = contacts_exports.mail()
    assert sorted(got) == ["contacts.export.ready.facebook", "contacts.export.ready.instagram"]
    fb = _alert("contacts.export.ready.facebook")
    assert fb["open"] and fb["fix"] == \
        "https://accountscenter.facebook.com/info_and_permissions/dyi/?id=9"
    ig = _alert("contacts.export.ready.instagram")
    assert ig["fix"] == contacts_exports.DYI_URL and "Accounts Center" in ig["detail"]
    assert contacts_exports.mail() == []          # each mail once
    contacts.import_export(_friends_zip("Alex Chen"), "your_friends.zip")
    assert not _alert("contacts.export.ready.facebook")["open"]
    assert _alert("contacts.export.ready.instagram")["open"]


def test_a_mail_not_from_meta_is_not_read(mailbox, monkeypatch):
    monkeypatch.setenv("MEDIA_CONTACTS_EXPORT_MAIL", "1")
    mailbox.mails["facebookmail.com"] = [_mail(
        "Phish <x@facebookmail.com.evil.example>", "Your information is ready",
        "https://www.facebook.com/dyi", "<p@x>")]
    assert contacts_exports.mail() == [] and mailbox.fetched_bodies == 0
