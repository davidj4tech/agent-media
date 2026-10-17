"""Contacts (contacts.py, §6.25): the phone normaliser, Google's People API
through a fake (never the real one), the phone-friendly sign-in, the match
of Matrix rooms to people, and the two routes. No network but a loopback
listener of the test's own; no real homeserver, phone or canvas; HOME and
the config dir are throwaway."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import urllib.parse
import urllib.request

import pytest

from agent_media_core import matrix as core_matrix
from agent_media_server import contacts, matrix
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401

ME, SAM = "@david:ryer.org", "@sam:ryer.org"
BOT = "@gmessagesbot:ryer.org"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_GOOGLE")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("MATRIX_SAM_ID", SAM)
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()
    # Nothing may reach Google: a test that forgets its fake fails loudly.
    def nowhere(*a, **k):
        raise AssertionError(f"a real request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    contacts._reset_for_tests()


# --- phone numbers --------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("0412 345 678", "+61412345678"),
    ("0412-345-678", "+61412345678"),
    ("(03) 9876 5432", "+61398765432"),
    ("412 345 678", "+61412345678"),
    ("+61 412 345 678", "+61412345678"),
    ("+61 (0)412 345 678", "+61412345678"),
    ("+61 0412 345 678", "+61412345678"),
    ("61412345678", "+61412345678"),
    ("0011 44 20 7946 0958", "+442079460958"),
    ("0044 20 7946 0958", "+442079460958"),
    ("+1 (415) 555-2671", "+14155552671"),
    ("tel:+61412345678", "+61412345678"),
    ("0412 345 678 ext 12", "+61412345678"),
    ("1300 123 456", "+611300123456"),
    ("13 12 34", "+61131234"),
    ("9876 5432", None),               # no area code
    ("1-800-FLOWERS", None),
    ("", None),
    (None, None),
    ("+0123", None),
])
def test_e164_au(raw, want):
    assert contacts.e164(raw, "AU") == want


def test_e164_other_regions():
    assert contacts.e164("(415) 555-2671", "US") == "+14155552671"
    assert contacts.e164("020 7946 0958", "GB") == "+442079460958"
    assert contacts.e164("021 123 4567", "NZ") == "+64211234567"
    # International as written, whatever the default.
    assert contacts.e164("+61412345678", "US") == "+61412345678"


# --- who a room member is -------------------------------------------------------

def test_identities_of_real_puppet_shapes():
    ids = contacts.identities
    # mautrix-gmessages: `gmessages_<login>.<participant>` — no number in the
    # id; the displayname is the phone's contact name, else the number.
    assert ids("@gmessages_1.42:ryer.org", "Lionel Smith") == \
        {"phones": set(), "emails": set(), "name": "lionel smith"}
    assert ids("@gmessages_1.963:ryer.org", "+61 412 345 678")["phones"] == {"+61412345678"}
    assert ids("@gmessages_1.19:ryer.org", "0412 345 678")["phones"] == {"+61412345678"}
    # mautrix-signal: a uuid (no number), or the escaped number in old ones.
    sig = ids("@signal_8c3a1f2e-3b4c-4d5e-9f60-718293a4b5c6:ryer.org", "Mel")
    assert sig["phones"] == set() and sig["name"] == "mel"
    assert ids("@signal_=2b61412345678:ryer.org", "")["phones"] == {"+61412345678"}
    # mautrix-whatsapp: the number's digits.
    assert ids("@whatsapp_61412345678:ryer.org", "Lionel")["phones"] == {"+61412345678"}
    # An email for a displayname; a plain Matrix user with no displayname.
    assert ids("@gmessages_1.7:ryer.org", "Lionel@Example.com")["emails"] == {"lionel@example.com"}
    assert ids("@mel:ryer.org", "@mel:ryer.org") == {"phones": set(), "emails": set(), "name": ""}


# --- a fake People API ----------------------------------------------------------

def _person(n, name, phones=(), emails=(), etag=None, deleted=False, **extra):
    p = {"resourceName": f"people/c{n}", "etag": etag or f"e{n}",
         "metadata": {"deleted": True} if deleted else {"sources": [{"type": "CONTACT"}]}}
    if deleted:
        return p
    p["names"] = [{"displayName": name, "givenName": name.split()[0],
                   "familyName": name.split()[-1], "metadata": {"primary": True}}]
    p["phoneNumbers"] = [{"value": v, "type": "mobile", "formattedType": "Mobile"} for v in phones]
    p["emailAddresses"] = [{"value": v, "type": "home", "formattedType": "Home"} for v in emails]
    p.update(extra)
    return p


class Google:
    """The token endpoint and `people/me/connections`, from a list of people
    and a script of what changes."""

    def __init__(self, people, page=2):
        self.people = list(people)
        self.page = page
        self.changes: list = []           # what an incremental sync answers
        self.calls: list = []
        self.expired = False
        self.refreshes = 0
        self.unauthorised_once = False
        self.token = "rt-1"
        self.exchanged: list = []

    def __call__(self, method, url, *, form=None, headers=None, timeout=20.0):
        self.calls.append((method, url, form, headers))
        if url == contacts.TOKEN_URI:
            if form["grant_type"] == "authorization_code":
                self.exchanged.append(form)
                if form["code"] != "4/good-code":
                    return 400, {"error": "invalid_grant"}
                return 200, {"access_token": "at-0", "expires_in": 3599,
                             "refresh_token": self.token, "scope": contacts.SCOPE}
            assert form["grant_type"] == "refresh_token"
            if form["refresh_token"] != self.token:
                return 400, {"error": "invalid_grant"}
            self.refreshes += 1
            return 200, {"access_token": f"at-{self.refreshes}", "expires_in": 3599}
        u = urllib.parse.urlsplit(url)
        assert u.netloc == "people.googleapis.com" and u.path == "/v1/people/me/connections"
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        assert q["personFields"] == contacts.PERSON_FIELDS and q["requestSyncToken"] == "true"
        assert headers["Authorization"].startswith("Bearer at-")
        if self.unauthorised_once:
            self.unauthorised_once = False
            return 401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}}
        if q.get("syncToken"):
            if self.expired:
                return 410, {"error": {"code": 410, "status": "GONE", "message": "expired",
                                       "details": [{"reason": "EXPIRED_SYNC_TOKEN"}]}}
            assert q["syncToken"] == "sync-1"
            return 200, {"connections": self.changes, "nextSyncToken": "sync-2"}
        start = int(q.get("pageToken") or 0)
        chunk = self.people[start:start + self.page]
        more = start + self.page < len(self.people)
        return 200, {"connections": chunk, "totalPeople": len(self.people),
                     **({"nextPageToken": str(start + self.page)} if more
                        else {"nextSyncToken": "sync-1"})}


CLIENT = {"installed": {"client_id": "cid.apps.googleusercontent.com",
                        "client_secret": "csecret", "project_id": "p",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"]}}


def _client_file():
    p = contacts.client_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(CLIENT))
    return p


@pytest.fixture
def google(monkeypatch):
    """Signed in, with a fake Google holding three people."""
    _client_file()
    contacts._save_token({"refresh_token": "rt-1", "scope": contacts.SCOPE})
    g = Google([
        _person(1, "Lionel Smith", ["0412 345 678"], ["lionel@example.com"],
                addresses=[{"formattedValue": "1 Main Rd, Donvale VIC 3111",
                            "formattedType": "Home"}],
                biographies=[{"value": "Met at the market."}]),
        _person(2, "Mel Jones", ["+61 400 111 222"]),
        _person(3, "Zoë Brown", [], ["zoe@example.com"]),
    ])
    monkeypatch.setattr(contacts, "_request", g)
    return g


# --- the sync -------------------------------------------------------------------

def test_full_sync_pages_and_keeps_everyone(google):
    got = contacts.sync()
    assert got == {"full": True, "changed": 3, "deleted": 0, "contacts": 3}
    pages = [c for c in google.calls if "connections" in c[1]]
    assert len(pages) == 2 and "pageToken=2" in pages[1][1]
    rows = contacts.listing()["contacts"]
    assert [r["name"] for r in rows] == ["Lionel Smith", "Mel Jones", "Zoë Brown"]
    assert rows[0]["phones"] == ["+61412345678"] and rows[0]["emails"] == ["lionel@example.com"]
    assert contacts.db_path().parent == contacts.db_path().parent  # under the state dir
    assert str(contacts.db_path()).startswith(os.environ["XDG_STATE_HOME"])


def test_incremental_sync_takes_changes_and_deletions(google):
    contacts.sync()
    google.changes = [_person(2, "Mel Jones-Park", ["+61 400 111 222"], etag="e2b"),
                      _person(3, "", deleted=True)]
    got = contacts.sync()
    assert got == {"full": False, "changed": 1, "deleted": 1, "contacts": 2}
    assert "syncToken=sync-1" in google.calls[-1][1]
    names = [r["name"] for r in contacts.listing()["contacts"]]
    assert names == ["Lionel Smith", "Mel Jones-Park"]


def test_an_expired_sync_token_is_a_full_resync(google):
    contacts.sync()
    google.expired = True
    google.people = google.people[:2]          # Zoë went while the token aged
    got = contacts.sync()
    assert got["full"] is True and got["deleted"] == 1 and got["contacts"] == 2


def test_an_access_token_google_ended_early_is_refreshed_once(google):
    contacts.sync()
    google.unauthorised_once = True
    contacts._ACCESS["expires"] = 10 ** 12      # still "current" here
    contacts.sync(full=True)
    assert google.refreshes == 2


def test_a_revoked_sign_in_says_so(google):
    google.token = "rt-other"                   # Google no longer takes ours
    with pytest.raises(contacts.NotSignedIn, match="login again|run login"):
        contacts.sync()
    assert "no longer takes" in contacts.status()["last_error"]


def test_card_of_reads_googles_fields():
    c = contacts.card_of(_person(9, "Ann Lee", ["9876 5432"], ["a@x.io"],
                                 phoneNumbers=[{"value": "(03) 9876 5432",
                                                "canonicalForm": "+61398765432",
                                                "formattedType": "Work"},
                                               {"value": "5555"}],
                                 addresses=[{"streetAddress": "2 High St", "city": "Kew",
                                             "postalCode": "3101", "type": "home"}]))
    assert c["name"] == "Ann Lee" and c["names"][0] == {"display": "Ann Lee", "given": "Ann",
                                                        "family": "Lee"}
    assert c["phones"] == [{"value": "+61398765432", "raw": "(03) 9876 5432", "type": "Work"},
                           {"value": None, "raw": "5555", "type": ""}]
    assert c["addresses"] == [{"formatted": "2 High St, Kew, 3101", "type": "home"}]
    assert contacts.card_of({"resourceName": "people/c0"})["name"] == "(no name)"


# --- searching ------------------------------------------------------------------

def test_as_you_type(google):
    contacts.sync()
    q = lambda s: [r["name"] for r in contacts.listing(s)["contacts"]]  # noqa: E731
    assert q("li") == ["Lionel Smith"]
    assert q("jon") == ["Mel Jones"]                 # a word's start
    assert q("zoe") == ["Zoë Brown"]                 # accents folded
    assert q("0412") == ["Lionel Smith"]             # dialled at home
    assert q("+61400") == ["Mel Jones"]
    assert q("345 678") == ["Lionel Smith"]
    assert q("example.com") == ["Lionel Smith", "Zoë Brown"]
    assert q("nobody") == []
    out = contacts.listing("", limit=1)
    assert len(out["contacts"]) == 1 and out["total"] == 3 and out["signed_in"] is True
    assert isinstance(out["synced_at"], float)


def test_no_store_is_no_contacts_and_makes_none():
    out = contacts.listing("x")
    assert out == {"contacts": [], "total": 0, "signed_in": False, "synced_at": None}
    assert contacts.card("3f0c0000-0000-0000-0000-000000000000") is None
    assert not contacts.db_path().exists()


# --- the sign-in ----------------------------------------------------------------

def test_the_client_from_its_file_or_the_env(monkeypatch, tmp_path):
    assert contacts.load_client() is None
    _client_file()
    c = contacts.load_client()
    assert c["client_id"] == "cid.apps.googleusercontent.com" and c["client_secret"] == "csecret"
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps({"installed": {"client_id": "other"}}))
    monkeypatch.setenv("MEDIA_GOOGLE_OAUTH_CLIENT", str(other))
    assert contacts.load_client()["client_id"] == "other"
    assert contacts.load_client()["token_uri"] == contacts.TOKEN_URI
    monkeypatch.setenv("MEDIA_GOOGLE_OAUTH_CLIENT", json.dumps({"installed": {"client_id": "inl"}}))
    assert contacts.load_client()["client_id"] == "inl"


def test_pkce_and_the_consent_url():
    v, ch = contacts.pkce()
    assert 43 <= len(v) <= 128
    assert ch == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    url = contacts.consent_url(contacts.load_client() or {
        "client_id": "cid", "auth_uri": contacts.AUTH_URI}, "http://127.0.0.1:5555", "st", ch)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
    assert q == {"client_id": "cid", "redirect_uri": "http://127.0.0.1:5555",
                 "response_type": "code", "scope": contacts.SCOPE, "state": "st",
                 "code_challenge": ch, "code_challenge_method": "S256",
                 "access_type": "offline", "prompt": "consent"}


def test_the_code_from_what_comes_back():
    full = "http://127.0.0.1:5555/?state=st&code=4/0AbC-d_e&scope=https://www.googleapis.com/auth/contacts.readonly"
    assert contacts.code_from(full, "st") == "4/0AbC-d_e"
    assert contacts.code_from("  " + full + "\n", "st") == "4/0AbC-d_e"
    assert contacts.code_from("/?state=st&code=4%2F0AbC", "st") == "4/0AbC"   # the loopback hit
    assert contacts.code_from("4/0AbCdEfGhIjK", "st") == "4/0AbCdEfGhIjK"     # the bare code
    with pytest.raises(ValueError, match="different sign-in"):
        contacts.code_from(full, "other")
    with pytest.raises(ValueError, match="access_denied"):
        contacts.code_from("http://127.0.0.1:5555/?error=access_denied&state=st", "st")
    with pytest.raises(ValueError):
        contacts.code_from("hello there", "st")


def test_login_by_a_pasted_address_keeps_the_token_owner_only(google):
    contacts.token_path().unlink()
    login = contacts.Login(contacts.load_client())
    try:
        assert login.redirect_uri.startswith("http://127.0.0.1:")
        pasted = f"{login.redirect_uri}/?state={login.state}&code=4/good-code&scope=x"
        kept = login.finish(pasted)
    finally:
        login.close()
    sent = google.exchanged[-1]
    assert sent["code_verifier"] == login.verifier and sent["redirect_uri"] == login.redirect_uri
    assert kept["refresh_token"] == "rt-1"
    mode = stat.S_IMODE(os.stat(contacts.token_path()).st_mode)
    assert mode == 0o600
    assert str(contacts.token_path()).startswith(os.environ["XDG_CONFIG_HOME"])
    assert contacts.signed_in()


def test_the_loopback_hit_is_an_answer():
    login = contacts.Login({"client_id": "cid", "client_secret": "", "auth_uri": contacts.AUTH_URI,
                            "token_uri": contacts.TOKEN_URI})
    try:
        url = f"{login.redirect_uri}/?state={login.state}&code=4/xyz"
        with urllib.request.urlopen(url, timeout=5) as r:
            assert b"close this page" in r.read()
        src, text = login.answers.get(timeout=5)
        assert src == "loopback" and contacts.code_from(text, login.state) == "4/xyz"
    finally:
        login.close()


def test_the_login_command_retries_a_bad_paste_then_syncs(google, monkeypatch, capsys):
    made = []

    class Recorded(contacts.Login):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    def pasted(answers):
        login = made[0]
        answers.put(("paste", "not it"))
        answers.put(("paste", f"{login.redirect_uri}/?state={login.state}&code=4/good-code"))

    monkeypatch.setattr(contacts, "Login", Recorded)
    monkeypatch.setattr(contacts, "_read_stdin", pasted)
    assert contacts.main(["login"]) == 0
    out = capsys.readouterr()
    assert "accounts.google.com" in out.out and "Try again" in out.err
    assert "Signed in (pasted address)" in out.out and "3 contacts" in out.out
    assert contacts.status()["contacts"] == 3


def test_login_without_a_client_says_where_it_goes(capsys):
    assert contacts.main(["login"]) == 2
    assert "google-oauth-client.json" in capsys.readouterr().err


def test_status_and_sync_commands(google, capsys):
    assert contacts.main(["sync"]) == 0
    assert contacts.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "full: 3 changed" in out and "signed_in     True" in out


# --- rooms to people ------------------------------------------------------------

def _room(room_id, members, at=1_792_000_000.0):
    cfg = core_matrix.Config(homeserver="https://hs.test", token="t", rooms={room_id})
    r = matrix.Room(room_id, cfg, ME, get=lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    r.members = {ME: "David", BOT: "Google Messages bridge bot", **members}
    r.network = {"id": "gmessages", "name": "Google Messages"}
    r.messages = [{"id": "$1", "role": "assistant", "at": at,
                   "parts": [{"type": "text", "text": "hi"}],
                   "sender": {"id": next(iter(members), ME), "name": "x"}}]
    r.loaded = True
    matrix._ROOMS[r.thread] = r
    return r


@pytest.fixture
def rooms(google, monkeypatch):
    monkeypatch.setenv("MATRIX_OWNER_BRIDGES", BOT)
    contacts.sync()
    return {
        "by_name": _room("!a:ryer.org", {"@gmessages_1.42:ryer.org": "Lionel Smith"}, at=10.0),
        "by_number": _room("!b:ryer.org", {"@gmessages_1.43:ryer.org": "+61 400 111 222"}),
        "signal": _room("!c:ryer.org", {"@signal_=2b61412345678:ryer.org": ""}, at=20.0),
        "stranger": _room("!d:ryer.org", {"@gmessages_1.44:ryer.org": "0499 999 999"}),
        "group": _room("!e:ryer.org", {"@gmessages_1.42:ryer.org": "Lionel Smith",
                                       "@gmessages_1.43:ryer.org": "Mel Jones"}),
        "email": _room("!f:ryer.org", {"@gmessages_1.45:ryer.org": "zoe@example.com"}),
    }


def _contact_rows():
    return {r["room"]: r.get("contact") for r in matrix.rows()}


def test_rooms_match_by_number_then_email_then_a_unique_name(rooms):
    lionel, mel, zoe = (contacts.contact_id("google", f"people/c{n}") for n in (1, 2, 3))
    got = _contact_rows()
    assert got["!a:ryer.org"] == {"id": lionel, "name": "Lionel Smith"}
    assert got["!b:ryer.org"] == {"id": mel, "name": "Mel Jones"}
    assert got["!c:ryer.org"] == {"id": lionel, "name": "Lionel Smith"}
    assert got["!f:ryer.org"] == {"id": zoe, "name": "Zoë Brown"}
    assert got["!d:ryer.org"] is None and got["!e:ryer.org"] is None
    # Absent, not null, on a row with no person.
    row = next(r for r in matrix.rows() if r["room"] == "!d:ryer.org")
    assert "contact" not in row


def test_davids_own_number_is_not_a_person_in_the_room(rooms, monkeypatch):
    # His own texts, sent from the phone, arrive as his number's puppet.
    r = rooms["by_number"]
    r.members["@gmessages_1.1:ryer.org"] = "+61 412 345 678"     # (pretend) his
    assert _contact_rows()["!b:ryer.org"] is None                 # two people now
    monkeypatch.setenv("MEDIA_CONTACTS_SELF", "0412 345 678")
    assert _contact_rows()["!b:ryer.org"]["name"] == "Mel Jones"


def test_a_name_two_contacts_share_is_no_match(google, monkeypatch):
    google.people.append(_person(4, "Lionel Smith", ["0411 000 000"]))
    contacts.sync(full=True)
    _room("!a:ryer.org", {"@gmessages_1.42:ryer.org": "Lionel Smith"})
    assert _contact_rows()["!a:ryer.org"] is None


def test_overrides_win(rooms):
    mel = contacts.contact_id("google", "people/c2")
    lionel = contacts.contact_id("google", "people/c1")
    contacts.set_override("!d:ryer.org", mel, "join")
    contacts.set_override("!a:ryer.org", lionel, "split")
    contacts._INDEX["checked"] = 0.0
    got = _contact_rows()
    assert got["!d:ryer.org"]["id"] == mel and got["!a:ryer.org"] is None
    contacts.clear_override("!a:ryer.org", lionel)
    contacts._INDEX["checked"] = 0.0
    assert _contact_rows()["!a:ryer.org"]["id"] == lionel
    with pytest.raises(ValueError):
        contacts.set_override("!a:ryer.org", lionel, "merge")


def test_a_card_carries_its_rooms_newest_first(rooms):
    lionel = contacts.contact_id("google", "people/c1")
    c = contacts.card(lionel)
    assert c["sources"] == [{"kind": "google", "id": "people/c1", "etag": "e1"}]
    assert c["addresses"] == [{"formatted": "1 Main Rd, Donvale VIC 3111", "type": "Home"}]
    assert c["notes"] == "Met at the market."
    assert [(x["room"], x["at"]) for x in c["rooms"]] == [("!c:ryer.org", 20.0),
                                                          ("!a:ryer.org", 10.0)]
    assert c["rooms"][0]["session"] == matrix.thread_of("!c:ryer.org")
    assert set(c["rooms"][0]) == {"session", "room", "title", "network", "at"}


# --- the routes -----------------------------------------------------------------

def test_routes_shape_and_auth(server, signed_in, rooms):
    # No bearer, ABS down, a guest: test_contract's GATED_GETS.
    res, obj = call(server, "GET", "/contacts?q=mel", headers=AUTH)
    assert res.status == 200 and set(obj) == {"ok", "contacts", "total", "signed_in", "synced_at"}
    assert [set(r) for r in obj["contacts"]] == [{"id", "name", "phones", "emails"}]
    mel = obj["contacts"][0]["id"]
    res, obj = call(server, "GET", f"/contacts/{mel}", headers=AUTH)
    assert res.status == 200 and set(obj) == {"ok", "contact"}
    assert set(obj["contact"]) == {"id", "name", "names", "phones", "emails", "addresses",
                                   "notes", "sources", "updated", "rooms"}
    assert [x["room"] for x in obj["contact"]["rooms"]] == ["!b:ryer.org"]
    res, obj = call(server, "GET", "/contacts/00000000-0000-0000-0000-000000000000", headers=AUTH)
    assert res.status == 404 and obj == {"ok": False, "error": "no such contact"}
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    res, obj = call(server, "GET", "/targets", headers=AUTH)
    row = next(r for r in obj["sessions"] if r.get("room") == "!b:ryer.org")
    assert row["contact"] == {"id": mel, "name": "Mel Jones"}


def test_a_card_is_gated_like_the_list(server, monkeypatch):
    # The list is in test_contract's GATED_GETS; a card for an id not held
    # answers 404 to a device, so it is pinned here instead.
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    path = f"/contacts/{contacts.contact_id('google', 'people/c1')}"
    res, obj = call(server, "GET", path)
    assert res.status == 401 and obj["ok"] is False and isinstance(obj["error"], str)
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    monkeypatch.setattr(auth_abs, "abs_identity",
                        lambda bearer: ({"username": "guest", "type": "user"}, 200))
    res, _ = call(server, "GET", path, headers=AUTH)
    assert res.status == 403


def test_start_honours_the_switch(monkeypatch):
    monkeypatch.setenv("MEDIA_CONTACTS", "0")
    started = []
    monkeypatch.setattr(contacts.threading, "Thread", lambda *a, **k: started.append(1))
    contacts._STARTED.clear()
    contacts.start()
    assert started == [] and not contacts._STARTED.is_set()


def test_nothing_real_is_touched():
    assert not str(contacts.token_path()).startswith(os.path.expanduser("~ryer/.config"))
    assert not str(contacts.db_path()).startswith(os.path.expanduser("~ryer/.local"))
