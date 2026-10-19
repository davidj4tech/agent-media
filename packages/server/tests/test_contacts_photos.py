"""People's pictures (contacts_photos.py, contract §6.25): Google's first,
then a bridge ghost's avatar; kept per book as a small file; served at
`GET /contacts/<id>/photo`. Every fetch goes to a fake (`_http` replaced),
never Google or a homeserver; HOME, config and state are throwaway, and no
MEDIA_* or MATRIX_* setting of this host leaks in."""

from __future__ import annotations

import io
import json
import os
import stat

import pytest

from agent_media_server import auth, contacts, contacts_photos, devices, matrix
from test_contract import call, server, typed  # noqa: F401

ISS = "https://cms.test"
HS = "http://hs.test"
FB = "@facebook_1001:ryer.org"
WA = "@whatsapp_61400111222:ryer.org"


def _image(colour=(200, 30, 30), size=(600, 400)) -> bytes:
    try:
        from PIL import Image
    except ImportError:                                  # a JPEG's magic is enough then
        return b"\xff\xd8\xff\xe0" + bytes(colour) * 50
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "JPEG")
    return out.getvalue()


RED, BLUE, GREEN = _image((200, 0, 0)), _image((0, 0, 200)), _image((0, 200, 0))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setenv("MATRIX_HOMESERVER", HS)
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "sam-token")
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    auth._REQ.__dict__.clear()
    contacts._reset_for_tests()


class Web:
    """The fake web: url prefix → bytes (or an exception); every fetch kept."""

    def __init__(self):
        self.pages: dict[str, object] = {}
        self.got: list[tuple[str, dict]] = []

    def __call__(self, url, headers=None, max_bytes=None):
        self.got.append((url, dict(headers or {})))
        for prefix, v in self.pages.items():
            if url.startswith(prefix):
                if isinstance(v, Exception):
                    raise v
                return v
        raise OSError(f"nothing at {url}")

    def avatar(self, user, mxc):
        q = contacts_photos.urllib.parse.quote(user, safe="")
        self.pages[f"{HS}/_matrix/client/v3/profile/{q}/avatar_url"] = \
            json.dumps({"avatar_url": mxc} if mxc else {}).encode()

    def media(self, mxc, data):
        server_, media = mxc[6:].split("/", 1)
        self.pages[f"{HS}/_matrix/client/v1/media/thumbnail/{server_}/{media}"] = data


@pytest.fixture
def web(monkeypatch):
    w = Web()
    monkeypatch.setattr(contacts_photos, "_http", w)
    return w


def _google(n, name, photo="", phones=()):
    return ("google", f"people/c{n}",
            {"name": name, "names": [{"display": name}],
             "phones": [{"value": contacts.e164(p, "AU"), "raw": p, "type": "Mobile"}
                        for p in phones],
             "emails": [], "addresses": [], "organizations": [], "notes": ""},
            {"etag": f"e{n}", "photo": photo})


def _ghost(uid, name, network="facebookgo", phones=()):
    return ("bridges", uid,
            {"name": name, "names": [{"display": name, "given": "", "family": ""}],
             "phones": [{"value": p, "raw": p, "type": network} for p in phones],
             "emails": [], "addresses": [], "organizations": [], "notes": "",
             "handles": [{"network": network, "network_name": network, "id": uid}]},
            {"network": network, "network_name": network, "rooms": ["!r:ryer.org"]})


def _book(*recs):
    conn = contacts._connect()
    try:
        for kind, sid, card, meta in recs:
            conn.execute("INSERT OR REPLACE INTO records (kind, sid, card, meta) "
                         "VALUES (?, ?, ?, ?)", (kind, sid, json.dumps(card), json.dumps(meta)))
        contacts._rebuild(conn)
    finally:
        conn.close()
    for idx in contacts._INDEXES.values():
        idx["checked"] = 0.0


LIONEL = contacts.contact_id("google", "people/c1")
MEL = contacts.contact_id("google", "people/c2")
ZED = contacts.contact_id("bridges", FB)


def _pic(cid):
    got = contacts_photos.photo(cid)
    return got and got[0]


# --- the sources ----------------------------------------------------------------

def test_googles_url_skips_a_default_and_asks_for_our_size():
    assert contacts_photos.google_url({"photos": [{"url": "https://lh3.test/a=s100",
                                                  "default": True}]}) == ""
    p = {"photos": [{"url": "https://lh3.test/other=s100"},
                    {"url": "https://lh3.test/mine=s100-p", "metadata": {"primary": True}}]}
    assert contacts_photos.google_url(p) == "https://lh3.test/mine=s256"
    assert contacts_photos.google_url({}) == ""


def test_google_first_then_the_ghost(web):
    web.pages["https://lh3.test/lionel"] = RED
    web.avatar(FB, "mxc://ryer.org/zed")
    web.media("mxc://ryer.org/zed", BLUE)
    _book(_google(1, "Lionel Smith", "https://lh3.test/lionel=s256", ["0412 345 678"]),
          _google(2, "Mel Jones"),
          _ghost(FB, "Zed Facebook"))
    got = contacts_photos.refresh()
    assert got["kept"] == 2 and got["none"] == 1 and got["failed"] == 0
    assert _pic(LIONEL) and _pic(ZED) and _pic(MEL) is None
    assert _pic(LIONEL) != _pic(ZED)
    # The ghost's picture came through authenticated media, as a thumbnail.
    thumb = [u for u, h in web.got if "/media/thumbnail/" in u]
    assert thumb and "width=256" in thumb[0]
    assert dict(web.got)[thumb[0]]["Authorization"] == "Bearer sam-token"


def test_a_person_known_both_ways_shows_googles(web):
    web.pages["https://lh3.test/w"] = RED
    web.avatar(WA, "mxc://ryer.org/wa")
    web.media("mxc://ryer.org/wa", GREEN)
    # The same number: the ghost joins the Google contact.
    _book(_google(1, "Lionel Smith", "https://lh3.test/w=s256", ["+61 400 111 222"]),
          _ghost(WA, "Lionel", "whatsapp", ["+61400111222"]))
    contacts_photos.refresh()
    c = contacts.card(LIONEL)
    assert {s["kind"] for s in c["sources"]} >= {"google", "bridges"}
    assert c["photo"]["url"] == f"/contacts/{LIONEL}/photo"
    data, ctype, etag, _ = contacts_photos.photo(LIONEL)
    assert data == contacts_photos.thumbnail(RED)[0] and etag == c["photo"]["etag"]


def test_kept_small_and_private(web):
    web.pages["https://lh3.test/l"] = RED
    _book(_google(1, "Lionel Smith", "https://lh3.test/l=s256"))
    contacts_photos.refresh()
    d = contacts_photos.photos_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    (f,) = [p for p in d.iterdir()]
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    try:
        from PIL import Image
    except ImportError:
        return
    im = Image.open(f)
    assert max(im.size) <= contacts_photos.SIZE and im.format == "JPEG"


# --- what is fetched again ------------------------------------------------------

def test_an_unchanged_url_is_not_fetched_again_a_new_one_is(web):
    web.pages["https://lh3.test/l1"] = RED
    web.pages["https://lh3.test/l2"] = BLUE
    _book(_google(1, "Lionel Smith", "https://lh3.test/l1=s256"))
    contacts_photos.refresh()
    first = _pic(LIONEL)
    n = len(web.got)
    assert contacts_photos.refresh()["fetched"] == 0 and len(web.got) == n
    _book(_google(1, "Lionel Smith", "https://lh3.test/l2=s256"))
    assert contacts_photos.refresh()["kept"] == 1
    assert _pic(LIONEL) != first
    # The old file goes once nothing points at it.
    assert len(list(contacts_photos.photos_dir().iterdir())) == 1
    # A picture taken away on Google: none any more.
    _book(_google(1, "Lionel Smith", ""))
    contacts_photos.refresh()
    assert _pic(LIONEL) is None and not list(contacts_photos.photos_dir().iterdir())


def test_a_ghost_is_looked_at_again_only_when_due(web):
    web.avatar(FB, "mxc://ryer.org/z1")
    web.media("mxc://ryer.org/z1", BLUE)
    _book(_ghost(FB, "Zed Facebook"))
    now = 1_000_000.0
    contacts_photos.refresh(now=now)
    n = len(web.got)
    assert contacts_photos.refresh(now=now + 3600)["fetched"] == 0 and len(web.got) == n
    # Due, the same avatar: its profile is read, the picture is not.
    got = contacts_photos.refresh(now=now + contacts_photos.RECHECK_S + 1)
    assert got["kept"] == 1 and not any("/media/" in u for u, _ in web.got[n:])
    # A new avatar: fetched.
    web.avatar(FB, "mxc://ryer.org/z2")
    web.media("mxc://ryer.org/z2", GREEN)
    contacts_photos.refresh(now=now + 2 * contacts_photos.RECHECK_S + 2)
    assert any("/z2" in u for u, _ in web.got)


def test_a_failure_waits_longer_each_time(web):
    web.pages["https://lh3.test/l"] = OSError("down")
    _book(_google(1, "Lionel Smith", "https://lh3.test/l=s256"))
    now = 1_000_000.0
    assert contacts_photos.refresh(now=now)["failed"] == 1
    assert contacts_photos.refresh(now=now + 60)["fetched"] == 0       # not yet
    assert contacts_photos.refresh(now=now + 901)["failed"] == 1       # 15 min on
    assert contacts_photos.refresh(now=now + 901 + 1000)["fetched"] == 0  # now 30 min
    web.pages["https://lh3.test/l"] = RED
    assert contacts_photos.refresh(now=now + 901 + 1801)["kept"] == 1
    assert _pic(LIONEL)


def test_not_an_image_is_no_picture(web):
    web.pages["https://lh3.test/l"] = b"<html>sign in</html>"
    _book(_google(1, "Lionel Smith", "https://lh3.test/l=s256"))
    assert contacts_photos.refresh()["failed"] == 1 and _pic(LIONEL) is None


def test_a_pass_fetches_at_most_its_budget(web):
    for i in range(5):
        web.pages[f"https://lh3.test/p{i}"] = _image((i * 40, 0, 0))
    _book(*[_google(i + 1, f"Person {i}", f"https://lh3.test/p{i}=s256") for i in range(5)])
    got = contacts_photos.refresh(budget=2)
    assert got["fetched"] == 2 and got["waiting"] == 3
    assert contacts_photos.refresh(budget=10)["kept"] == 3


def test_another_accounts_book_never_fetches_a_ghost(web):
    key = auth.account_key_of(ISS, "kat")
    with contacts.as_account(key):
        _book(_ghost(FB, "Zed Facebook"))
        assert contacts_photos.refresh()["fetched"] == 0
    assert not web.got


def test_googles_photos_are_asked_for_and_kept_in_the_record():
    assert "photos" in contacts.PERSON_FIELDS.split(",")


# --- the route ------------------------------------------------------------------

def _paired():
    code, _ = devices.mint_code("desk")
    return devices.redeem(code, "desk")["token"]


def _get(addr, path, headers=None):
    import http.client

    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request("GET", path, headers=headers or {})
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res, body


def test_the_route_serves_the_callers_book_only(server, web):
    web.pages["https://lh3.test/l"] = RED
    _book(_google(1, "Lionel Smith", "https://lh3.test/l=s256"), _google(2, "Mel Jones"))
    contacts_photos.refresh()
    tok = _paired()
    res, body = _get(server, f"/contacts/{LIONEL}/photo")
    assert res.status in (401, 403)
    res, body = _get(server, f"/contacts/{LIONEL}/photo", {"Authorization": f"Bearer {tok}"})
    assert res.status == 200 and res.getheader("Content-Type").startswith("image/")
    assert body == _pic(LIONEL) and res.getheader("Last-Modified")
    tag = res.getheader("ETag")
    res, _ = _get(server, f"/contacts/{LIONEL}/photo",
                  {"Authorization": f"Bearer {tok}", "If-None-Match": tag})
    assert res.status == 304
    # An <img> sends the token in the address.
    res, body = _get(server, f"/contacts/{LIONEL}/photo?access_token={tok}")
    assert res.status == 200 and body == _pic(LIONEL)
    res, _ = _get(server, f"/contacts/{MEL}/photo", {"Authorization": f"Bearer {tok}"})
    assert res.status == 404
    # The list and the card say where it is.
    res, obj = call(server, "GET", "/contacts", headers={"Authorization": f"Bearer {tok}"})
    rows = {c["id"]: c for c in obj["contacts"]}
    assert rows[LIONEL]["photo"] == {"url": f"/contacts/{LIONEL}/photo", "etag": tag.strip('"')}
    assert rows[MEL]["photo"] is None
    # Another account's device reads its own book: no Lionel there.
    kat = devices.enrol_account({"iss": ISS, "sub": "kat", "email": "k@x.test"}, "phone")["token"]
    res, _ = _get(server, f"/contacts/{LIONEL}/photo", {"Authorization": f"Bearer {kat}"})
    assert res.status == 404


# --- the larger picture ---------------------------------------------------------

BIG = _image((10, 120, 200), (2000, 1500))


def _side(data):
    try:
        from PIL import Image
    except ImportError:
        return None
    return max(Image.open(io.BytesIO(data)).size)


def test_a_larger_googles_is_fetched_when_first_asked_then_kept(web):
    web.pages["https://lh3.test/l"] = BIG
    _book(_google(1, "Lionel Smith", "https://lh3.test/l=s256"))
    contacts_photos.refresh()
    n = len(web.got)
    assert not (contacts_photos.photos_dir() / "large").exists()      # not up front
    data, ctype, etag, _ = contacts_photos.photo(LIONEL, "large")
    assert web.got[n][0] == "https://lh3.test/l=s1024"
    assert etag.startswith("L") and ctype.startswith("image/")
    if _side(data) is not None:
        assert _side(data) == contacts_photos.LARGE > _side(_pic(LIONEL))
    (f,) = list((contacts_photos.photos_dir() / "large").iterdir())
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    m = len(web.got)
    assert contacts_photos.photo(LIONEL, "large")[0] == data and len(web.got) == m


def test_a_ghosts_larger_one_is_a_large_thumbnail(web):
    web.avatar(FB, "mxc://ryer.org/zed")
    web.media("mxc://ryer.org/zed", BIG)
    _book(_ghost(FB, "Zed Facebook"))
    contacts_photos.refresh()
    n = len(web.got)
    assert contacts_photos.photo(ZED, "large")[2].startswith("L")
    assert any("/media/thumbnail/" in u and "width=1024" in u for u, _ in web.got[n:])


def test_no_larger_one_answers_the_thumbnail_and_is_not_asked_again_soon(web):
    web.pages["https://lh3.test/l"] = RED
    _book(_google(1, "Lionel Smith", "https://lh3.test/l=s256"))
    contacts_photos.refresh()
    web.pages = {"https://lh3.test/l": OSError("down")}
    data, _, etag, _ = contacts_photos.photo(LIONEL, "large")
    assert data == _pic(LIONEL) and not etag.startswith("L")
    n = len(web.got)
    contacts_photos.photo(LIONEL, "large")
    assert len(web.got) == n                                  # remembered for a day


def test_a_new_picture_drops_the_old_larger_one(web):
    web.pages["https://lh3.test/l1"] = BIG
    web.pages["https://lh3.test/l2"] = BLUE
    _book(_google(1, "Lionel Smith", "https://lh3.test/l1=s256"))
    contacts_photos.refresh()
    contacts_photos.photo(LIONEL, "large")
    _book(_google(1, "Lionel Smith", "https://lh3.test/l2=s256"))
    contacts_photos.refresh()
    assert not list((contacts_photos.photos_dir() / "large").iterdir())


def test_the_route_serves_the_larger_one(server, web):
    web.pages["https://lh3.test/l"] = BIG
    _book(_google(1, "Lionel Smith", "https://lh3.test/l=s256"))
    contacts_photos.refresh()
    tok = _paired()
    res, _ = _get(server, f"/contacts/{LIONEL}/photo?size=large")
    assert res.status in (401, 403)
    res, body = _get(server, f"/contacts/{LIONEL}/photo?size=large&access_token={tok}")
    assert res.status == 200 and body == contacts_photos.photo(LIONEL, "large")[0]
    tag = res.getheader("ETag")
    assert tag.startswith('"L')
    res, _ = _get(server, f"/contacts/{LIONEL}/photo?size=large",
                  {"Authorization": f"Bearer {tok}", "If-None-Match": tag})
    assert res.status == 304
    res, body = _get(server, f"/contacts/{LIONEL}/photo", {"Authorization": f"Bearer {tok}"})
    assert body == _pic(LIONEL)                                # the small one as before


# --- Find on Messenger (contacts_find.py) ----------------------------------------

from agent_media_server import bridges, contacts_find  # noqa: E402


class FakeBridge:
    """The Messenger bridge's provisioning API, faked: every search kept."""

    def __init__(self, results):
        self.results = results
        self.searches: list[dict] = []

    def call(self, b, method, path, body=None, query=None, timeout=10.0):
        assert (b.id, method, path) == ("facebook", "POST", "/search_users")
        self.searches.append(body)
        if isinstance(self.results, Exception):
            raise self.results
        return {"results": self.results}


@pytest.fixture
def bridge(monkeypatch, web):
    fake = FakeBridge([
        {"id": "1001", "name": "Lionel Smith", "mxid": FB, "avatar_url": "mxc://ryer.org/ls"},
        {"id": "1002", "name": "Lionel Smithers", "mxid": "@facebook_1002:ryer.org"},
        {"id": "1003", "name": "Lionel S", "mxid": "@facebook_1003:ryer.org"},
    ])
    b = bridges.Bridge(id="facebook", url="http://bridge.test", secret="s", homeserver=HS,
                       config="/x")
    monkeypatch.setattr(bridges, "find", lambda bid: b if bid == "facebook" else None)
    monkeypatch.setattr(bridges, "_call", fake.call)
    web.media("mxc://ryer.org/ls", RED)
    web.avatar(FB, "mxc://ryer.org/ls")
    return fake


def test_the_card_offers_find_when_no_messenger_handle(bridge):
    _book(_google(1, "Lionel Smith"), _ghost("@facebook_9:ryer.org", "Zed"))
    assert contacts.card(LIONEL)["find"] == ["messenger"]
    assert contacts.card(contacts.contact_id("bridges", "@facebook_9:ryer.org"))["find"] == []


def test_one_search_up_to_five_and_a_pick_only_for_one_exact_name(bridge):
    _book(_google(1, "Lionel Smith"))
    got = contacts_find.search(LIONEL, "messenger", now=1000.0)
    assert bridge.searches == [{"query": "Lionel Smith"}]
    assert [c["id"] for c in got["candidates"]] == ["1001", "1002", "1003"]
    assert got["pick"] == "1001"
    assert got["candidates"][0]["avatar"].startswith("data:image/")
    assert got["candidates"][1]["avatar"] is None
    # Two of that exact name: no pick.
    bridge.results = [{"id": "1", "name": "Lionel Smith"}, {"id": "2", "name": "lionel smith"}] + \
        [{"id": str(i), "name": f"L {i}"} for i in range(3, 9)]
    got = contacts_find.search(LIONEL, "messenger", now=1011.0)
    assert got["pick"] is None and len(got["candidates"]) == contacts_find.SHOW


def test_at_most_one_search_every_ten_seconds(bridge):
    _book(_google(1, "Lionel Smith"), _google(2, "Mel Jones"))
    contacts_find.search(LIONEL, "messenger", now=1000.0)
    with pytest.raises(contacts_find.TooSoon) as e:
        contacts_find.search(MEL, "messenger", now=1004.0)
    assert e.value.retry_after == 6.0 and len(bridge.searches) == 1
    contacts_find.search(MEL, "messenger", now=1010.5)
    assert len(bridge.searches) == 2


def test_a_pick_becomes_their_handle_and_picture(bridge):
    _book(_google(1, "Lionel Smith"))
    contacts_find.search(LIONEL, "messenger", now=time_now())
    with pytest.raises(LookupError):
        contacts_find.confirm(LIONEL, "messenger", "9999")       # not one of them
    got = contacts_find.confirm(LIONEL, "messenger", "1001")
    assert got == {"id": LIONEL, "handle": FB}
    c = contacts.card(LIONEL)
    assert any(h["id"] == FB and h["network"] == "facebookgo" for h in c["handles"])
    assert any(s["kind"] == "found" for s in c["sources"]) and c["find"] == []
    assert c["photo"] and contacts_photos.photo(LIONEL)[0] == contacts_photos.thumbnail(RED)[0]
    # The same ghost met in a room later lands on Lionel too, not on its own.
    _book(_ghost(FB, "Lionel Smith"))
    assert contacts.contact_id("bridges", FB) not in {r["id"] for r in contacts.listing()["contacts"]}
    # Kept through a rebuild, and the pick is gone (search again to change it).
    with pytest.raises(LookupError):
        contacts_find.confirm(LIONEL, "messenger", "1002")


def test_none_of_these_is_kept_and_the_action_goes(bridge):
    _book(_google(1, "Lionel Smith"))
    contacts_find.none_of_these(LIONEL, "messenger")
    assert contacts.card(LIONEL)["find"] == []


def test_a_bridge_that_cannot_search_says_so(bridge):
    bridge.results = bridges.BridgeError(400, "not logged in")
    _book(_google(1, "Lionel Smith"))
    with pytest.raises(contacts_find.Unavailable):
        contacts_find.search(LIONEL, "messenger")


def test_another_accounts_book_cannot_search(bridge):
    key = auth.account_key_of(ISS, "kat")
    with contacts.as_account(key):
        _book(_google(1, "Lionel Smith"))
        assert contacts.card(LIONEL)["find"] == []
        with pytest.raises(contacts_find.NotOwner):
            contacts_find.search(LIONEL, "messenger")
    assert not bridge.searches


def test_the_find_route(server, bridge):
    _book(_google(1, "Lionel Smith"), _google(2, "Mel Jones"))
    H = {"Authorization": f"Bearer {_paired()}"}
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/find", {"network": "messenger"})
    assert res.status in (401, 403) and not bridge.searches
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/find", {"network": "messenger"}, H)
    assert res.status == 200 and obj["pick"] == "1001" and len(obj["candidates"]) == 3
    res, obj = call(server, "POST", f"/contacts/{MEL}/find", {"network": "messenger"}, H)
    assert res.status == 429 and obj["retry_after"] > 0
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/find", {"network": "nope"}, H)
    assert res.status == 400
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/find",
                    {"network": "messenger", "confirm": "1001"}, H)
    assert res.status == 200 and obj["handle"] == FB
    res, obj = call(server, "POST", f"/contacts/{MEL}/find", {"network": "messenger", "none": True}, H)
    assert res.status == 200
    res, obj = call(server, "GET", f"/contacts/{MEL}", headers=H)
    assert obj["contact"]["find"] == []


def time_now():
    import time

    return time.time()
