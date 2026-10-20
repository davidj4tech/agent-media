"""One email read inside Sasonica (mail_read.py, §6.25 "Reading one"):
headers, a body made safe, attachments, and reading aloud on a tap.
notmuch is a fake script that maps made-up ids to made-up .eml files in a
tmp dir; HOME and the state dirs are throwaway, no MEDIA_*/MATRIX_*
setting of this host leaks in, the real ~/Mail is never read, and nothing
is spoken (the say route's child is recorded, never started)."""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

from agent_media_server import contacts, mail_read, matrix, timeline
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_TIMELINE_NOTMUCH", str(tmp_path / "no-such-notmuch"))
    monkeypatch.setattr(matrix, "_ROOMS", {})
    timeline._NM.clear()
    contacts._reset_for_tests()
    yield
    timeline._NM.clear()
    contacts._reset_for_tests()


class Mail:
    """A fake notmuch: `count`, `config get`, and `search --output=files`
    for an `id:"…"` query, answered from the .eml files added."""

    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path / "nm"
        self.dir.mkdir()
        self.index = self.dir / "index.json"
        self.files: dict = {}
        self._save()
        script = self.dir / "notmuch"
        script.write_text(f"""#!{sys.executable}
import json, re, sys
args = sys.argv[1:]
idx = json.load(open({str(self.index)!r}))
if args[:1] == ["count"]:
    print(len(idx))
elif args[:2] == ["config", "get"]:
    print({{"user.primary_email": "david@example.com"}}.get(args[2], ""))
elif args[:1] == ["search"] and "--output=files" in args:
    m = re.fullmatch(r'id:"(.*)"', args[-1], re.S)
    mid = m.group(1).replace('""', '"') if m else None
    print(json.dumps([idx[mid]] if mid in idx else []))
else:
    sys.exit(1)
""")
        script.chmod(0o755)
        monkeypatch.setenv("MEDIA_TIMELINE_NOTMUCH", str(script))
        timeline._NM.clear()

    def _save(self):
        self.index.write_text(json.dumps(self.files))

    def add(self, mid: str, raw: str) -> str:
        p = self.dir / f"m{len(self.files)}.eml"
        p.write_bytes(raw.replace("\n", "\r\n").encode())
        self.files[mid] = str(p)
        self._save()
        return mid


@pytest.fixture
def mail(tmp_path, monkeypatch):
    return Mail(tmp_path, monkeypatch)


PLAIN = """From: "Lionel Smith" <lionel@example.com>
To: David <david@example.com>
Cc: ana@example.com
Subject: Lunch on Friday?
Date: Mon, 19 Oct 2026 10:00:00 +1100
Message-ID: <plain-1@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="B"

--B
Content-Type: multipart/alternative; boundary="A"

--A
Content-Type: text/plain; charset=utf-8

Hi David,
Friday at the café?

> an earlier mail, quoted
Lionel
--A
Content-Type: text/html; charset=utf-8

<p>Hi David,</p><img src="https://track.example/p.gif">
--A--

--B
Content-Type: application/pdf
Content-Disposition: attachment; filename="menu.pdf"
Content-Transfer-Encoding: base64

""" + base64.b64encode(b"%PDF-1.4 the menu").decode() + """
--B--
"""

HOSTILE = """From: Shop <news@shop.example>
To: david@example.com
Subject: Sale
Date: Mon, 19 Oct 2026 09:00:00 +1100
Message-ID: <html-1@example.com>
MIME-Version: 1.0
Content-Type: multipart/related; boundary="R"

--R
Content-Type: text/html; charset=utf-8

<html><head><title>t</title><style>body{background:url(https://x.example/s)}</style>
<script>alert(1)</script></head><body onload="evil()">
<p style="color:red" class="x" id="y">Hello <b>there</b></p>
<a href="javascript:alert(2)">bad link</a>
<a href="https://shop.example/sale" onclick="evil()">good link</a>
<img src="https://img.example/a.png" alt="Banner" onerror="evil()">
<img src="http://img.example/b.png">
<img src="cid:logo@x">
<iframe src="https://x.example"></iframe>
<form action="https://x.example"><input name="q"><button>Go</button></form>
<svg><script>alert(3)</script></svg>
<table><tr><td colspan="2" width="9">cell</td></tr></table>
</body></html>
--R
Content-Type: image/png
Content-ID: <logo@x>
Content-Transfer-Encoding: base64

""" + base64.b64encode(b"\x89PNG fake").decode() + """
--R--
"""


def test_plain_is_preferred_with_headers_and_attachments(mail):
    mid = mail.add("plain-1@example.com", PLAIN)
    got = mail_read.message(mid)
    assert got["subject"] == "Lunch on Friday?"
    assert got["from"] == [{"name": "Lionel Smith", "address": "lionel@example.com"}]
    assert got["to"][0]["address"] == "david@example.com" and got["cc"][0]["address"] == "ana@example.com"
    assert got["date"] == 1792364400 and got["mine"] is False
    assert "Friday at the café?" in got["text"] and got["html"] is None
    assert got["formats"] == ["text", "html"]
    assert got["attachments"] == [{"part": 2, "name": "menu.pdf", "type": "application/pdf",
                                   "size": len(b"%PDF-1.4 the menu")}]
    assert got["gmail_url"] and "rfc822msgid" in got["gmail_url"]


def test_the_formatted_view_when_asked(mail):
    mid = mail.add("plain-1@example.com", PLAIN)
    got = mail_read.message(mid, fmt="html")
    assert got["text"] is None and "<p>Hi David,</p>" in got["html"]
    assert "track.example" not in got["html"] and got["images_blocked"] == 1


def test_hostile_html_is_made_safe(mail):
    mid = mail.add("html-1@example.com", HOSTILE)
    got = mail_read.message(mid)
    h = got["html"]
    assert got["text"] is None and got["formats"] == ["html"]
    for bad in ("<script", "alert(", "onload", "onclick", "onerror", "style", "<style",
                "javascript:", "<iframe", "<form", "<input", "<button", "<svg",
                "x.example", 'class="x"', 'id="y"', "<title"):
        assert bad not in h, bad
    assert "Hello <b>there</b>" in h
    assert '<a href="https://shop.example/sale" rel="noopener noreferrer" target="_blank">' in h
    assert "<a>bad link</a>" in h
    # no picture from the web, nor the carried one, until asked
    assert "img.example" not in h and "<img" not in h and "[Banner]" in h
    assert got["images_blocked"] == 3
    assert '<td colspan="2">cell</td>' in h


def test_show_images_lets_https_and_carried_pictures_through(mail):
    mid = mail.add("html-1@example.com", HOSTILE)
    h = mail_read.message(mid, images=True)["html"]
    assert '<img src="https://img.example/a.png" alt="Banner">' in h
    assert "http://img.example/b.png" not in h            # plain http never
    assert 'src="data:image/png;base64,' in h              # the cid: one, as data
    assert "onerror" not in h and "<script" not in h


def test_unclosed_and_stray_tags_stay_balanced():
    safe, _ = mail_read.sanitise("<div><p>one<b>two</div></span><ul><li>x")
    assert safe == "<div><p>one<b>two</b></p></div><ul><li>x</li></ul>"
    safe, _ = mail_read.sanitise("<style>p{}</style><script>bad</script>ok")
    assert safe == "ok"


def test_an_attachment_and_what_is_not_one(mail):
    mid = mail.add("plain-1@example.com", PLAIN)
    data, ctype, name = mail_read.attachment(mid, 2)
    assert data == b"%PDF-1.4 the menu" and ctype == "application/pdf" and name == "menu.pdf"
    with pytest.raises(mail_read.NotFound):
        mail_read.attachment(mid, 1)                       # the text body
    with pytest.raises(mail_read.NotFound):
        mail_read.attachment(mid, 99)


def test_ids_quoted_and_unknown(mail):
    with pytest.raises(mail_read.NotFound):
        mail_read.message('x" or tag:inbox or id:"y')     # a space: not an id
    mid = mail.add('odd"id@example.com', PLAIN)            # a quote is doubled for notmuch
    assert mail_read.message(mid)["subject"] == "Lunch on Friday?"
    with pytest.raises(mail_read.NotFound):
        mail_read.message("nobody@example.com")


def test_spoken_leaves_out_quotes(mail):
    mid = mail.add("plain-1@example.com", PLAIN)
    text = mail_read.spoken(mid)
    assert text.startswith("From Lionel Smith. Lunch on Friday?.")
    assert "Friday at the café?" in text and "an earlier mail" not in text
    html_mid = mail.add("html-1@example.com", HOSTILE)
    said = mail_read.spoken(html_mid)
    assert "Hello there" in said and "alert" not in said and "body{" not in said


def test_nothing_is_written(mail, tmp_path):
    mid = mail.add("plain-1@example.com", PLAIN)
    before = {p for p in tmp_path.rglob("*")}
    mail_read.message(mid, images=True)
    mail_read.attachment(mid, 2)
    mail_read.spoken(mid)
    assert {p for p in tmp_path.rglob("*")} == before


def test_the_routes(server, signed_in, mail, monkeypatch):
    mid = mail.add("plain-1@example.com", PLAIN)
    q = "id=plain-1%40example.com"
    res, obj = call(server, "GET", f"/mail/message?{q}", headers=AUTH)
    assert res.status == 200 and obj["subject"] == "Lunch on Friday?"
    res, obj = call(server, "GET", f"/mail/message?{q}&format=html", headers=AUTH)
    assert res.status == 200 and obj["html"]
    res, obj = call(server, "GET", "/mail/message?id=", headers=AUTH)
    assert res.status == 400
    res, obj = call(server, "GET", "/mail/message?id=nobody%40x", headers=AUTH)
    assert res.status == 404
    res, raw = call(server, "GET", f"/mail/attachment?{q}&part=2", headers=AUTH)
    assert res.status == 200 and raw == b"%PDF-1.4 the menu"
    assert res.getheader("Content-Type") == "application/pdf"
    assert "menu.pdf" in res.getheader("Content-Disposition")
    res, raw = call(server, "GET", f"/mail/attachment?{q}&part=2&access_token=tok")
    assert res.status == 200 and raw == b"%PDF-1.4 the menu"
    res, _ = call(server, "GET", f"/mail/attachment?{q}&part=1", headers=AUTH)
    assert res.status == 404


def test_say_on_a_tap(server, signed_in, mail, monkeypatch):
    import subprocess

    mid = mail.add("plain-1@example.com", PLAIN)
    said = []

    real = subprocess.Popen

    class Child:
        """`media say` recorded, never started; notmuch runs for real."""

        def __new__(cls, args, **kw):
            if "say" not in list(args)[-2:]:
                return real(args, **kw)
            return super().__new__(cls)

        def __init__(self, args, **kw):
            said.append(args)
            self.stdin = self

        def write(self, b):
            said.append(b.decode())

        def close(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", Child)
    res, obj = call(server, "POST", "/mail/say", {"id": mid}, headers=AUTH)
    assert res.status == 200 and obj["chars"] > 0
    # Played, never kept in speech history (David, 20 Oct 2026).
    assert said[0][-2:] == ["say", "--private"] and said[1].startswith("From Lionel Smith.")
    res, _ = call(server, "POST", "/mail/say", {"id": ""}, headers=AUTH)
    assert res.status == 400


def test_another_account_and_no_token(server, signed_in, mail, monkeypatch):
    mail.add("plain-1@example.com", PLAIN)
    res, _ = call(server, "GET", "/mail/message?id=plain-1%40example.com")
    assert res.status in (401, 403)
    monkeypatch.setattr(contacts, "is_owner", lambda: False)
    res, obj = call(server, "GET", "/mail/message?id=plain-1%40example.com", headers=AUTH)
    assert res.status == 403 and "owner" in obj["error"]
