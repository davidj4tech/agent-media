"""The bridged networks' people (contacts.py's `bridges` source) and "Same
person?" (§6.25). Rooms are made up `matrix.Room`s in `matrix._ROOMS`,
never a homeserver; HOME, the config dir and the state dir are throwaway,
and no MEDIA_* or MATRIX_* setting of this host leaks in."""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest

from agent_media_core import matrix as core_matrix
from agent_media_server import contacts, matrix, timeline
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401

ME, SAM = "@david:ryer.org", "@sam:ryer.org"
BOTS = "@gmessagesbot:ryer.org,@facebookbot:ryer.org,@instagrambot:ryer.org,@msteamsbot:ryer.org"


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


def _google(n, name, phones=(), emails=()):
    return ("google", f"people/c{n}",
            {"name": name, "names": [{"display": name, "given": "", "family": ""}],
             "phones": [{"value": contacts.e164(p, "AU"), "raw": p, "type": "Mobile"} for p in phones],
             "emails": [{"value": e, "type": "Home"} for e in emails],
             "addresses": [], "organizations": [], "notes": ""},
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
MEL = contacts.contact_id("google", "people/c2")
JO = contacts.contact_id("google", "people/c3")


@pytest.fixture
def book():
    _book(_google(1, "Lionel Smith", ["0412 345 678"]),
          _google(2, "Mel Jones", ["+61 400 111 222"]),
          _google(3, "Jo Work", emails=["jo@work.example"]))


def _at(day_offset=0, hour=12):
    d = dt.date.today() - dt.timedelta(days=day_offset)
    return dt.datetime.combine(d, dt.time(hour)).timestamp()


def _room(room_id, members, network, net_name, msgs=()):
    cfg = core_matrix.Config(homeserver="https://hs.test", token="t", rooms={room_id})
    r = matrix.Room(room_id, cfg, ME, get=lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    r.members = {ME: "David Ryer", **members}
    r.network = {"id": network, "name": net_name}
    r.messages = [{"id": f"${i}", "role": "user" if s == ME else "assistant", "at": at,
                   "parts": [{"type": "text", "text": "words"}],
                   "sender": {"id": s, "name": s}} for i, (at, s) in enumerate(msgs)]
    r.loaded = True
    matrix._ROOMS[r.thread] = r
    return r


FB_LIONEL = "@facebook_100012345678901:ryer.org"     # a Facebook id, not a number
FB_ANA = "@facebook_100099999999999:ryer.org"
IG_ME = "@instagram_178414000001:ryer.org"           # David's own Instagram ghost
IG_LIONEL = "@instagram_178414000002:ryer.org"
IG_B, IG_C = "@instagram_178414000003:ryer.org", "@instagram_178414000004:ryer.org"
SIG_MEL = "@signal_=2b61400111222:ryer.org"
TEAMS_JO = "@msteams_8=3aorgid=3aabc:ryer.org"
SMS_LIONEL = "@gmessages_1.42:ryer.org"


@pytest.fixture
def rooms(book):
    out = {
        "fb_lionel": _room("!fb1:ryer.org", {"@facebookbot:ryer.org": "bot", FB_LIONEL: "Lionel Smith"},
                           "facebookgo", "Facebook Messenger",
                           [(_at(0, 9), FB_LIONEL), (_at(0, 10), ME)]),
        "fb_ana": _room("!fb2:ryer.org", {FB_ANA: "Ana Stranger"}, "facebookgo", "Facebook Messenger"),
        "fb_group": _room("!fb3:ryer.org", {FB_ANA: "Ana Stranger", FB_LIONEL: "Lionel Smith",
                                            "@facebook_1000333:ryer.org": "Third Person"},
                          "facebookgo", "Facebook Messenger"),
        "ig_lionel": _room("!ig1:ryer.org", {IG_ME: "davidryer", IG_LIONEL: "Lionel Smith"},
                           "instagramgo", "Instagram", [(_at(0), IG_ME), (_at(0), IG_LIONEL)]),
        "ig_b": _room("!ig2:ryer.org", {IG_ME: "davidryer", IG_B: "Bee"}, "instagramgo", "Instagram"),
        "ig_c": _room("!ig3:ryer.org", {IG_ME: "davidryer", IG_C: "Cee"}, "instagramgo", "Instagram"),
        "sig_mel": _room("!s1:ryer.org", {SIG_MEL: "M"}, "signal", "Signal"),
        "teams_jo": _room("!t1:ryer.org", {"@msteamsbot:ryer.org": "bot", TEAMS_JO: "jo@work.example"},
                          "msteams", "Microsoft Teams"),
        "sms_lionel": _room("!g1:ryer.org", {SMS_LIONEL: "Lionel Smith"}, "gmessages",
                            "Google Messages"),
    }
    return out


def _people():
    return {c["name"]: c["id"] for c in contacts.listing(limit=1000)["contacts"]}


# --- who comes in --------------------------------------------------------------

def test_chats_bring_their_people_groups_with_a_label(rooms):
    got = contacts.sync_bridges()
    assert got["source"] == "bridges" and got["rooms"] == 9 and not got["unchanged"]
    people = _people()
    # Messenger's Ana is new; the group's Third Person comes in too (David,
    # 19 Oct 2026: a full list), labelled where David met them.
    assert "Ana Stranger" in people and "Third Person" in people
    third = contacts.card(people["Third Person"])
    assert third["labels"] == ["Messenger groups"]
    assert contacts.card(people["Ana Stranger"])["labels"] == []     # met one to one too
    assert contacts.card(JO)["labels"] == ["Teams"]                  # Teams is work
    assert contacts.for_room(rooms["fb_group"]) is None              # a group is no one's
    ana = contacts.card(people["Ana Stranger"])
    assert ana["handles"] == [{"network": "facebookgo", "network_name": "Facebook Messenger",
                               "id": FB_ANA}]
    assert ana["sources"] == [{"kind": "bridges", "id": FB_ANA, "network": "facebookgo",
                               "network_name": "Facebook Messenger"}]
    assert ana["phones"] == []                            # a Facebook id is not a number
    # David's own Instagram ghost is in every Instagram chat: not a person.
    assert "davidryer" not in people and {"Bee", "Cee"} <= set(people)
    assert contacts.sync_bridges()["unchanged"] is True


def test_a_shared_number_or_address_joins_by_itself(rooms):
    contacts.sync_bridges()
    mel = contacts.card(MEL)
    assert [s["kind"] for s in mel["sources"]] == ["google", "bridges"]
    assert mel["handles"][0]["id"] == SIG_MEL
    jo = contacts.card(JO)
    assert any(h["id"] == TEAMS_JO and h["network"] == "msteams" for h in jo["handles"])
    assert contacts.for_room(rooms["teams_jo"]) == {"id": JO, "name": "Jo Work"}
    assert contacts.for_room(rooms["sig_mel"]) == {"id": MEL, "name": "Mel Jones"}


def test_a_same_name_is_a_suggestion_not_a_join_but_sms_names_still_join(rooms):
    contacts.sync_bridges()
    people = contacts.listing("lionel")["contacts"]
    # Google's Lionel (with the SMS ghost, a trusted name), and Messenger's
    # and Instagram's Lionel, each their own until David says.
    assert len(people) == 3
    lionel = contacts.card(LIONEL)
    assert {h["id"] for h in lionel["handles"]} == {SMS_LIONEL}
    assert contacts.for_room(rooms["sms_lionel"])["id"] == LIONEL
    fb = contacts.contact_id("bridges", FB_LIONEL)
    assert contacts.for_room(rooms["fb_lionel"])["id"] == fb      # by its handle, not the name
    sug = {s["id"] for s in lionel["suggestions"]}
    assert sug == {fb, contacts.contact_id("bridges", IG_LIONEL)}
    assert all(s["why"] == "same name" for s in lionel["suggestions"])
    assert {s["id"] for s in contacts.card(fb)["suggestions"]} == {
        LIONEL, contacts.contact_id("bridges", IG_LIONEL)}


def test_same_joins_and_the_room_follows(rooms):
    contacts.sync_bridges()
    fb = contacts.contact_id("bridges", FB_LIONEL)
    assert contacts.same(fb, LIONEL) == {"id": LIONEL}
    assert contacts.card(fb) is None
    lionel = contacts.card(LIONEL)
    assert {h["id"] for h in lionel["handles"]} == {SMS_LIONEL, FB_LIONEL}
    assert lionel["phones"][0]["value"] == "+61412345678"          # Google's kept
    assert contacts.for_room(rooms["fb_lionel"])["id"] == LIONEL
    assert {r["room"] for r in lionel["rooms"]} == {"!fb1:ryer.org", "!g1:ryer.org"}
    # The other way round (the card is Lionel's, the other the bridged one).
    ig = contacts.contact_id("bridges", IG_LIONEL)
    assert contacts.same(LIONEL, ig) == {"id": LIONEL}
    assert contacts.card(LIONEL)["suggestions"] == []
    # It holds through the next read, and a rename of the ghost.
    rooms["fb_lionel"].members[FB_LIONEL] = "Li S"
    contacts.sync_bridges()
    assert contacts.for_room(rooms["fb_lionel"])["id"] == LIONEL


def test_two_bridged_people_can_be_one(rooms):
    contacts.sync_bridges()
    fb = contacts.contact_id("bridges", FB_LIONEL)
    ig = contacts.contact_id("bridges", IG_LIONEL)
    assert contacts.same(fb, ig) == {"id": ig}
    card = contacts.card(ig)
    assert {h["id"] for h in card["handles"]} == {FB_LIONEL, IG_LIONEL}
    assert contacts.for_room(rooms["fb_lionel"])["id"] == ig
    # Google's Lionel is still a suggestion for them.
    assert [s["id"] for s in card["suggestions"]] == [LIONEL]


def test_different_keeps_them_apart(rooms):
    contacts.sync_bridges()
    fb = contacts.contact_id("bridges", FB_LIONEL)
    contacts.different(LIONEL, fb)
    assert fb not in {s["id"] for s in contacts.card(LIONEL)["suggestions"]}
    assert LIONEL not in {s["id"] for s in contacts.card(fb)["suggestions"]}
    contacts.sync_bridges()
    assert fb not in {s["id"] for s in contacts.card(LIONEL)["suggestions"]}


def test_only_a_bridged_person_can_be_joined(rooms):
    contacts.sync_bridges()
    sms = contacts.contact_id("bridges", SMS_LIONEL)
    assert contacts.card(sms) is None                               # joined Lionel by name
    with pytest.raises(KeyError):
        contacts.same(LIONEL, sms)                                  # not a contact of its own
    with pytest.raises(contacts.NotBridged):
        contacts.same(LIONEL, MEL)                                  # neither is a bridge's alone


def test_no_rooms_in_this_process_keeps_the_last_copy(rooms, monkeypatch):
    contacts.sync_bridges()
    before = _people()
    monkeypatch.setattr(matrix, "_ROOMS", {})
    got = contacts.sync_bridges()
    assert got["rooms"] == 0 and got["unchanged"] and got["read"] > 0
    assert _people() == before


def test_another_book_reads_no_bridges(rooms):
    assert "bridges" in contacts.sources()
    key = "acct-0123456789abcdef0123"
    with contacts.as_account(key):
        assert "bridges" not in contacts.sources()
        assert contacts.sync_bridges()["read"] == 0
        assert contacts.for_room(rooms["fb_ana"]) is None


def test_the_timeline_counts_messenger_and_instagram(rooms):
    contacts.sync_bridges()
    fb = contacts.contact_id("bridges", FB_LIONEL)
    ig = contacts.contact_id("bridges", IG_LIONEL)
    today = dt.date.today().isoformat()
    rows = timeline.texts([today])
    assert (fb, "messenger", today, "in", 0) in rows and (fb, "messenger", today, "out", 0) in rows
    # David's own Instagram ghost writing is him: out, not in.
    assert sorted(x[3] for x in rows if x[0] == ig) == ["in", "out"]


# --- the routes ------------------------------------------------------------------

def test_the_routes(server, signed_in, rooms):
    contacts.sync_bridges()
    fb = contacts.contact_id("bridges", FB_LIONEL)
    res, obj = call(server, "GET", f"/contacts/{LIONEL}", headers=AUTH)
    assert res.status == 200 and fb in {s["id"] for s in obj["contact"]["suggestions"]}
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/different", {"other": fb}, headers=AUTH)
    assert res.status == 200 and obj == {"ok": True, "id": LIONEL}
    res, obj = call(server, "POST", f"/contacts/{fb}/same", {"other": LIONEL}, headers=AUTH)
    assert res.status == 200 and obj == {"ok": True, "id": LIONEL}
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/same", {"other": "nope"}, headers=AUTH)
    assert res.status == 400
    nobody = "00000000-0000-0000-0000-000000000000"
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/same", {"other": nobody}, headers=AUTH)
    assert res.status == 404
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/same", {"other": MEL}, headers=AUTH)
    assert res.status == 400 and "bridge" in obj["error"]


def test_the_routes_are_gated(server, rooms, monkeypatch):
    from agent_media_server import auth_abs

    contacts.sync_bridges()
    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    fb = contacts.contact_id("bridges", FB_LIONEL)
    res, _ = call(server, "POST", f"/contacts/{fb}/same", {"other": LIONEL})
    assert res.status in (401, 403)
    assert contacts.card(fb) is not None                            # nothing joined


def test_whatsapp_ghosts_number_or_lid():
    """mautrix-whatsapp names a ghost by the phone number (`@whatsapp_61412…`)
    or, for a contact WhatsApp hides behind a LID, `@whatsapp_lid-<digits>`:
    the first is a number, the second never is (David, 19 Oct 2026)."""
    from agent_media_server import contacts

    assert "+61412345678" in contacts.identities("@whatsapp_61412345678:ryer.org")["phones"]
    assert not contacts.identities("@whatsapp_lid-123456789012345:ryer.org")["phones"]
