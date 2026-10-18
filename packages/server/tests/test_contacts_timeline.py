"""The Drupal CRM source (contacts_drupal.py), the seed, and the contacts
timeline (timeline.py, §6.25a). The CRM is a fake JSON:API server on a
loopback port of the test's own, never a real site; no phone (the call log
is a fake adb script), no IMAP server (a fake mailbox object), no
homeserver; HOME, the config dir and the state dir are throwaway."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_media_core import matrix as core_matrix
from agent_media_server import contacts, contacts_drupal, matrix, timeline
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401

ME, SAM = "@david:ryer.org", "@sam:ryer.org"
BOT = "@gmessagesbot:ryer.org"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MATRIX_SAM_ID", SAM)
    monkeypatch.setenv("MATRIX_OWNER_BRIDGES", BOT)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    contacts._reset_for_tests()


# --- a fake CRM -----------------------------------------------------------------

class Crm:
    """Simple OAuth's token endpoint and the JSON:API of crm_contact,
    crm_contact_method and crm_activity, kept in dicts."""

    def __init__(self):
        self.people: dict[str, dict] = {}
        self.methods: dict[str, dict] = {}
        self.activity: dict[str, dict] = {}
        self.log: list = []
        self.tokens = 0
        self.valid: set = set()
        self.expire_next = False
        self.page = 2

    # -- seeding the fake --
    def person(self, name, given="", family="", phones=(), emails=(), **extra):
        pid = str(uuid.uuid4())
        rels = {"telephones": {"data": []}, "emails": {"data": []}, "addresses": {"data": []}}
        for field, bundle, key, values in (("telephones", "telephone", "telephone", phones),
                                           ("emails", "email", "email", emails)):
            for v in values:
                mid = self._method(bundle, key, v, pid)
                rels[field]["data"].append({"type": f"crm_contact_method--{bundle}", "id": mid})
        self.people[pid] = {"type": "crm_contact--person", "id": pid,
                            "attributes": {"name": name, "drupal_internal__id": len(self.people) + 1,
                                           "full_name": {"given": given, "family": family},
                                           "status": True, "changed": "2026-10-18T00:00:00+00:00",
                                           **extra},
                            "relationships": rels}
        return pid

    def _method(self, bundle, key, value, owner):
        mid = str(uuid.uuid4())
        self.methods[mid] = {"type": f"crm_contact_method--{bundle}", "id": mid,
                             "attributes": {key: value},
                             "relationships": {"crm_contact": {"data": {"id": owner}},
                                               "detail": {"data": {"meta": {
                                                   "drupal_internal__target_id": "mobile"}}}}}
        return mid

    # -- HTTP --
    def handle(self, method, path, headers, body):
        u = urllib.parse.urlsplit(path)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.log.append((method, u.path, q, body))
        if u.path == "/oauth/token":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            if form.get("grant_type") != "client_credentials" or form.get("client_secret") != "s3cret":
                return 401, {"error": "invalid_client"}
            self.tokens += 1
            tok = f"tok-{self.tokens}"
            self.valid = {tok}
            return 200, {"access_token": tok, "expires_in": 300, "token_type": "Bearer"}
        auth = headers.get("Authorization", "")
        if self.expire_next:
            self.expire_next = False
            self.valid = set()
        if auth.removeprefix("Bearer ") not in self.valid:
            return 401, {"errors": [{"title": "Unauthorized", "detail": "bad token"}]}
        assert headers.get("Accept") == contacts_drupal.JSONAPI
        doc = json.loads(body) if body else None
        if body:
            assert headers.get("Content-Type") == contacts_drupal.JSONAPI
        parts = u.path.split("/")[2:]          # after /jsonapi
        if parts[:2] == ["crm_contact", "person"]:
            return self._people(method, parts[2:], q, doc)
        if parts[0] == "crm_contact_method":
            assert method == "POST"
            d = doc["data"]
            mid = str(uuid.uuid4())
            self.methods[mid] = {**d, "id": mid}
            return 201, {"data": self.methods[mid]}
        if parts[:2] == ["crm_activity", "crm_activity"]:
            return self._activity(method, parts[2:], q, doc)
        return 404, {"errors": [{"title": "Not Found"}]}

    def _people(self, method, rest, q, doc):
        if method == "GET" and not rest:
            assert q["include"] == "emails,telephones,addresses"
            ids = sorted(self.people, key=lambda p: self.people[p]["attributes"]["drupal_internal__id"])
            start = int(q.get("page[offset]") or 0)
            chunk = ids[start:start + self.page]
            inc = []
            for pid in chunk:
                for rel in self.people[pid]["relationships"].values():
                    for ref in rel["data"]:
                        inc.append(self.methods[ref["id"]])
            links = {}
            if start + self.page < len(ids):
                links["next"] = {"href": f"/jsonapi/crm_contact/person?include={q['include']}"
                                         f"&page[offset]={start + self.page}"}
            return 200, {"data": [self.people[p] for p in chunk], "included": inc, "links": links}
        if method == "POST" and not rest:
            d = doc["data"]
            assert d["type"] == "crm_contact--person"
            pid = str(uuid.uuid4())
            self.people[pid] = {"type": d["type"], "id": pid,
                                "attributes": {**d["attributes"],
                                               "drupal_internal__id": 100 + len(self.people)},
                                "relationships": {"telephones": {"data": []}, "emails": {"data": []},
                                                  "addresses": {"data": []}}}
            return 201, {"data": self.people[pid]}
        if method == "PATCH" and len(rest) == 3 and rest[1] == "relationships":
            self.people[rest[0]]["relationships"][rest[2]] = {"data": doc["data"]}
            return 204, {}
        return 405, {}

    def _activity(self, method, rest, q, doc):
        if method == "GET":
            rows = list(self.activity.values())
            if "filter[source_key]" in q:
                rows = [r for r in rows if r["attributes"]["source_key"] == q["filter[source_key]"]]
            if "filter[contact.id]" in q:
                rows = [r for r in rows
                        if r["relationships"]["contact"]["data"]["id"] == q["filter[contact.id]"]]
            return 200, {"data": rows}
        d = doc["data"]
        assert d["type"] == "crm_activity--crm_activity"
        if method == "POST":
            key = d["attributes"]["source_key"]
            if any(r["attributes"]["source_key"] == key for r in self.activity.values()):
                return 422, {"errors": [{"title": "Unprocessable", "detail": "source_key taken"}]}
            aid = str(uuid.uuid4())
            self.activity[aid] = {**d, "id": aid}
            return 201, {"data": self.activity[aid]}
        if method == "PATCH":
            assert rest[0] == d["id"]
            self.activity[rest[0]] = {**d}
            return 200, {"data": d}
        return 405, {}


@pytest.fixture
def crm(monkeypatch, tmp_path):
    fake = Crm()

    class H(BaseHTTPRequestHandler):
        def _go(self):
            n = int(self.headers.get("Content-Length") or 0)
            status, obj = fake.handle(self.command, self.path, dict(self.headers),
                                      self.rfile.read(n) if n else b"")
            raw = b"" if status == 204 else json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", contacts_drupal.JSONAPI)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PATCH = _go

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{srv.server_address[1]}"
    cfg = contacts.config_dir() / "drupal-crm.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"url": fake.url + "/", "client_id": "agent-media",
                               "client_secret": "s3cret", "scope": "agent_media_contacts"}))
    yield fake
    srv.shutdown()
    srv.server_close()


# --- the store, from records ----------------------------------------------------

def _google(n, name, phones=(), emails=()):
    return ("google", f"people/c{n}",
            {"name": name, "names": [{"display": name, "given": name.split()[0],
                                      "family": name.split()[-1]}],
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


@pytest.fixture
def book():
    _book(_google(1, "Lionel Smith", ["0412 345 678"], ["lionel@example.com"]),
          _google(2, "Mel Jones", ["+61 400 111 222"]),
          _google(3, "Nobody Reachable"))


# --- the connection and the token -----------------------------------------------

def test_no_connection_is_no_crm(book):
    assert not contacts_drupal.configured()
    assert "drupal" not in contacts.sources()
    assert contacts.card(LIONEL)["crm"] is None
    assert timeline.push() == {"pushed": 0, "unlinked": 0, "errors": 0}
    with pytest.raises(contacts_drupal.NotConfigured):
        contacts_drupal.token()


def test_the_connection_from_the_env_or_its_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DRUPAL_CRM", json.dumps({"url": "https://crm.test/",
                                                       "client_id": "a", "client_secret": "b"}))
    assert contacts_drupal.settings() == {"url": "https://crm.test", "client_id": "a",
                                          "client_secret": "b", "scope": ""}
    p = tmp_path / "elsewhere.json"
    p.write_text(json.dumps({"url": "https://x.test", "client_id": "a"}))   # no secret
    monkeypatch.setenv("MEDIA_DRUPAL_CRM", str(p))
    assert contacts_drupal.settings() is None


def test_the_token_is_client_credentials_cached_owner_only_and_renewed(crm):
    assert contacts_drupal.token() == "tok-1"
    sign_in = crm.log[0]
    form = dict(urllib.parse.parse_qsl(sign_in[3].decode()))
    assert form == {"grant_type": "client_credentials", "client_id": "agent-media",
                    "client_secret": "s3cret", "scope": "agent_media_contacts"}
    mode = stat.S_IMODE(os.stat(contacts_drupal.token_path()).st_mode)
    assert mode == 0o600
    assert contacts_drupal.token() == "tok-1" and crm.tokens == 1       # cached
    crm.expire_next = True                                              # the CRM ended it early
    contacts_drupal.get("/jsonapi/crm_activity/crm_activity")
    assert crm.tokens == 2


def test_a_refused_sign_in_says_so(crm):
    cfg = json.loads((contacts.config_dir() / "drupal-crm.json").read_text())
    (contacts.config_dir() / "drupal-crm.json").write_text(json.dumps({**cfg, "client_secret": "no"}))
    with pytest.raises(contacts_drupal.CrmError, match="refused the sign-in.*invalid_client"):
        contacts_drupal.token()


# --- reading the CRM into the book ----------------------------------------------

def test_crm_people_join_by_number_google_wins_and_the_rest_is_its_section(crm, book):
    crm.person("Lionel S", "Lionel", "S", phones=["0412 345 678"], emails=["old@example.com"],
               field_matrix_id="@lionel:ryer.org", preferred_name="Li")
    crm.person("Only In Crm", "Only", "Crm", phones=["0499 000 111"])
    crm.person("Mel J", emails=["mel@example.com"])        # no number shared: her own
    got = contacts.sync_drupal()
    assert got == {"source": "drupal", "unchanged": False, "read": 3, "contacts": 5}
    lionel = contacts.card(LIONEL)
    assert lionel["name"] == "Lionel Smith"                              # Google's
    assert [e["value"] for e in lionel["emails"]] == ["lionel@example.com"]
    assert lionel["crm"]["fields"] == {"matrix_id": "@lionel:ryer.org", "preferred_name": "Li"}
    assert lionel["crm"]["url"] == f"{crm.url}/crm/contact/{lionel['crm']['id']}"
    assert [s["kind"] for s in lionel["sources"]] == ["google", "drupal"]
    only = [c for c in contacts.listing("only")["contacts"]]
    assert [c["name"] for c in only] == ["Only Crm"] and only[0]["phones"] == ["+61499000111"]
    assert contacts.card(only[0]["id"])["phones"][0]["type"] == "Mobile"
    assert contacts.sync_drupal()["unchanged"] is True
    # Two pages were read.
    pages = [x for x in crm.log if x[1] == "/jsonapi/crm_contact/person" and x[0] == "GET"]
    assert len(pages) >= 2


def test_an_unreachable_crm_keeps_the_last_copy(crm, book, monkeypatch):
    crm.person("Lionel S", phones=["0412 345 678"])
    contacts.sync_drupal()
    monkeypatch.setenv("MEDIA_DRUPAL_CRM", json.dumps({"url": "http://127.0.0.1:9",
                                                       "client_id": "a", "client_secret": "b"}))
    with pytest.raises(OSError):
        contacts.sync_drupal()
    assert contacts.card(LIONEL)["crm"] is not None
    states = {s["kind"]: s for s in contacts.listing()["sources"]}
    assert states["drupal"]["error"]


# --- the seed -------------------------------------------------------------------

def test_the_seed_is_a_dry_run_unless_told(crm, book):
    got = contacts.seed()
    assert got["dry_run"] and got["would_add"] == ["Lionel Smith", "Mel Jones"]
    assert got["skipped_no_way_to_reach"] == 1
    assert not any(m in ("POST", "PATCH") for m, *_ in crm.log)


def test_the_seed_adds_people_once_with_their_numbers(crm, book):
    crm.person("Mel J", phones=["0400 111 222"])                         # already there
    got = contacts.seed(dry_run=False)
    assert [m["name"] for m in got["added"]] == ["Lionel Smith"] and got["already"] == 1
    pid = got["added"][0]["uuid"]
    p = crm.people[pid]
    assert p["attributes"]["full_name"] == {"given": "Lionel", "family": "Smith"}
    tel = p["relationships"]["telephones"]["data"]
    assert [crm.methods[t["id"]]["attributes"]["telephone"] for t in tel] == ["+61412345678"]
    em = p["relationships"]["emails"]["data"]
    assert [crm.methods[e["id"]]["attributes"]["email"] for e in em] == ["lionel@example.com"]
    assert crm.methods[tel[0]["id"]]["relationships"]["crm_contact"]["data"]["id"] == pid
    assert contacts.drupal_uuid(LIONEL) == pid
    again = contacts.seed(dry_run=False)
    assert again["added"] == [] and again["already"] == 2
    assert len(crm.people) == 2


def test_the_seed_link_holds_when_the_numbers_no_longer_match(crm, book):
    got = contacts.seed(dry_run=False, limit=1)
    pid = got["added"][0]["uuid"]
    crm.people[pid]["relationships"]["telephones"] = {"data": []}        # edited in the CRM
    crm.people[pid]["relationships"]["emails"] = {"data": []}
    contacts.sync_drupal()
    assert contacts.card(LIONEL)["crm"]["id"] == pid


# --- the rollups ----------------------------------------------------------------

def _room(room_id, member, network, msgs):
    cfg = core_matrix.Config(homeserver="https://hs.test", token="t", rooms={room_id})
    r = matrix.Room(room_id, cfg, ME, get=lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    r.members = {ME: "David", BOT: "bridge bot", **member}
    r.network = {"id": network, "name": network}
    r.messages = [{"id": f"${i}", "role": "user" if s == ME else "assistant", "at": at,
                   "parts": [{"type": "text", "text": "secret words"}],
                   "sender": {"id": s, "name": s}} for i, (at, s) in enumerate(msgs)]
    r.loaded = True
    matrix._ROOMS[r.thread] = r
    return r


def _at(day_offset=0, hour=12):
    d = dt.date.today() - dt.timedelta(days=day_offset)
    return dt.datetime.combine(d, dt.time(hour)).timestamp()


TODAY = dt.date.today().isoformat()
YESTERDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()
LIONEL_SMS = "@gmessages_1.42:ryer.org"


@pytest.fixture
def rooms(book):
    sms = _room("!a:ryer.org", {LIONEL_SMS: "Lionel Smith"}, "gmessages",
                [(_at(1), LIONEL_SMS), (_at(0, 9), LIONEL_SMS), (_at(0, 10), ME),
                 (_at(0, 11), ME), (_at(5), LIONEL_SMS)])            # 5 days ago: outside
    sig = _room("!b:ryer.org", {"@signal_=2b61412345678:ryer.org": ""}, "signal",
                [(_at(0), "@signal_=2b61412345678:ryer.org")])
    wa = _room("!c:ryer.org", {"@whatsapp_61400111222:ryer.org": "Mel Jones"}, "whatsapp",
               [(_at(0), "@whatsapp_61400111222:ryer.org")])         # no channel for it yet
    return sms, sig, wa


def _ledger():
    conn = timeline._connect()
    try:
        return {(r[0], r[1]): r[2:] for r in conn.execute(
            "SELECT channel, day, count_in, count_out, duration, summary FROM entries "
            "WHERE contact=?", (LIONEL,))}
    finally:
        conn.close()


def test_sources_default_to_texts_and_notes():
    assert timeline.enabled() == ["texts", "notes"]
    os.environ["MEDIA_TIMELINE_SOURCES"] = "texts, calls,email,bogus"
    try:
        assert timeline.enabled() == ["texts", "calls", "email"]
    finally:
        del os.environ["MEDIA_TIMELINE_SOURCES"]


def test_texts_roll_up_per_contact_channel_and_day_without_bodies(rooms):
    got = timeline.rollup(push_=False)
    assert got["changed"] == 3 and got["window"] == [YESTERDAY, TODAY]
    assert _ledger() == {("sms", TODAY): (1, 2, 0, "3 texts (1 in, 2 out)"),
                         ("sms", YESTERDAY): (1, 0, 0, "1 text (1 in, 0 out)"),
                         ("signal", TODAY): (1, 0, 0, "1 Signal message (1 in, 0 out)")}
    raw = timeline.db_path().read_bytes()
    assert b"secret words" not in raw
    assert stat.S_IMODE(os.stat(timeline.db_path()).st_mode) == 0o600


def test_a_count_never_goes_down(rooms):
    timeline.rollup(push_=False)
    sms = rooms[0]
    sms.messages = sms.messages[-2:]                    # the room holds less now
    assert timeline.rollup(push_=False)["changed"] == 0
    assert _ledger()[("sms", TODAY)][:2] == (1, 2)


def test_davids_own_number_counts_as_out(rooms, monkeypatch):
    mine = "@gmessages_1.1:ryer.org"
    rooms[0].members[mine] = "+61 499 555 000"
    rooms[0].messages.append({"id": "$m", "at": _at(0, 14), "sender": {"id": mine}})
    monkeypatch.setenv("MEDIA_CONTACTS_SELF", "0499 555 000")
    contacts._ROOM_MEMO.clear()
    timeline.rollup(push_=False)
    assert _ledger()[("sms", TODAY)][:2] == (1, 3)


def test_rollups_go_to_the_crm_once_by_source_key(crm, rooms):
    contacts.seed(dry_run=False)
    pid = contacts.drupal_uuid(LIONEL)
    got = timeline.rollup()
    assert got["pushed"] == 3 and got["unlinked"] == 0 and got["errors"] == 0
    rows = {(r["attributes"]["channel"], r["attributes"]["day"]): r for r in crm.activity.values()}
    today = rows[("sms", TODAY)]
    assert today["attributes"]["source_key"] == timeline.key_of("sms", TODAY, LIONEL)
    assert today["attributes"]["count_out"] == 2
    assert today["relationships"]["contact"]["data"] == {"type": "crm_contact--person", "id": pid}
    assert "secret" not in json.dumps(crm.activity)
    rooms[0].messages.append({"id": "$n", "at": _at(0, 15), "sender": {"id": LIONEL_SMS}})
    got = timeline.rollup()
    assert got["pushed"] == 1 and len(crm.activity) == 3                  # updated, not added
    assert rows[("sms", TODAY)]["id"] in crm.activity
    assert crm.activity[today["id"]]["attributes"]["count_in"] == 2


def test_a_contact_not_in_the_crm_waits_and_a_down_crm_is_retried(crm, rooms):
    got = timeline.rollup()
    assert got["pushed"] == 0 and got["unlinked"] == 3                    # not seeded yet
    contacts.seed(dry_run=False)
    cfg = contacts.config_dir() / "drupal-crm.json"
    good = cfg.read_text()
    cfg.write_text(json.dumps({**json.loads(good), "url": "http://127.0.0.1:9"}))
    contacts_drupal.token_path().unlink()
    got = timeline.push()
    assert got["errors"] == 3
    cfg.write_text(good)
    assert timeline.push()["pushed"] == 3


def test_a_ledger_lost_finds_its_crm_entry_by_key(crm, rooms):
    contacts.seed(dry_run=False)
    timeline.rollup()
    timeline.db_path().unlink()
    timeline.rollup()
    assert len(crm.activity) == 3


# --- calls and mail (off by default) --------------------------------------------

def _adb(tmp_path, rows):
    script = tmp_path / "fake-adb"
    body = "\n".join(f"Row: {i} {r}" for i, r in enumerate(rows)) or "No result found."
    script.write_text("#!/bin/sh\ncat <<'EOF'\n" + body + "\nEOF\n")
    script.chmod(0o755)
    return str(script) + " shell"


def test_calls_are_off_by_default_and_counted_when_on(book, tmp_path, monkeypatch):
    ms = lambda off, h: int(_at(off, h) * 1000)  # noqa: E731
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", _adb(tmp_path, [
        f"date={ms(0, 9)}, duration=600, type=1, number=0412 345 678",
        f"date={ms(0, 10)}, duration=240, type=2, number=+61412345678",
        f"date={ms(0, 11)}, duration=0, type=3, number=0412345678",
        f"date={ms(0, 12)}, duration=0, type=6, number=0412345678",       # blocked
        f"date={ms(0, 12)}, duration=30, type=2, number=0499 999 999",    # nobody
    ]))
    timeline.rollup(push_=False)
    assert ("call", TODAY) not in _ledger()
    monkeypatch.setenv("MEDIA_TIMELINE_SOURCES", "calls")
    got = timeline.rollup(push_=False)
    assert got["source_errors"] == {}
    assert _ledger()[("call", TODAY)] == (2, 1, 840, "3 calls, 14 min (2 in, 1 out)")


def test_a_call_log_that_cannot_be_read_is_said(book, monkeypatch):
    monkeypatch.setenv("MEDIA_TIMELINE_SOURCES", "calls,texts")
    got = timeline.rollup(push_=False)
    assert "calls" in got["source_errors"] and "texts" not in got["source_errors"]


class Box:
    def __init__(self, by_day):
        self.by_day = by_day
        self.selected = None

    def login(self, user, pw):
        assert (user, pw) == ("david@example.com", "app-pw")

    def select(self, folder, readonly):
        assert readonly
        self.selected = folder

    def search(self, charset, *crit):
        assert crit[0] == "SINCE" and crit[2] == "BEFORE"
        day = dt.datetime.strptime(crit[1], "%d-%b-%Y").date().isoformat()
        n = len(self.by_day.get(day, []))
        return "OK", [" ".join(f"{day}-{i}" for i in range(n)).encode()]

    def fetch(self, ids, what):
        assert "HEADER.FIELDS (FROM TO CC)" in what
        out = []
        for i in ids.split(b","):
            day, n = i.decode().rsplit("-", 1)
            out.append((b"x", self.by_day[day][int(n)].encode()))
            out.append(b")")
        return "OK", out

    def logout(self):
        pass


def test_email_is_off_by_default_headers_only_by_address(book, monkeypatch):
    contacts.config_dir().mkdir(parents=True, exist_ok=True)
    (contacts.config_dir() / "timeline-imap.json").write_text(json.dumps(
        {"host": "imap.test", "user": "david@example.com", "password": "app-pw"}))
    box = Box({TODAY: ["From: Lionel <lionel@example.com>\r\nTo: david@example.com\r\n\r\n",
                       "From: david@example.com\r\nTo: Lionel@Example.com, x@y.z\r\n\r\n",
                       "From: spam@nowhere.test\r\nTo: david@example.com\r\n\r\n"]})
    monkeypatch.setattr(timeline, "_imap", lambda cfg: box)
    timeline.rollup(push_=False)
    assert ("email", TODAY) not in _ledger()
    monkeypatch.setenv("MEDIA_TIMELINE_SOURCES", "email")
    timeline.rollup(push_=False)
    assert box.selected == '"[Gmail]/All Mail"'
    assert _ledger()[("email", TODAY)] == (1, 1, 0, "2 emails (1 in, 1 out)")


# --- notes ----------------------------------------------------------------------

def test_a_note_is_kept_and_written_to_the_crm(crm, book):
    contacts.seed(dry_run=False)
    got = timeline.add_note(LIONEL, "  Moved to Donvale.  ")
    assert got["synced"] and got["summary"] == "Moved to Donvale." and got["day"] == TODAY
    (row,) = crm.activity.values()
    assert row["attributes"]["channel"] == "note" and row["attributes"]["summary"] == "Moved to Donvale."
    with pytest.raises(ValueError):
        timeline.add_note(LIONEL, "  ")


def test_notes_can_be_switched_off(book, monkeypatch):
    monkeypatch.setenv("MEDIA_TIMELINE_SOURCES", "texts")
    with pytest.raises(PermissionError):
        timeline.add_note(LIONEL, "hi")


def test_the_timeline_is_the_ledger_and_what_else_the_crm_holds(crm, rooms):
    contacts.seed(dry_run=False)
    timeline.rollup()
    pid = contacts.drupal_uuid(LIONEL)
    aid = str(uuid.uuid4())
    crm.activity[aid] = {"type": "crm_activity--crm_activity", "id": aid,
                         "attributes": {"channel": "note", "day": "2026-01-02", "count_in": 0,
                                        "count_out": 0, "duration": 0,
                                        "summary": "Typed in the CRM", "source_key": "manual:1"},
                         "relationships": {"contact": {"data": {"id": pid}}}}
    got = timeline.timeline(LIONEL)
    assert [(e["channel"], e["day"], e["origin"]) for e in got["entries"]] == [
        ("sms", TODAY, "local"), ("signal", TODAY, "local"), ("sms", YESTERDAY, "local"),
        ("note", "2026-01-02", "crm")]
    assert all(e["synced"] for e in got["entries"])
    assert got["crm"] == {"id": pid, "error": None}
    assert set(got) == {"entries", "sources", "crm"}


# --- the routes and the command line --------------------------------------------

def test_routes_shape_and_auth(server, signed_in, crm, book):
    contacts.seed(dry_run=False)
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/notes", {"text": "Call back Tuesday"},
                    headers=AUTH)
    assert res.status == 200 and obj["ok"] and obj["note"]["synced"] is True
    res, obj = call(server, "GET", f"/contacts/{LIONEL}/timeline", headers=AUTH)
    assert res.status == 200 and set(obj) == {"ok", "entries", "sources", "crm"}
    assert [e["summary"] for e in obj["entries"]] == ["Call back Tuesday"]
    assert set(obj["entries"][0]) == {"key", "channel", "day", "count_in", "count_out",
                                      "duration", "summary", "origin", "synced"}
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    nobody = "00000000-0000-0000-0000-000000000000"
    res, obj = call(server, "GET", f"/contacts/{nobody}/timeline", headers=AUTH)
    assert res.status == 404
    res, obj = call(server, "POST", f"/contacts/{nobody}/notes", {"text": "x"}, headers=AUTH)
    assert res.status == 404
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/notes", {"text": ""}, headers=AUTH)
    assert res.status == 400


def test_routes_are_gated(server, book, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, _ = call(server, "GET", f"/contacts/{LIONEL}/timeline")
    assert res.status in (401, 403)
    res, _ = call(server, "POST", f"/contacts/{LIONEL}/notes", {"text": "x"})
    assert res.status in (401, 403)
    assert not timeline.db_path().exists() or not timeline.timeline(LIONEL)["entries"]


def test_the_commands(book, capsys):
    assert contacts.main(["note", "lionel", "Likes", "jazz"]) == 0
    assert "noted on" in capsys.readouterr().out
    assert contacts.main(["timeline", "Lionel Smith"]) == 0
    assert "note    Likes jazz" in capsys.readouterr().out
    assert contacts.main(["note", "zzz", "x"]) == 1
    assert "no contact matches" in capsys.readouterr().err
    assert contacts.main(["rollup", "--no-push"]) == 0
    assert contacts.main(["seed"]) == 0
    assert "would add 2 person(s)" in capsys.readouterr().out
