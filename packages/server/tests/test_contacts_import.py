"""People from a Facebook or Instagram data export (contacts_import.py, the
`import` source, `POST /contacts/import`, §6.25). The exports are small
made-up files; HOME, the config dir and the state dir are throwaway, and no
MEDIA_* or MATRIX_* setting of this host leaks in."""

from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

from agent_media_core import matrix as core_matrix
from agent_media_server import contacts, contacts_import, matrix
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401

ME, SAM = "@david:ryer.org", "@sam:ryer.org"
BOTS = "@facebookbot:ryer.org,@instagrambot:ryer.org"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MATRIX_SAM_ID", SAM)
    monkeypatch.setenv("MATRIX_OWNER_BRIDGES", BOTS)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    contacts._reset_for_tests()


def _google(n, name, phones=()):
    return ("google", f"people/c{n}",
            {"name": name, "names": [{"display": name, "given": "", "family": ""}],
             "phones": [{"value": contacts.e164(p, "AU"), "raw": p, "type": "Mobile"} for p in phones],
             "emails": [], "addresses": [], "organizations": [], "notes": ""},
            {"etag": f"e{n}"})


def _book(*recs):
    conn = contacts._connect()
    try:
        for kind, sid, card, meta in recs:
            conn.execute("INSERT OR REPLACE INTO records (kind, sid, card, meta) VALUES (?, ?, ?, ?)",
                         (kind, sid, json.dumps(card), json.dumps(meta)))
        contacts._rebuild(conn)
    finally:
        conn.close()
    contacts._INDEX["checked"] = 0.0


LIONEL = contacts.contact_id("google", "people/c1")


@pytest.fixture
def book():
    _book(_google(1, "Lionel Smith", ["0412 345 678"]))


def _mojibake(s: str) -> str:
    """How Facebook writes a name: each UTF-8 byte its own character."""
    return s.encode("utf-8").decode("latin-1")


FB_NEW = {"friends_v2": [{"name": "Lionel Smith", "timestamp": 1500000000},
                         {"name": _mojibake("José Núñez"), "timestamp": 1500000100},
                         {"name": "Ana Stranger", "timestamp": 1500000200}]}
FB_OLD = {"friends": [{"name": "Old Friend", "timestamp": 1300000000}]}
FB_LABELS = [{"timestamp": 1600000000,
              "label_values": [{"label": "Name", "value": "Label Person"}]}]
IG_FOLLOWERS = [{"title": "", "media_list_data": [],
                 "string_list_data": [{"href": "https://www.instagram.com/bee.b", "value": "bee.b",
                                       "timestamp": 1600000000}]},
                {"title": "", "media_list_data": [],
                 "string_list_data": [{"href": "https://www.instagram.com/both_ways",
                                       "value": "both_ways", "timestamp": 1600000001}]}]
IG_FOLLOWING = {"relationships_following": [
    {"title": "both_ways", "string_list_data": [{"href": "https://www.instagram.com/_u/both_ways",
                                                 "timestamp": 1600000002}]},
    {"title": "lionel.ig", "string_list_data": [{"href": "https://www.instagram.com/_u/lionel.ig",
                                                 "timestamp": 1600000003}]}]}


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, obj in files.items():
            z.writestr(path, obj if isinstance(obj, (str, bytes)) else json.dumps(obj))
    return buf.getvalue()


FB_ZIP = {"connections/friends/your_friends.json": FB_NEW,
          "connections/friends/removed_friends.json": {"deleted_friends_v2": [{"name": "Gone"}]},
          "connections/friends/sent_friend_requests.json": {"sent_requests_v2": [{"name": "Req"}]}}
IG_ZIP = {"connections/followers_and_following/followers_1.json": IG_FOLLOWERS,
          "connections/followers_and_following/following.json": IG_FOLLOWING,
          "connections/followers_and_following/close_friends.json": {"x": []}}


def _people() -> dict[str, str]:
    return {r["name"]: r["id"] for r in contacts.listing(limit=5000)["contacts"]}


# --- reading the exports -----------------------------------------------------------

def test_facebook_friends_from_the_zip_names_fixed():
    got, counts = contacts_import.parse(_zip(FB_ZIP), "facebook-david.zip")
    assert counts == {"facebook_friends": 3}
    names = sorted(r["card"]["name"] for r in got["facebook"].values())
    assert names == ["Ana Stranger", "José Núñez", "Lionel Smith"]   # not "JosÃ©"
    assert all(r["card"]["labels"] == ["Facebook friends"] for r in got["facebook"].values())


def test_older_and_newest_facebook_shapes_as_lone_files():
    got, counts = contacts_import.parse(json.dumps(FB_OLD).encode(), "friends.json")
    assert counts == {"facebook_friends": 1}
    got, counts = contacts_import.parse(json.dumps(FB_LABELS).encode(), "your_friends.json")
    assert [r["card"]["name"] for r in got["facebook"].values()] == ["Label Person"]


def test_instagram_followers_and_following_one_person_each():
    got, counts = contacts_import.parse(_zip(IG_ZIP), "instagram.zip")
    assert counts == {"instagram_followers": 2, "instagram_following": 2}
    both = got["instagram"]["instagram:both_ways"]
    assert both["card"]["labels"] == ["Instagram followers", "Instagram following"]
    assert both["card"]["handles"][0]["username"] == "both_ways"
    assert set(got["instagram"]) == {"instagram:bee.b", "instagram:both_ways",
                                     "instagram:lionel.ig"}
    # A lone followers file, its kind told by what it holds.
    got, counts = contacts_import.parse(json.dumps(IG_FOLLOWERS).encode(), "followers_1.json")
    assert counts == {"instagram_followers": 2}


def test_html_and_other_files_are_refused_with_why():
    with pytest.raises(contacts_import.NotAnExport, match="JSON"):
        contacts_import.parse(_zip({"connections/friends/your_friends.html": "<html>"}), "x.zip")
    with pytest.raises(contacts_import.NotAnExport, match="JSON"):
        contacts_import.parse(b"<html><body>", "your_friends.html")
    with pytest.raises(contacts_import.NotAnExport):
        contacts_import.parse(b'{"something": "else"}', "x.json")
    with pytest.raises(contacts_import.NotAnExport):
        contacts_import.parse(b"PK\x03\x04 not really", "x.zip")


# --- into the book -------------------------------------------------------------------

def test_an_import_adds_people_and_a_same_name_is_only_a_suggestion(book):
    got = contacts.import_export(_zip(FB_ZIP), "facebook.zip")
    assert got == {"imported": {"facebook_friends": 3}, "people": 3, "joined": 0,
                   "contacts": 4}
    people = _people()
    assert {"José Núñez", "Ana Stranger"} <= set(people)
    fb_lionel = next(cid for cid, c in ((r["id"], contacts.card(r["id"]))
                                        for r in contacts.listing(limit=50)["contacts"])
                     if c["name"] == "Lionel Smith" and cid != LIONEL)
    assert {s["id"] for s in contacts.card(LIONEL)["suggestions"]} == {fb_lionel}
    assert contacts.card(fb_lionel)["labels"] == ["Facebook friends"]
    assert contacts.labels()["labels"] == [{"name": "Facebook friends", "count": 3}]
    assert contacts.status()["import"]["records"] == 3


def test_reimport_replaces_and_a_join_holds(book):
    contacts.import_export(_zip(FB_ZIP), "facebook.zip")
    fb_lionel = [r["id"] for r in contacts.listing(q="Lionel")["contacts"] if r["id"] != LIONEL][0]
    assert contacts.same(fb_lionel, LIONEL) == {"id": LIONEL}
    newer = {"friends_v2": [f for f in FB_NEW["friends_v2"] if f["name"] != "Ana Stranger"]}
    got = contacts.import_export(_zip({"connections/friends/your_friends.json": newer}), "fb.zip")
    assert got["joined"] == 1 and got["people"] == 2
    people = _people()
    assert "Ana Stranger" not in people                     # unfriended: gone
    lionel = contacts.card(LIONEL)
    assert [s["kind"] for s in lionel["sources"]] == ["google", "import"]
    assert lionel["labels"] == ["Facebook friends"] and lionel["suggestions"] == []
    # An Instagram import leaves the Facebook one alone.
    contacts.import_export(_zip(IG_ZIP), "instagram.zip")
    assert "José Núñez" in _people() and "bee.b" in _people()


def _room(room_id, members, network, net_name):
    cfg = core_matrix.Config(homeserver="https://hs.test", token="t", rooms={room_id})
    r = matrix.Room(room_id, cfg, ME, get=lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    r.members = {ME: "David Ryer", **members}
    r.network = {"id": network, "name": net_name}
    r.messages = []
    r.loaded = True
    matrix._ROOMS[r.thread] = r
    return r


def test_an_instagram_username_joins_the_bridged_ghost_it_names(book):
    ghost = "@instagram_178414000009:ryer.org"
    _room("!ig9:ryer.org", {ghost: "lionel.ig"}, "instagramgo", "Instagram")
    contacts.sync_bridges()
    bridged = contacts.contact_id("bridges", ghost)
    got = contacts.import_export(_zip(IG_ZIP), "instagram.zip")
    assert got["joined"] == 1
    card = contacts.card(bridged)
    assert [s["kind"] for s in card["sources"]] == ["bridges", "import"]
    assert card["labels"] == ["Instagram following"]
    assert "lionel.ig" not in {n for n, cid in _people().items() if cid != bridged}


def test_another_book_keeps_its_own_import(book):
    with contacts.as_account("acct-0123456789abcdef0123"):
        contacts.import_export(_zip(IG_ZIP), "instagram.zip")
        assert "bee.b" in _people()
    assert "bee.b" not in _people()


# --- the route and the CLI ------------------------------------------------------------

def _post_raw(addr, path, data: bytes, headers: dict):
    import http.client

    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request("POST", path, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        return res, json.loads(raw)
    except ValueError:
        return res, raw


def test_the_route_raw_and_multipart(server, signed_in, book):
    res, obj = _post_raw(server, "/contacts/import?name=facebook.zip", _zip(FB_ZIP),
                    headers={**AUTH, "Content-Type": "application/zip"})
    assert res.status == 200 and obj["imported"] == {"facebook_friends": 3}
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    boundary = "xyzBOUNDARY"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"instagram.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode() \
        + _zip(IG_ZIP) + f"\r\n--{boundary}--\r\n".encode()
    res, obj = _post_raw(server, "/contacts/import", body,
                    headers={**AUTH, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert res.status == 200 and obj["imported"] == {"instagram_followers": 2,
                                                     "instagram_following": 2}
    res, obj = _post_raw(server, "/contacts/import", b"<html>",
                    headers={**AUTH, "Content-Type": "text/html"})
    assert res.status == 400 and obj["code"] == "not_an_export"


def test_the_route_is_gated(server, book, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, _ = _post_raw(server, "/contacts/import", _zip(FB_ZIP),
                  headers={"Content-Type": "application/zip"})
    assert res.status in (401, 403)
    assert "Ana Stranger" not in _people()


def test_the_cli(tmp_path, book, capsys):
    path = tmp_path / "facebook.zip"
    path.write_bytes(_zip(FB_ZIP))
    assert contacts.main(["import", str(path)]) == 0
    out = capsys.readouterr().out
    assert "3 facebook friends" in out and "Lionel" not in out          # counts, no names
    (tmp_path / "bad.json").write_text("{}")
    assert contacts.main(["import", str(tmp_path / "bad.json")]) == 2
