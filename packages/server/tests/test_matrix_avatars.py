"""Pictures in Matrix rooms (matrix.py, 20 Oct 2026): a room's own avatar on
its `/targets` row, each sender's on their messages with the contact they
are, and `GET /rooms/<thread>/avatar` serving both from the homeserver once.
A canned homeserver and a fake web; no network."""

import io
import json
import os
import stat

import pytest

from agent_media_core import matrix as core_matrix
from agent_media_server import auth, contacts, contacts_photos, devices, matrix
from test_contract import server, typed  # noqa: F401

ROOM = "!group:ryer.org"
ME, SAM = "@david:ryer.org", "@sam:ryer.org"
ZED, AMY = "@facebook_1001:ryer.org", "@facebook_1002:ryer.org"
HS = "http://hs.test"


def _image(colour=(200, 30, 30)) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return b"\xff\xd8\xff\xe0" + bytes(colour) * 50
    out = io.BytesIO()
    Image.new("RGB", (400, 400), colour).save(out, "JPEG")
    return out.getvalue()


def _ev(i, sender, body="hi"):
    return {"type": "m.room.message", "event_id": f"${i}", "sender": sender,
            "origin_server_ts": 1_790_000_000_000 + i * 1000,
            "content": {"msgtype": "m.text", "body": body}}


class Homeserver:
    def __init__(self, history, room_avatar="mxc://ryer.org/room"):
        self.history = history
        self.room_avatar = room_avatar

    def __call__(self, config, path, params=None, timeout=10.0):
        if path.endswith("/joined_members"):
            return {"joined": {ME: {"display_name": "David"},
                               SAM: {"display_name": "Sam"},
                               ZED: {"display_name": "Zed Facebook",
                                     "avatar_url": "mxc://ryer.org/zed"},
                               AMY: {"display_name": "Amy"}}}
        if path.endswith("/state"):
            st = [{"type": "m.bridge", "state_key": "x", "content": {"protocol": {
                "id": "facebookgo", "displayname": "Messenger"}}}]
            if self.room_avatar:
                st.append({"type": "m.room.avatar", "state_key": "",
                           "content": {"url": self.room_avatar}})
            return st
        if path.endswith("/state/m.room.name"):
            return {"name": "Walkers"}
        return {"chunk": list(reversed(self.history))}


class Web:
    def __init__(self):
        self.pages: dict[str, object] = {}
        self.got: list[str] = []

    def __call__(self, url, headers=None, max_bytes=None):
        self.got.append(url)
        for prefix, v in self.pages.items():
            if url.startswith(prefix):
                if isinstance(v, Exception):
                    raise v
                return v
        raise OSError(f"nothing at {url}")


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
    monkeypatch.setenv("MATRIX_SAM_ID", SAM)
    monkeypatch.setattr(matrix, "_ROOMS", {})
    monkeypatch.setattr(matrix, "_bridges", lambda: set())
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError("a real Google request")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    auth._REQ.__dict__.clear()
    contacts._reset_for_tests()


@pytest.fixture
def web(monkeypatch):
    w = Web()
    monkeypatch.setattr(contacts_photos, "_http", w)
    return w


def _room(**kw):
    cfg = core_matrix.Config(homeserver=HS, token="t", rooms={ROOM})
    r = matrix.Room(ROOM, cfg, ME, get=Homeserver([_ev(1, ZED), _ev(2, AMY), _ev(3, ZED)], **kw))
    matrix._ROOMS[r.thread] = r
    r.load()
    return r


def _ghost_book():
    conn = contacts._connect()
    try:
        card = {"name": "Zed Facebook", "names": [{"display": "Zed Facebook"}],
                "phones": [], "emails": [], "addresses": [], "organizations": [], "notes": "",
                "handles": [{"network": "facebookgo", "network_name": "Messenger", "id": ZED}]}
        conn.execute("INSERT INTO records (kind, sid, card, meta) VALUES (?, ?, ?, ?)",
                     ("bridges", ZED, json.dumps(card),
                      json.dumps({"network": "facebookgo", "rooms": [ROOM]})))
        contacts._rebuild(conn)
    finally:
        conn.close()
    for idx in contacts._INDEXES.values():
        idx["checked"] = 0.0
    return contacts.contact_id("bridges", ZED)


def test_the_row_carries_the_rooms_own_picture():
    r = _room()
    (row,) = matrix.rows()
    assert row["avatar"] == {"url": f"/rooms/{r.thread}/avatar",
                             "etag": matrix._etag("mxc://ryer.org/room")}


def test_a_room_with_no_picture_has_no_avatar():
    _room(room_avatar="")
    (row,) = matrix.rows()
    assert "avatar" not in row


def test_each_sender_carries_their_picture_and_card():
    zed = _ghost_book()
    r = _room()
    msgs, _ = r.page(30)
    z1, a, z3 = msgs
    assert z1["sender"]["avatar"]["url"] == \
        f"/rooms/{r.thread}/avatar?user=%40facebook_1001%3Aryer.org"
    assert z1["sender"]["contact"]["id"] == zed and z3["sender"]["contact"]["id"] == zed
    # Amy has no picture on the homeserver and no card here.
    assert "avatar" not in a["sender"] and "contact" not in a["sender"]


def test_the_owner_and_the_agent_are_never_a_contact():
    r = _room()
    assert contacts.for_member(r, ME) is None and contacts.for_member(r, SAM) is None


def test_the_picture_is_fetched_once_and_kept_private(web):
    r = _room()
    web.pages[f"{HS}/_matrix/client/v1/media/thumbnail/ryer.org/zed"] = _image()
    data, ctype, etag = matrix.avatar(r.thread, ZED)
    assert ctype.startswith("image/") and etag == matrix._etag("mxc://ryer.org/zed")
    n = len(web.got)
    assert matrix.avatar(r.thread, ZED)[0] == data and len(web.got) == n
    d = contacts_photos.photos_dir().parent / "room-avatars"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in d.iterdir())
    # No picture, an unknown room: none; a failure is not asked again soon.
    assert matrix.avatar(r.thread, AMY) is None
    assert matrix.avatar("00000000-0000-0000-0000-000000000000") is None
    web.pages = {f"{HS}/_matrix/client/v1/media": OSError("down"),
                 f"{HS}/_matrix/media": OSError("down")}
    assert matrix.avatar(r.thread) is None
    n = len(web.got)
    assert matrix.avatar(r.thread) is None and len(web.got) == n


def _get(addr, path, headers=None):
    import http.client

    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request("GET", path, headers=headers or {})
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res, body


def test_the_route(server, web):
    r = _room()
    web.pages[f"{HS}/_matrix/client/v1/media/thumbnail/ryer.org/room"] = _image((0, 0, 200))
    code, _ = devices.mint_code("desk")
    tok = devices.redeem(code, "desk")["token"]
    res, _ = _get(server, f"/rooms/{r.thread}/avatar")
    assert res.status in (401, 403)
    res, body = _get(server, f"/rooms/{r.thread}/avatar?access_token={tok}")
    assert res.status == 200 and res.getheader("Content-Type").startswith("image/")
    tag = res.getheader("ETag")
    res, _ = _get(server, f"/rooms/{r.thread}/avatar",
                  {"Authorization": f"Bearer {tok}", "If-None-Match": tag})
    assert res.status == 304
    res, _ = _get(server, f"/rooms/{r.thread}/avatar?user=%40facebook_1002%3Aryer.org",
                  {"Authorization": f"Bearer {tok}"})
    assert res.status == 404
