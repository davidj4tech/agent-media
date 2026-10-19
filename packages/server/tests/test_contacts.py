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
    # No phone: the android source is a fake where a test wants one.
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
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
    got = contacts.sync_google()
    assert got == {"source": "google", "full": True, "changed": 3, "deleted": 0, "contacts": 3}
    pages = [c for c in google.calls if "connections" in c[1]]
    assert len(pages) == 2 and "pageToken=2" in pages[1][1]
    rows = contacts.listing()["contacts"]
    assert [r["name"] for r in rows] == ["Lionel Smith", "Mel Jones", "Zoë Brown"]
    assert rows[0]["phones"] == ["+61412345678"] and rows[0]["emails"] == ["lionel@example.com"]
    assert contacts.db_path().parent == contacts.db_path().parent  # under the state dir
    assert str(contacts.db_path()).startswith(os.environ["XDG_STATE_HOME"])


def test_incremental_sync_takes_changes_and_deletions(google):
    contacts.sync_google()
    google.changes = [_person(2, "Mel Jones-Park", ["+61 400 111 222"], etag="e2b"),
                      _person(3, "", deleted=True)]
    got = contacts.sync_google()
    assert got == {"source": "google", "full": False, "changed": 1, "deleted": 1,
                   "contacts": 2}
    assert "syncToken=sync-1" in google.calls[-1][1]
    names = [r["name"] for r in contacts.listing()["contacts"]]
    assert names == ["Lionel Smith", "Mel Jones-Park"]


def test_an_expired_sync_token_is_a_full_resync(google):
    contacts.sync_google()
    google.expired = True
    google.people = google.people[:2]          # Zoë went while the token aged
    got = contacts.sync_google()
    assert got["full"] is True and got["deleted"] == 1 and got["contacts"] == 2


def test_an_access_token_google_ended_early_is_refreshed_once(google):
    contacts.sync_google()
    google.unauthorised_once = True
    contacts._ACCESS["expires"] = 10 ** 12      # still "current" here
    contacts.sync_google(full=True)
    assert google.refreshes == 2


def test_a_revoked_sign_in_says_so(google):
    google.token = "rt-other"                   # Google no longer takes ours
    with pytest.raises(contacts.SignInExpired, match="sign in again"):
        contacts.sync_google()
    st = contacts.status()["google"]
    assert "no longer takes" in st["last_error"]
    # Kept as expired: said, and not tried again until the next sign-in.
    assert st["expired"] and not st["signed_in"] and "google" not in contacts.sources()


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
    contacts.sync_google()
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
    assert len(out["contacts"]) == 1 and out["total"] == 3
    assert isinstance(out["synced_at"], float)
    assert out["sources"] == [{"kind": "google", "synced_at": out["synced_at"], "error": None}]


def test_no_store_is_no_contacts_and_makes_none():
    out = contacts.listing("x")
    assert out == {"contacts": [], "total": 0, "synced_at": None, "sources": [], "sort": "name",
                   "talked": False}
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
                 "response_type": "code", "scope": contacts.LOGIN_SCOPE, "state": "st",
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
    assert contacts.status()["contacts"] == 3 and contacts.status()["google"]["signed_in"]


def test_login_without_a_client_says_where_it_goes(capsys):
    assert contacts.main(["login"]) == 2
    assert "google-oauth-client.json" in capsys.readouterr().err


def test_status_and_sync_commands(google, capsys):
    assert contacts.main(["sync"]) == 0
    assert contacts.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "google full: 3 changed" in out and "google.signed_in" in out
    assert "sources       ['google']" in out


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
    contacts.sync_google()
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
    contacts.sync_google(full=True)
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
    assert res.status == 200 and set(obj) == {"ok", "contacts", "total", "synced_at", "sources",
                                                "sort", "talked"}
    assert [set(r) for r in obj["contacts"]] == [{"id", "name", "phones", "emails", "labels",
                                                 "last", "count_90d", "photo"}]
    mel = obj["contacts"][0]["id"]
    res, obj = call(server, "GET", f"/contacts/{mel}", headers=AUTH)
    assert res.status == 200 and set(obj) == {"ok", "contact"}
    assert set(obj["contact"]) == {"id", "name", "names", "phones", "emails", "addresses",
                                   "organizations", "notes", "sources", "updated", "rooms",
                                   "crm", "labels", "handles", "suggestions", "mail", "old_addresses",
                                   "photo", "find"}
    assert obj["contact"]["crm"] is None             # no CRM connection here
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


# --- the phone, over adb (contacts_android.py) ---------------------------------
#
# A fake of what `content query` prints: `Row: N col=value, …`, nothing
# escaped, `NULL` for a null. The fixtures are made up; nothing is read from
# the real phone.

from agent_media_server import contacts_android as ca  # noqa: E402

MIME = ca.MIME
NOTE = ("Met at the market, data2=fake, lookup=x\nRow: 7 not a row\n"
        "= also, this; and été — \U0001f600")
ADDRESS = "1 Main Rd,\nDonvale VIC 3111, data2=Australia"
LIONEL_LOOKUP = "3789r11-4F2B4D.3789r12-4F2B4D"


def _content(rows, cols):
    if not rows:
        return "No result found.\n"
    return "".join(
        f"Row: {i} " + ", ".join(f"{c}={'NULL' if r.get(c) is None else r[c]}" for c in cols) + "\n"
        for i, r in enumerate(rows))


def phone_output(nonce, raw, data):
    m = f"@@AM-{nonce}@@"
    out = [f"{m} raw\n", _content(raw, ca.RAW_COLS), f"{m} data\n", _content(data, ca.DATA_COLS)]
    for c in ca.FREE_COLS:
        out += [f"{m} {c}\n", _content([{"_id": d["_id"], c: d.get(c)} for d in data], ("_id", c))]
    out.append(f"{m} end\n")
    return "".join(out)


def _d(i, contact, raw, lookup, kind, display, d1, d2=None, d3=None, d4=None):
    return {"_id": str(i), "contact_id": contact, "raw_contact_id": raw, "lookup": lookup,
            "mimetype": MIME + kind, "data_version": "1", "display_name": display,
            "data1": d1, "data2": d2, "data3": d3, "data4": d4}


def phone_book():
    raw = [{"_id": "11", "contact_id": "1", "account_type": "com.google", "version": "4"},
           {"_id": "12", "contact_id": "1", "account_type": "com.whatsapp", "version": "2"},
           {"_id": "21", "contact_id": "2", "account_type": "com.google", "version": "9"}]
    zoe = "Zoë 王"
    data = [
        _d(1, "1", "11", LIONEL_LOOKUP, "name", "Lionel Smith", "Lionel Smith", "Lionel", "Smith"),
        _d(2, "1", "11", LIONEL_LOOKUP, "phone_v2", "Lionel Smith", "0412 345 678", "2", None,
           "+61412345678"),
        # WhatsApp's raw contact has the same number: kept once.
        _d(3, "1", "12", LIONEL_LOOKUP, "phone_v2", "Lionel Smith", "+61 412 345 678", "2", None,
           "+61412345678"),
        _d(4, "1", "11", LIONEL_LOOKUP, "email_v2", "Lionel Smith", "Lionel@Example.com", "1"),
        _d(5, "1", "11", LIONEL_LOOKUP, "postal-address_v2", "Lionel Smith", ADDRESS, "1"),
        _d(6, "1", "11", LIONEL_LOOKUP, "note", "Lionel Smith", NOTE),
        _d(7, "1", "11", LIONEL_LOOKUP, "organization", "Lionel Smith", "Acme, Inc.", "1", None,
           "Boss"),
        _d(8, "2", "21", "3789r21-ZZ", "name", zoe, zoe, "Zoë", "王"),
        _d(9, "2", "21", "3789r21-ZZ", "phone_v2", zoe, "(03) 9876 5432", "0", "Studio", None),
        # A raw contact deleted on the phone (not in raw_contacts): dropped.
        _d(10, "3", "31", "3789r31-GONE", "name", "Gone Person", "Gone Person"),
    ]
    return raw, data


@pytest.fixture
def phone(monkeypatch):
    """The phone, answering `content query` from `book` (a test may change
    it), or failing when `down` is set."""
    state = {"book": phone_book(), "down": None, "runs": 0}

    def run(timeout=ca.TIMEOUT_S):
        state["runs"] += 1
        if state["down"]:
            raise ca.Unreachable(state["down"])
        nonce = "n%d" % state["runs"]
        return nonce, phone_output(nonce, *state["book"])

    monkeypatch.setattr(ca, "run", run)
    monkeypatch.setattr(ca, "available", lambda: True)
    return state


def test_rows_keep_newlines_and_only_the_next_index_starts_a_row():
    text = "Row: 0 _id=1, data1=a\nRow: 5 b\nc\nRow: 1 _id=2, data1=NULL\n"
    assert ca.rows(text) == ["_id=1, data1=a\nRow: 5 b\nc", "_id=2, data1=NULL"]
    assert ca.rows("No result found.\n") == []
    with pytest.raises(ca.Unreachable, match="refused"):
        ca.rows("Error while accessing provider:com.android.contacts\njava.lang.Illegal…")


def test_split_by_the_projections_names():
    row = "_id=7, contact_id=1, raw_contact_id=11, lookup=a=b, mimetype=x, data_version=NULL"
    assert ca.split(row, ca.DATA_COLS) == {"_id": "7", "contact_id": "1", "raw_contact_id": "11",
                                           "lookup": "a=b", "mimetype": "x", "data_version": None}
    # The free-text column is last: the rest of the row, whatever it holds.
    assert ca.split("_id=6, data1=" + NOTE, ("_id", "data1")) == {"_id": "6", "data1": NOTE}
    with pytest.raises(ValueError):
        ca.split("nonsense", ca.DATA_COLS)


def test_parse_reads_nasty_values_whole():
    raw, data = phone_book()
    got = ca.parse(phone_output("abc", raw, data), "abc")
    assert set(got["raw"]) == {"11", "12", "21"}
    assert got["data"]["6"]["data1"] == NOTE
    assert got["data"]["5"]["data1"] == ADDRESS
    assert got["data"]["9"]["data4"] is None and got["data"]["9"]["data3"] == "Studio"
    assert got["data"]["8"]["display_name"] == "Zoë 王"


def test_a_cut_short_or_foreign_read_is_unreachable():
    raw, data = phone_book()
    out = phone_output("abc", raw, data)
    with pytest.raises(ca.Unreachable, match="cut short"):
        ca.parse(out.replace("@@AM-abc@@ end\n", ""), "abc")
    with pytest.raises(ca.Unreachable):
        ca.parse(out, "another-nonce")      # a marker in a value is never ours


def test_records_one_per_person_by_lookup_key():
    raw, data = phone_book()
    recs = ca.records(ca.parse(phone_output("n", raw, data), "n"))
    assert set(recs) == {LIONEL_LOOKUP, "3789r21-ZZ"}       # the deleted one is not there
    lionel = recs[LIONEL_LOOKUP]
    assert lionel["meta"] == {"raw": [11, 12], "accounts": ["com.google", "com.whatsapp"],
                              "version": 6}
    c = lionel["card"]
    assert c["name"] == "Lionel Smith"
    assert c["names"] == [{"display": "Lionel Smith", "given": "Lionel", "family": "Smith"}]
    assert c["phones"] == [{"value": "+61412345678", "raw": "0412 345 678", "type": "Mobile"}]
    assert c["emails"] == [{"value": "Lionel@Example.com", "type": "Home"}]
    assert c["addresses"] == [{"formatted": ADDRESS, "type": "Home"}]
    assert c["notes"] == NOTE.strip()
    assert c["organizations"] == [{"company": "Acme, Inc.", "title": "Boss"}]
    zoe = recs["3789r21-ZZ"]["card"]
    assert zoe["phones"] == [{"value": "+61398765432", "raw": "(03) 9876 5432", "type": "Studio"}]


def test_the_script_reads_only_what_it_needs():
    s = ca.script("abc")
    assert s.count("content query") == 4 + len(ca.FREE_COLS)      # + the groups, twice
    assert "--projection _id:contact_id:account_type:version --where 'deleted=0'" in s
    assert "mimetype IN ('vnd.android.cursor.item/phone_v2'," in s
    assert "photo" not in s and "@@AM-abc@@ end" in s


def test_android_sync_keeps_the_book_and_skips_an_unchanged_read(phone, monkeypatch):
    got = contacts.sync_android()
    assert got == {"source": "android", "unchanged": False, "read": 2, "gone": 0, "contacts": 2}
    lionel = contacts.contact_id("android", LIONEL_LOOKUP)
    c = contacts.card(lionel)
    assert c["sources"] == [{"kind": "android", "id": LIONEL_LOOKUP, "raw": [11, 12],
                             "accounts": ["com.google", "com.whatsapp"], "version": 6}]
    assert c["notes"] == NOTE.strip() and c["organizations"][0]["company"] == "Acme, Inc."
    assert [r["name"] for r in contacts.listing("zo")["contacts"]] == ["Zoë 王"]
    conn = contacts._connect()
    gen = contacts._meta(conn, "gen")
    conn.close()
    assert contacts.sync_android()["unchanged"] is True
    conn = contacts._connect()
    assert contacts._meta(conn, "gen") == gen          # nothing rewritten
    conn.close()
    # A change on the phone: a new number for Zoë.
    phone["book"][1][8]["data1"] = "0400 000 001"
    got = contacts.sync_android()
    assert got["unchanged"] is False
    zoe = contacts.contact_id("android", "3789r21-ZZ")
    assert contacts.card(zoe)["phones"][0]["value"] == "+61400000001"


def test_the_phone_off_wifi_keeps_the_last_good_copy(phone):
    contacts.sync_android()
    phone["down"] = "agent-phone-adb exited 1: adb: no devices/emulators found"
    with pytest.raises(ca.Unreachable):
        contacts.sync_android()
    out = contacts.listing()
    assert out["total"] == 2
    [android] = out["sources"]
    assert android["kind"] == "android" and "no devices" in android["error"]
    assert isinstance(android["synced_at"], float)
    assert contacts.status()["android"]["last_error"].endswith("found")
    # sync() says it and goes on; the next good read clears it.
    assert contacts.sync()["errors"] == {"android": phone["down"]}
    phone["down"] = None
    assert contacts.sync()["errors"] == {}
    assert contacts.listing()["sources"][0]["error"] is None


def test_google_wins_where_both_know_the_person(phone, google):
    contacts.sync_android()
    contacts.sync_google()
    lionel = contacts.contact_id("android", LIONEL_LOOKUP)
    c = contacts.card(lionel)
    # One person: the phone's id, both sources, Google's fields first.
    assert [s["kind"] for s in c["sources"]] == ["google", "android"]
    assert c["emails"] == [{"value": "lionel@example.com", "type": "Home"}]
    assert c["notes"] == "Met at the market."                      # Google's
    assert c["organizations"] == [{"company": "Acme, Inc.", "title": "Boss"}]  # the phone's
    names = sorted(r["name"] for r in contacts.listing()["contacts"])
    # Mel and Zoë Brown are Google's alone; Zoë 王 the phone's alone.
    assert names == ["Lionel Smith", "Mel Jones", "Zoë Brown", "Zoë 王"]
    assert contacts.sources() == ["android", "google"]


def test_sources_default_to_the_phone_alone(phone, monkeypatch):
    assert contacts.sources() == ["android"]
    monkeypatch.setenv("MEDIA_CONTACTS_SOURCES", "google")
    assert contacts.sources() == []                                # not signed in
    monkeypatch.setattr(ca, "available", lambda: False)
    monkeypatch.delenv("MEDIA_CONTACTS_SOURCES")
    assert contacts.sources() == []


def test_the_command_is_a_subprocess_and_replaceable(monkeypatch, tmp_path):
    # A stand-in for `agent-phone-adb shell`: it reads the nonce off the
    # script it is handed and prints the fixture, as the phone would.
    import sys

    raw, data = phone_book()
    canned = tmp_path / "answer.txt"
    canned.write_text(phone_output("NONCE", raw, data))
    fake = tmp_path / "fake-adb"
    fake.write_text(f"""#!{sys.executable}
import re, sys
assert sys.argv[1] == "shell"
nonce = re.search(r"@@AM-([0-9a-f]+)@@", sys.argv[2]).group(1)
text = open({str(canned)!r}, encoding="utf-8").read()
sys.stdout.buffer.write(text.replace("@@AM-NONCE@@", f"@@AM-{{nonce}}@@").encode("utf-8"))
""")
    fake.chmod(0o755)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", f"{fake} shell")
    assert ca.available()
    recs, digest = ca.read()
    assert set(recs) == {LIONEL_LOOKUP, "3789r21-ZZ"} and len(digest) == 64
    fake.write_text("#!/bin/sh\necho 'adb: device offline' >&2\nexit 1\n")
    with pytest.raises(ca.Unreachable, match="device offline"):
        ca.read()
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "missing") + " shell")
    assert not ca.available()
    with pytest.raises(ca.Unreachable, match="could not run"):
        ca.read()


def test_sync_command_covers_the_phone(phone, capsys):
    assert contacts.main(["sync"]) == 0
    assert "android: read 2 contact(s) from the phone, 2 contacts" in capsys.readouterr().out
    phone["down"] = "off Wi-Fi"
    assert contacts.main(["sync", "--source", "android"]) == 1
    assert "android: sync failed: off Wi-Fi" in capsys.readouterr().err
    assert contacts.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "android.last_error" in out and "off Wi-Fi" in out


def test_rooms_match_phone_contacts(phone, monkeypatch):
    monkeypatch.setenv("MATRIX_OWNER_BRIDGES", BOT)
    contacts.sync_android()
    _room("!a:ryer.org", {"@gmessages_1.42:ryer.org": "Lionel Smith"})
    _room("!z:ryer.org", {"@gmessages_1.50:ryer.org": "+61 3 9876 5432"})
    got = _contact_rows()
    assert got["!a:ryer.org"]["id"] == contacts.contact_id("android", LIONEL_LOOKUP)
    assert got["!z:ryer.org"]["name"] == "Zoë 王"
