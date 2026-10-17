"""Contacts: people, not rooms (docs/proposals/2026-10-17-contacts.md).

Step 1, read-only: David's contacts in a SQLite store in the state dir
(`contacts.db`), and the match of Matrix rooms to the people in them, so
`/targets`' Matrix rows can carry `contact: {id, name}` (§6.1). Step 2's
read-only card is `GET /contacts/<id>` (`card`).

* **Sources.** `android`, the default (David, 17 Oct 2026: "I'm pretty
  unfamiliar with Google cloud. can you run it with ADB?"): the phone's
  contacts provider over `agent-phone-adb shell` (contacts_android.py),
  which already merges the com.google and com.whatsapp raw contacts into
  one aggregate per person. Read whole every `MEDIA_CONTACTS_INTERVAL_S`
  (900); a read the same as the last writes nothing; the phone off Wi-Fi
  keeps the last good copy and says why (`android_last_error`). `google`,
  optional and only once signed in: the People API (`contacts.readonly`)
  with a sync token. Where both know a person (by number, else address)
  Google's fields win; `MEDIA_CONTACTS_SOURCES` picks the sources.
* **The store.** Each source's `records`, and the merged `contacts`: names,
  phones (E.164, default region AU, `MEDIA_CONTACTS_REGION`), emails,
  addresses, organisations, notes, and `sources` (`[{kind: "android", id:
  <lookup key>, raw: [raw contact ids], accounts, version}, {kind:
  "google", id: "people/c123", etag}]`). A contact's id is a uuid made from
  its phone lookup key (`android:<lookup>`), else its Google resource
  name. Manual joins and splits of a room and a contact are kept in
  `overrides` and win over the matcher (no route writes them yet).
* **Google sign-in** (optional). The installed-app flow with PKCE and a
  loopback redirect, made for a phone: `python -m agent_media_server.contacts
  login` prints the consent URL and takes either the loopback hit or the
  redirect's address pasted back. The client is Google's Desktop JSON at
  `~/.config/agent-media/google-oauth-client.json` (or
  `MEDIA_GOOGLE_OAUTH_CLIENT`: a path, or the JSON itself); the refresh
  token is kept next to `secrets.env`, owner-only, in
  `~/.config/agent-media/google-contacts-token.json`.
* `MEDIA_CONTACTS=0` leaves the background sync off.

Contacts are personal data: they stay on this host, and nothing here sends
them anywhere but back to a signed-in device.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

log = logging.getLogger("agent-media.server.contacts")

SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
PEOPLE = "https://people.googleapis.com/v1"
PERSON_FIELDS = "names,phoneNumbers,emailAddresses,addresses,biographies,metadata"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
#: People a page of `connections`, the API's most.
PAGE_SIZE = 1000
#: How often the canvas syncs. The phone is read whole each time (a few
#: thousand rows over ssh and adb), so not often.
INTERVAL_S = 900.0
#: Rows `GET /contacts` answers by default, and at most.
LIMIT = 200
LIMIT_MAX = 5000
#: How long a login waits for the code before giving up.
LOGIN_WAIT_S = 900.0
#: A stable namespace for contact ids (uuid5 of "<kind>:<source id>").
_NS = uuid.UUID("5b0e6f8c-7d0a-4c55-9a4b-2f1d0c6e1a17")


# --- where things are -----------------------------------------------------------

def config_dir() -> Path:
    """`$XDG_CONFIG_HOME/agent-media`, where `secrets.env` is."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "agent-media"


def client_path() -> Path:
    return config_dir() / "google-oauth-client.json"


def token_path() -> Path:
    return config_dir() / "google-contacts-token.json"


def db_path() -> Path:
    from agent_media_core._paths import state_dir

    return state_dir() / "contacts.db"


def region() -> str:
    return (os.environ.get("MEDIA_CONTACTS_REGION") or "AU").strip().upper() or "AU"


# --- phone numbers --------------------------------------------------------------

# Regions whose national numbers start with a trunk 0: country code. AU is
# the one checked closely (its number lengths); the others are best effort.
_TRUNK0 = {"AU": "61", "NZ": "64", "GB": "44", "IE": "353", "DE": "49", "FR": "33",
           "IT": "39", "NL": "31", "IN": "91", "JP": "81", "ZA": "27"}
_E164 = re.compile(r"\+[1-9]\d{6,14}")


def _e164_ok(s: str) -> str | None:
    return s if _E164.fullmatch(s) else None


def e164(raw: str | None, default_region: str = "AU") -> str | None:
    """`raw` as E.164 (`+61412345678`), or None when it is not a number this
    can place: no digits, letters in it, a local number with no area code.

    `+…` and `00…` (and AU's `0011…`) are international as written; otherwise
    the number is national to `default_region`. An extension is dropped, and
    the "(0)" of "+61 (0)4…" too."""
    if not raw:
        return None
    s = str(raw).strip()
    if s.lower().startswith("tel:"):
        s = s[4:]
    s = re.sub(r"(?i)\s*(?:ext\.?|extn\.?|extension|x)\s*\d+\s*$", "", s)
    s = re.sub(r"[;,#].*$", "", s)
    s = s.replace("(0)", "")
    if re.search(r"[A-Za-z]", s):
        return None                    # a vanity number or not a number
    plus = s.lstrip().startswith("+")
    d = re.sub(r"\D", "", s)
    if not d:
        return None
    if plus:
        for cc in ("61", "64", "44", "353"):
            # "+61 0412 …": the trunk 0 written after the country code.
            if d.startswith(cc + "0"):
                d = cc + d[len(cc) + 1:]
                break
        return _e164_ok("+" + d)
    reg = (default_region or "AU").upper()
    if reg == "AU" and d.startswith("0011"):
        return _e164_ok("+" + d[4:])
    if d.startswith("00"):
        return _e164_ok("+" + d[2:])
    if reg == "AU":
        if len(d) == 10 and d[0] == "0" and d[1] in "23478":
            return "+61" + d[1:]       # 04xx xxx xxx, (03) xxxx xxxx
        if len(d) == 9 and d[0] in "23478":
            return "+61" + d           # the 0 left off
        if len(d) == 10 and d[:4] in ("1300", "1800", "1900"):
            return "+61" + d
        if len(d) == 6 and d[:2] == "13":
            return "+61" + d           # 13 xx xx
        if len(d) == 11 and d.startswith("61") and d[2] in "23478":
            return "+" + d             # 61412345678, no plus
        if len(d) == 12 and d.startswith("610") and d[3] in "23478":
            return "+61" + d[3:]
        return None
    if reg in ("US", "CA"):
        if len(d) == 10 and d[0] in "23456789":
            return "+1" + d
        if len(d) == 11 and d[0] == "1":
            return "+" + d
        return None
    cc = _TRUNK0.get(reg)
    if cc and d.startswith("0") and len(d) > 6:
        return _e164_ok("+" + cc + d[1:])
    if cc and d.startswith(cc):
        return _e164_ok("+" + d)
    return None


def _national(num: str) -> str:
    """An E.164 AU number as dialled at home (`+61412…` → `0412…`); others as
    their digits."""
    if num.startswith("+61") and len(num) == 12:
        return "0" + num[3:]
    return num.lstrip("+")


# --- names, for matching and searching ------------------------------------------

def fold(s: str) -> str:
    """Case, accents and spacing folded: what two spellings of a name share."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.casefold().split())


_EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")


# --- the store ------------------------------------------------------------------

# `records` is what each source said, one row per source contact; `contacts`
# (with `phones` and `emails` to match by) is the merged book, rebuilt from
# the records after every change (`_rebuild`).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS records (
  kind TEXT NOT NULL, sid TEXT NOT NULL, card TEXT NOT NULL, meta TEXT NOT NULL,
  PRIMARY KEY (kind, sid));
CREATE TABLE IF NOT EXISTS contacts (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, card TEXT NOT NULL, updated REAL);
CREATE TABLE IF NOT EXISTS phones (contact TEXT NOT NULL, e164 TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS phones_e164 ON phones(e164);
CREATE INDEX IF NOT EXISTS phones_contact ON phones(contact);
CREATE TABLE IF NOT EXISTS emails (contact TEXT NOT NULL, email TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS emails_email ON emails(email);
CREATE INDEX IF NOT EXISTS emails_contact ON emails(contact);
CREATE TABLE IF NOT EXISTS overrides (
  room TEXT NOT NULL, contact TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('join', 'split')), at REAL,
  PRIMARY KEY (room, contact));
"""
#: Bump when what a row holds changes: the store is emptied (but for the
#: overrides) and read again.
SCHEMA_VERSION = "2"

_WRITE = threading.Lock()
_SYNC_LOCK = threading.Lock()


def _connect(create: bool = True) -> sqlite3.Connection | None:
    """A connection to the store; None when it does not exist and `create`
    is false (a server that never synced has no contacts.db, and a read must
    not make one)."""
    path = db_path()
    if not create and not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "meta" in have and conn.execute(
            "SELECT v FROM meta WHERE k='schema'").fetchone() != (SCHEMA_VERSION,):
        for t in have - {"overrides"}:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('schema', ?)", (SCHEMA_VERSION,))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def _meta(conn: sqlite3.Connection, k: str) -> str | None:
    got = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return got[0] if got else None


def _set_meta(conn: sqlite3.Connection, k: str, v: str | None) -> None:
    if v is None:
        conn.execute("DELETE FROM meta WHERE k=?", (k,))
    else:
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))


def _bump(conn: sqlite3.Connection) -> None:
    """A change the room matcher must see: its cache is keyed by this."""
    _set_meta(conn, "gen", str(int(_meta(conn, "gen") or 0) + 1))


def contact_id(kind: str, sid: str) -> str:
    return str(uuid.uuid5(_NS, f"{kind}:{sid}"))


def _source(kind: str, sid: str, meta: dict) -> dict:
    if kind == "android":
        return {"kind": "android", "id": sid, "raw": meta.get("raw") or [],
                "accounts": meta.get("accounts") or [], "version": meta.get("version") or 0}
    return {"kind": kind, "id": sid, "etag": meta.get("etag") or ""}


def _keys(card: dict) -> tuple[set, set]:
    return ({p["value"] for p in card.get("phones") or [] if p.get("value")},
            {e["value"].lower() for e in card.get("emails") or [] if e.get("value")})


#: The card's list fields. Google's value wins where it has one (the
#: proposal's "Google wins"); the phone's fills the rest.
_FIELDS = ("names", "phones", "emails", "addresses", "organizations")


def _merge(google: dict, phone: dict) -> dict:
    out = {f: list(google.get(f) or []) or list(phone.get(f) or []) for f in _FIELDS}
    out["notes"] = google.get("notes") or phone.get("notes") or ""
    gname = google.get("name") or ""
    out["name"] = gname if gname and gname != "(no name)" else (phone.get("name") or gname)
    return out


def _rebuild(conn: sqlite3.Connection) -> None:
    """The merged book from the records: one contact per phone contact (its
    id from its lookup key); a Google contact sharing a number, else an
    address, with exactly one of them joins it, else stands as its own."""
    recs = conn.execute("SELECT kind, sid, card, meta FROM records ORDER BY kind, sid").fetchall()
    book: dict[str, dict] = {}
    by_phone: dict[str, set] = {}
    by_email: dict[str, set] = {}
    for kind, sid, card, meta in recs:
        if kind != "android":
            continue
        cid = contact_id("android", sid)
        c = json.loads(card)
        book[cid] = {"card": c, "sources": [_source(kind, sid, json.loads(meta))]}
        phones, emails = _keys(c)
        for p in phones:
            by_phone.setdefault(p, set()).add(cid)
        for e in emails:
            by_email.setdefault(e, set()).add(cid)
    for kind, sid, card, meta in recs:
        if kind == "android":
            continue
        c = json.loads(card)
        phones, emails = _keys(c)
        hits: set = set()
        for p in phones:
            hits |= by_phone.get(p, set())
        if not hits:
            for e in emails:
                hits |= by_email.get(e, set())
        # One Google contact per phone contact.
        hits = {h for h in hits if not any(s["kind"] == kind for s in book[h]["sources"])}
        src = _source(kind, sid, json.loads(meta))
        if len(hits) == 1:
            cid = next(iter(hits))
            book[cid] = {"card": _merge(c, book[cid]["card"]),
                         "sources": [src] + book[cid]["sources"]}
        else:
            book[contact_id(kind, sid)] = {"card": c, "sources": [src]}
    old = {cid: (raw, upd) for cid, raw, upd in conn.execute(
        "SELECT id, card, updated FROM contacts")}
    for t in ("contacts", "phones", "emails"):
        conn.execute(f"DELETE FROM {t}")
    now = time.time()
    for cid, entry in book.items():
        c = {**entry["card"], "sources": entry["sources"]}
        raw = json.dumps(c, sort_keys=True)
        was = old.get(cid)
        conn.execute("INSERT INTO contacts (id, name, card, updated) VALUES (?, ?, ?, ?)",
                     (cid, c.get("name") or "(no name)", raw,
                      was[1] if was and was[0] == raw else now))
        phones, emails = _keys(c)
        conn.executemany("INSERT INTO phones (contact, e164) VALUES (?, ?)",
                         [(cid, p) for p in sorted(phones)])
        conn.executemany("INSERT INTO emails (contact, email) VALUES (?, ?)",
                         [(cid, e) for e in sorted(emails)])
    _bump(conn)


def set_override(room: str, contact: str, kind: str) -> None:
    """A manual decision for a room: `join` (this room is this contact's) or
    `split` (it is not, whatever the matcher thinks). Kept; wins."""
    if kind not in ("join", "split"):
        raise ValueError(kind)
    with _WRITE:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if kind == "join":
                # One person per room: a join replaces any other join there.
                conn.execute("DELETE FROM overrides WHERE room=? AND kind='join'", (room,))
            conn.execute("INSERT OR REPLACE INTO overrides (room, contact, kind, at) "
                         "VALUES (?, ?, ?, ?)", (room, contact, kind, time.time()))
            _bump(conn)
            conn.execute("COMMIT")
        finally:
            conn.close()


def clear_override(room: str, contact: str) -> None:
    with _WRITE:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM overrides WHERE room=? AND contact=?", (room, contact))
            _bump(conn)
            conn.execute("COMMIT")
        finally:
            conn.close()


# --- a Google person into a card ------------------------------------------------

def _primary_first(items: list) -> list:
    return sorted(items or [], key=lambda x: not ((x.get("metadata") or {}).get("primary")))


def card_of(person: dict, default_region: str = "AU") -> dict:
    """The People API's person as the store keeps it."""
    names = [{"display": n.get("displayName") or "", "given": n.get("givenName") or "",
              "family": n.get("familyName") or ""}
             for n in _primary_first(person.get("names"))]
    phones = []
    for p in _primary_first(person.get("phoneNumbers")):
        raw = p.get("value") or ""
        num = e164(p.get("canonicalForm"), default_region) or e164(raw, default_region)
        phones.append({"value": num, "raw": raw,
                       "type": p.get("formattedType") or p.get("type") or ""})
    emails = [{"value": (e.get("value") or "").strip(),
               "type": e.get("formattedType") or e.get("type") or ""}
              for e in _primary_first(person.get("emailAddresses")) if e.get("value")]
    addresses = []
    for a in _primary_first(person.get("addresses")):
        text = a.get("formattedValue") or ", ".join(
            x for x in (a.get("streetAddress"), a.get("extendedAddress"), a.get("city"),
                        a.get("region"), a.get("postalCode"), a.get("country")) if x)
        if text:
            addresses.append({"formatted": text,
                              "type": a.get("formattedType") or a.get("type") or ""})
    notes = "\n\n".join(b.get("value") or "" for b in person.get("biographies") or []
                        if b.get("value")).strip()
    name = next((n["display"] for n in names if n["display"]), "") \
        or next((e["value"] for e in emails), "") \
        or next((p["raw"] for p in phones if p["raw"]), "") or "(no name)"
    return {"name": name, "names": names, "phones": phones, "emails": emails,
            "addresses": addresses, "organizations": [], "notes": notes}


# --- HTTP -----------------------------------------------------------------------

def _request(method: str, url: str, *, form: dict | None = None,
             headers: dict | None = None, timeout: float = 20.0) -> tuple[int, dict]:
    """`(status, JSON body)`; an HTTP error is a status, not an exception (a
    network failure still raises). The one seam the tests replace."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    hdrs = dict(headers or {})
    if form is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {"raw": raw[:500].decode("utf-8", "replace")}
    return status, body if isinstance(body, dict) else {"value": body}


# --- OAuth ----------------------------------------------------------------------

class NotSignedIn(Exception):
    """No client, no refresh token, or Google no longer takes it."""


class SyncError(Exception):
    pass


def load_client() -> dict | None:
    """The OAuth client: `{client_id, client_secret, auth_uri, token_uri}`
    from Google's Desktop ("installed") JSON; None when there is none."""
    raw = (os.environ.get("MEDIA_GOOGLE_OAUTH_CLIENT") or "").strip()
    try:
        if raw.startswith("{"):
            data = json.loads(raw)
        else:
            path = Path(raw).expanduser() if raw else client_path()
            data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    c = data.get("installed") or data.get("web") or data
    if not isinstance(c, dict) or not c.get("client_id"):
        return None
    return {"client_id": c["client_id"], "client_secret": c.get("client_secret") or "",
            "auth_uri": c.get("auth_uri") or AUTH_URI,
            "token_uri": c.get("token_uri") or TOKEN_URI}


def load_token() -> dict | None:
    try:
        data = json.loads(token_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("refresh_token") else None


def _save_token(data: dict) -> None:
    """Owner-only from the first byte, and atomic (devices.py's way)."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def signed_in() -> bool:
    return load_client() is not None and load_token() is not None


def pkce() -> tuple[str, str]:
    """`(verifier, S256 challenge)`, RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def consent_url(client: dict, redirect_uri: str, state: str, challenge: str) -> str:
    return client["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect_uri,
        "response_type": "code", "scope": SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        # A refresh token, every time: a second login must get one too.
        "access_type": "offline", "prompt": "consent"})


def code_from(text: str, state: str) -> str:
    """The authorisation code in what came back: the redirect's full address
    (pasted from a phone's address bar), its path and query (the loopback
    hit), or the bare code. A URL's `state` must be ours."""
    text = (text or "").strip().strip("'\"<>")
    if not text:
        raise ValueError("nothing pasted")
    if "?" in text or "code=" in text or "error=" in text:
        query = urllib.parse.urlsplit(text).query or text.partition("?")[2] or text
        qs = urllib.parse.parse_qs(query)
        if qs.get("error"):
            raise ValueError(f"Google answered: {qs['error'][0]}")
        if (qs.get("state") or [""])[0] != state:
            raise ValueError("that address is from a different sign-in (its state does not match)")
        code = (qs.get("code") or [""])[0]
        if not code:
            raise ValueError("there is no code in that address")
        return code
    if re.fullmatch(r"[\w/.~-]{10,}", text):
        return text
    raise ValueError("that is not the address Google sent you to")


_ACCESS: dict = {}                    # {"token", "expires"}, this process only


def _store_access(body: dict) -> None:
    _ACCESS.clear()
    _ACCESS.update(token=body["access_token"],
                   expires=time.time() + float(body.get("expires_in") or 3600))


def exchange(client: dict, code: str, verifier: str, redirect_uri: str) -> dict:
    """The code for tokens; the refresh token is kept. Answers what was kept."""
    status, body = _request("POST", client["token_uri"], form={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "redirect_uri": redirect_uri, "client_id": client["client_id"],
        **({"client_secret": client["client_secret"]} if client["client_secret"] else {})})
    if status != 200 or not body.get("access_token"):
        raise NotSignedIn(f"Google refused the code ({status}: "
                          f"{body.get('error_description') or body.get('error') or body})")
    if not body.get("refresh_token"):
        raise NotSignedIn("Google sent no refresh token; sign in again "
                          "(remove the app's access at myaccount.google.com first)")
    kept = {"refresh_token": body["refresh_token"], "scope": body.get("scope") or SCOPE,
            "client_id": client["client_id"], "obtained_at": round(time.time(), 3)}
    _save_token(kept)
    _store_access(body)
    return kept


def access_token() -> str:
    """A current access token, refreshed when it is near its end."""
    if _ACCESS.get("token") and _ACCESS.get("expires", 0) - 60 > time.time():
        return _ACCESS["token"]
    client, tok = load_client(), load_token()
    if client is None:
        raise NotSignedIn(f"no OAuth client ({client_path()})")
    if tok is None:
        raise NotSignedIn("not signed in (python -m agent_media_server.contacts login)")
    status, body = _request("POST", client["token_uri"], form={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": client["client_id"],
        **({"client_secret": client["client_secret"]} if client["client_secret"] else {})})
    if status != 200 or not body.get("access_token"):
        err = body.get("error") or status
        if err == "invalid_grant":
            raise NotSignedIn("Google no longer takes the sign-in (revoked or expired); "
                              "run login again")
        raise SyncError(f"token refresh failed ({err})")
    if body.get("refresh_token") and body["refresh_token"] != tok["refresh_token"]:
        _save_token({**tok, "refresh_token": body["refresh_token"]})
    _store_access(body)
    return _ACCESS["token"]


# --- the sync -------------------------------------------------------------------

class _Expired(Exception):
    pass


def _fetch(sync_token: str | None) -> tuple[list[dict], str | None]:
    """Every Google connection (or every change since `sync_token`), and the
    token for next time."""
    people: list[dict] = []
    page: str | None = None
    retried = False
    while True:
        params = {"personFields": PERSON_FIELDS, "pageSize": str(PAGE_SIZE),
                  "requestSyncToken": "true"}
        if page:
            params["pageToken"] = page
        if sync_token:
            params["syncToken"] = sync_token
        status, body = _request(
            "GET", f"{PEOPLE}/people/me/connections?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {access_token()}"})
        if status == 401 and not retried:
            _ACCESS.clear()                    # a token Google ended early
            retried = True
            continue
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        if status == 410 or (status == 400 and "EXPIRED_SYNC_TOKEN" in json.dumps(err)):
            raise _Expired()
        if status != 200:
            raise SyncError(f"People API {status}: {err.get('message') or body}")
        people += body.get("connections") or []
        page = body.get("nextPageToken")
        if not page:
            return people, body.get("nextSyncToken")


def _failed(kind: str, e: Exception) -> None:
    """A source that could not be read: said, and the last good copy kept."""
    conn = _connect()
    try:
        _set_meta(conn, f"{kind}_last_error", str(e) or type(e).__name__)
        _set_meta(conn, f"{kind}_failed_at", str(round(time.time(), 3)))
    finally:
        conn.close()


def sync_google(full: bool = False) -> dict:
    """Bring Google's records up to date. A sync token makes it the changes
    only; none, or an expired one, makes it everything."""
    with _SYNC_LOCK:
        conn = _connect()
        try:
            token = None if full else _meta(conn, "google_sync_token")
        finally:
            conn.close()
        try:
            try:
                people, next_token = _fetch(token)
            except _Expired:
                log.info("contacts: sync token expired, full resync")
                token = None
                people, next_token = _fetch(None)
        except (NotSignedIn, SyncError, OSError, urllib.error.URLError) as e:
            _failed("google", e)
            raise
        reg = region()
        changed = deleted = 0
        with _WRITE:
            conn = _connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                seen = set()
                for p in people:
                    rn = p.get("resourceName") or ""
                    if not rn:
                        continue
                    if (p.get("metadata") or {}).get("deleted"):
                        deleted += conn.execute("DELETE FROM records WHERE kind='google' "
                                                "AND sid=?", (rn,)).rowcount
                        continue
                    seen.add(rn)
                    conn.execute("INSERT OR REPLACE INTO records (kind, sid, card, meta) "
                                 "VALUES ('google', ?, ?, ?)",
                                 (rn, json.dumps(card_of(p, reg)),
                                  json.dumps({"etag": p.get("etag") or ""})))
                    changed += 1
                if token is None:
                    # Everything was read: what is not in it is gone.
                    for (sid,) in conn.execute(
                            "SELECT sid FROM records WHERE kind='google'").fetchall():
                        if sid not in seen:
                            conn.execute("DELETE FROM records WHERE kind='google' AND sid=?",
                                         (sid,))
                            deleted += 1
                _set_meta(conn, "google_sync_token", next_token)
                _set_meta(conn, "google_synced_at", str(round(time.time(), 3)))
                _set_meta(conn, "google_last_error", None)
                if changed or deleted:
                    _rebuild(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
                conn.close()
        return {"source": "google", "full": token is None, "changed": changed,
                "deleted": deleted, "contacts": total}


def sync_android() -> dict:
    """Read every contact on the phone (contacts_android.py). Unreachable —
    off Wi-Fi, adb down — raises, and the last good copy stays. A read the
    same as the last one writes nothing."""
    from . import contacts_android

    with _SYNC_LOCK:
        try:
            recs, digest = contacts_android.read(region())
        except contacts_android.Unreachable as e:
            _failed("android", e)
            raise
        with _WRITE:
            conn = _connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                same = _meta(conn, "android_hash") == digest
                before = {sid for (sid,) in conn.execute(
                    "SELECT sid FROM records WHERE kind='android'")}
                if not same:
                    conn.execute("DELETE FROM records WHERE kind='android'")
                    conn.executemany(
                        "INSERT INTO records (kind, sid, card, meta) VALUES ('android', ?, ?, ?)",
                        [(sid, json.dumps(r["card"]), json.dumps(r["meta"]))
                         for sid, r in recs.items()])
                    _rebuild(conn)
                    _set_meta(conn, "android_hash", digest)
                _set_meta(conn, "android_synced_at", str(round(time.time(), 3)))
                _set_meta(conn, "android_last_error", None)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
                conn.close()
        return {"source": "android", "unchanged": same, "read": len(recs),
                "gone": len(before - set(recs)) if not same else 0, "contacts": total}


def sources() -> list[str]:
    """The sources this host reads: `MEDIA_CONTACTS_SOURCES` (comma
    separated), else the phone when this host has the adb tool, and Google
    only when it is signed in."""
    from . import contacts_android

    raw = (os.environ.get("MEDIA_CONTACTS_SOURCES") or "").strip()
    if raw:
        want = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        want = ["android", "google"]
    out = []
    for s in want:
        if s == "android" and contacts_android.available():
            out.append(s)
        elif s == "google" and signed_in():
            out.append(s)
    return out


def sync(full: bool = False) -> dict:
    """Every source this host reads, each on its own: one that fails is said
    (`errors`) and the others still sync."""
    done, errors = [], {}
    for s in sources():
        try:
            done.append(sync_android() if s == "android" else sync_google(full=full))
        except Exception as e:  # noqa: BLE001 — one source down is not all of them
            errors[s] = str(e) or type(e).__name__
    return {"synced": done, "errors": errors}


# --- reading the store ----------------------------------------------------------

def _row(cid: str, name: str, card: dict) -> dict:
    return {"id": cid, "name": name,
            "phones": [p["value"] for p in card.get("phones") or [] if p.get("value")],
            "emails": [e["value"] for e in card.get("emails") or [] if e.get("value")]}


def _matches(q: str, name: str, card: dict) -> int:
    """How well `q` matches: 0 not at all, 1 inside a field, 2 a name's
    start or a word's (the as-you-type order)."""
    fq = fold(q)
    if not fq:
        return 2
    fn = fold(name) + " " + " ".join(fold(n.get("display") or "") for n in card.get("names") or [])
    if fn.startswith(fq) or any(w.startswith(fq) for w in fn.split()):
        return 2
    if fq in fn:
        return 1
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 3 and len(digits) >= len(re.sub(r"\s", "", q)) - 2:
        for p in card.get("phones") or []:
            num = p.get("value") or ""
            if num and (digits in num or digits in _national(num)):
                return 1
            if digits in re.sub(r"\D", "", p.get("raw") or ""):
                return 1
    if any(fq in (e.get("value") or "").lower() for e in card.get("emails") or []):
        return 1
    return 0


def _source_states(conn: sqlite3.Connection | None) -> list[dict]:
    out = []
    for kind in ("android", "google"):
        synced = _meta(conn, f"{kind}_synced_at") if conn is not None else None
        err = _meta(conn, f"{kind}_last_error") if conn is not None else None
        if synced or err or kind in sources():
            out.append({"kind": kind, "synced_at": float(synced) if synced else None,
                        "error": err})
    return out


def listing(q: str = "", limit: int | str | None = None) -> dict:
    """`GET /contacts`: everyone, or those `q` matches as it is typed (name,
    a phone's digits as written or dialled at home, an email)."""
    try:
        n = max(1, min(int(limit or LIMIT), LIMIT_MAX))
    except (TypeError, ValueError):
        n = LIMIT
    conn = _connect(create=False)
    rows: list[tuple[int, str, dict]] = []
    try:
        if conn is not None:
            for cid, name, raw in conn.execute("SELECT id, name, card FROM contacts"):
                card = json.loads(raw)
                score = _matches(q, name, card) if q else 2
                if score:
                    rows.append((score, fold(name), _row(cid, name, card)))
        states = _source_states(conn)
    finally:
        if conn is not None:
            conn.close()
    rows.sort(key=lambda r: (-r[0], r[1]))
    synced = [s["synced_at"] for s in states if s["synced_at"]]
    return {"contacts": [r[2] for r in rows[:n]], "total": len(rows),
            "synced_at": max(synced) if synced else None, "sources": states}


def card(cid: str) -> dict | None:
    """`GET /contacts/<id>`'s contact: the merged card, its sources, and the
    rooms that are this person's."""
    conn = _connect(create=False)
    if conn is None:
        return None
    try:
        got = conn.execute("SELECT name, card, updated FROM contacts WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    if not got:
        return None
    c = json.loads(got[1])
    return {"id": cid, "name": got[0], "names": c.get("names") or [],
            "phones": c.get("phones") or [], "emails": c.get("emails") or [],
            "addresses": c.get("addresses") or [], "organizations": c.get("organizations") or [],
            "notes": c.get("notes") or "", "sources": c.get("sources") or [],
            "updated": round(got[2], 3) if got[2] else None, "rooms": rooms_of(cid)}


# --- rooms to people ------------------------------------------------------------

def _unescape(local: str) -> str:
    """A mautrix localpart's `=xx` escapes (`=2b` is `+`)."""
    return re.sub(r"=([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), local)


def _looks_like_phone(s: str) -> bool:
    return bool(re.fullmatch(r"\+?[\d\s().\-]{6,}", s or "")) and \
        len(re.sub(r"\D", "", s)) >= 6


def identities(user_id: str, displayname: str = "", default_region: str = "AU") -> dict:
    """What a room member says about who they are: `{"phones", "emails",
    "name"}`.

    The bridges' puppets: mautrix-gmessages names them `@gmessages_<login>.
    <participant>:server` (`gmessages_1.42` — no number in the id) with the
    displayname `{{or .FullName .PhoneNumber}}`: the phone's own contact
    name, else the number. Signal's are `@signal_<uuid>` (older ones
    `@signal_=2b61…`, the escaped number), WhatsApp's `@whatsapp_61412…`.
    So a number comes from the id when it is one, and from a displayname that
    is one; the displayname is otherwise the name to try."""
    phones: set[str] = set()
    emails: set[str] = set()
    local = (user_id or "").lstrip("@").partition(":")[0]
    _prefix, sep, rest = local.partition("_")
    if sep:
        rest = _unescape(rest)
        if re.fullmatch(r"\+\d{7,15}", rest):
            num = e164(rest, default_region)
        elif re.fullmatch(r"[1-9]\d{7,14}", rest):
            num = e164("+" + rest, default_region)
        else:
            num = None
        if num:
            phones.add(num)
    dn = (displayname or "").strip()
    name = ""
    if dn and dn != user_id:
        if _looks_like_phone(dn):
            num = e164(dn, default_region)
            if num:
                phones.add(num)
        elif _EMAIL.fullmatch(dn):
            emails.add(dn.lower())
        else:
            name = fold(dn)
    return {"phones": phones, "emails": emails, "name": name}


_INDEX: dict = {"gen": None, "checked": 0.0}
_ROOM_MEMO: dict = {}
#: How long the store's generation is trusted before it is read again: a
#: sync from the CLI (another process) shows within this.
_GEN_TTL_S = 2.0


def _index() -> dict | None:
    """Phones, emails and names to contact ids, and the overrides; rebuilt
    when the store changes. None when there is no store."""
    now = time.monotonic()
    if _INDEX.get("gen") is not None and now - _INDEX["checked"] < _GEN_TTL_S:
        return _INDEX
    conn = _connect(create=False)
    if conn is None:
        _INDEX.update(gen=None, checked=now)
        return None
    try:
        gen = _meta(conn, "gen") or "0"
        if gen == _INDEX.get("gen"):
            _INDEX["checked"] = now
            return _INDEX
        phones: dict[str, set] = {}
        emails: dict[str, set] = {}
        names: dict[str, set] = {}
        display: dict[str, str] = {}
        for cid, num in conn.execute("SELECT contact, e164 FROM phones"):
            phones.setdefault(num, set()).add(cid)
        for cid, em in conn.execute("SELECT contact, email FROM emails"):
            emails.setdefault(em, set()).add(cid)
        for cid, name, raw in conn.execute("SELECT id, name, card FROM contacts"):
            display[cid] = name
            c = json.loads(raw)
            for n in {fold(name)} | {fold(x.get("display") or "") for x in c.get("names") or []}:
                if n:
                    names.setdefault(n, set()).add(cid)
        joins: dict[str, str] = {}
        splits: dict[str, set] = {}
        for room, cid, kind in conn.execute("SELECT room, contact, kind FROM overrides"):
            if kind == "join":
                joins[room] = cid
            else:
                splits.setdefault(room, set()).add(cid)
    finally:
        conn.close()
    _INDEX.clear()
    _INDEX.update(gen=gen, checked=now, phones=phones, emails=emails, names=names,
                  display=display, joins=joins, splits=splits)
    _ROOM_MEMO.clear()
    return _INDEX


def _self_ids() -> tuple[set, set]:
    """`MEDIA_CONTACTS_SELF`: David's own numbers and addresses, comma
    separated. Texts sent from the phone itself come in as the number's
    puppet (no double puppeting), so his own number is in every SMS room."""
    phones, emails = set(), set()
    for x in (os.environ.get("MEDIA_CONTACTS_SELF") or "").split(","):
        x = x.strip()
        if "@" in x:
            emails.add(x.lower())
        elif x and (num := e164(x, region())):
            phones.add(num)
    return phones, emails


def match_room(room_id: str, members: dict[str, str], skip: set[str]) -> dict | None:
    """`{id, name}` of the one contact a room is with, or None.

    Each member other than `skip` (the owner, the agent, the bridge bots) is
    matched by phone, else email, else — the bridge's name for them being
    the phone's own contact name — a name no other contact has. A room is a
    contact's when its members match exactly one; a group of several people
    is no one's. A manual join wins; a split takes that contact out."""
    idx = _index()
    if idx is None:
        return None
    if room_id in idx["joins"]:
        cid = idx["joins"][room_id]
        name = idx["display"].get(cid)
        return {"id": cid, "name": name} if name is not None else None
    reg = region()
    self_phones, self_emails = _self_ids()
    split = idx["splits"].get(room_id, set())
    found: set[str] = set()
    for uid, dn in members.items():
        if uid in skip:
            continue
        who = identities(uid, dn, reg)
        got: set[str] = set()
        for num in who["phones"] - self_phones:
            got |= idx["phones"].get(num, set())
        if not got:
            for em in who["emails"] - self_emails:
                got |= idx["emails"].get(em, set())
        if not got and who["name"]:
            by_name = idx["names"].get(who["name"], set())
            if len(by_name) == 1:
                got = set(by_name)
        got -= split
        if len(got) == 1:
            found |= got
        elif len(got) > 1:
            return None                # one number on two contacts: no guess
    if len(found) != 1:
        return None
    cid = next(iter(found))
    return {"id": cid, "name": idx["display"].get(cid) or ""}


def _skip_of(r) -> set[str]:
    from . import matrix

    bridges = matrix._bridges()
    skip = {r.owner, os.environ.get("MATRIX_SAM_ID") or ""} | bridges
    for uid in r.members:
        local = uid.lstrip("@").partition(":")[0]
        if "_" not in local and local.endswith("bot"):
            skip.add(uid)              # a bridge's bot, listed or not
    return skip


def for_room(r) -> dict | None:
    """The `contact` of a Matrix row (§6.1): `{id, name}`, or None. Cheap:
    remembered per room until its members or the store change."""
    idx = _index()
    if idx is None:
        return None
    with r.lock:
        members = dict(r.members)
    key = (idx["gen"], r.id, tuple(sorted(members.items())),
           os.environ.get("MEDIA_CONTACTS_SELF") or "")
    if key in _ROOM_MEMO:
        return _ROOM_MEMO[key]
    got = match_room(r.id, members, _skip_of(r))
    if len(_ROOM_MEMO) > 2000:
        _ROOM_MEMO.clear()
    _ROOM_MEMO[key] = got
    return got


def rooms_of(cid: str) -> list[dict]:
    """The Matrix rooms (sessions) that are this contact's, newest first."""
    from . import matrix

    out = []
    for r in list(matrix._ROOMS.values()):
        got = for_room(r)
        if not got or got["id"] != cid:
            continue
        with r.lock:
            last = r.messages[-1]["at"] if r.messages else None
        out.append({"session": r.thread, "room": r.id, "title": r.title(),
                    "network": r.network, "at": last})
    out.sort(key=lambda x: -(x["at"] or 0))
    return out


def _reset_for_tests() -> None:
    _INDEX.clear()
    _INDEX.update(gen=None, checked=0.0)
    _ROOM_MEMO.clear()
    _ACCESS.clear()


# --- the background sync --------------------------------------------------------

_STARTED = threading.Event()


def start() -> None:
    """The canvas's sync loop, once per process: the phone every
    `MEDIA_CONTACTS_INTERVAL_S` (900), Google too when signed in; idle on a
    host with neither; off with `MEDIA_CONTACTS=0`."""
    if _STARTED.is_set() or (os.environ.get("MEDIA_CONTACTS") or "1").strip() == "0":
        return
    _STARTED.set()
    threading.Thread(target=_run, name="contacts-sync", daemon=True).start()


def _run() -> None:
    try:
        interval = float(os.environ.get("MEDIA_CONTACTS_INTERVAL_S") or INTERVAL_S)
    except ValueError:
        interval = INTERVAL_S
    time.sleep(15)
    while True:
        try:
            got = sync()
            for s in got["synced"]:
                if s.get("changed") or s.get("deleted") or s.get("unchanged") is False:
                    print(f"contacts: {s['source']}: {s['contacts']} contacts", file=sys.stderr)
            for s, e in got["errors"].items():
                # The phone off Wi-Fi is the usual one: the last copy serves.
                print(f"contacts: {s} unread ({e})", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — never take the canvas down
            print(f"contacts: sync failed ({e})", file=sys.stderr)
        time.sleep(max(30.0, interval))


# --- the sign-in, for a phone ---------------------------------------------------

class Login:
    """One sign-in: the consent URL, a loopback listener for the redirect,
    and `finish` for whichever answer comes first."""

    def __init__(self, client: dict, port: int = 0) -> None:
        self.client = client
        self.state = secrets.token_urlsafe(24)
        self.verifier, challenge = pkce()
        self.answers: "queue.Queue[tuple[str, str]]" = queue.Queue()
        login = self

        class Hit(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = self.path.partition("?")[2]
                if "code=" not in q and "error=" not in q:
                    self.send_response(404)
                    self.end_headers()
                    return
                login.answers.put(("loopback", self.path))
                body = (b"<!doctype html><meta charset=utf-8><title>agent-media</title>"
                        b"<p>Got it. You can close this page.</p>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", port), Hit)
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url = consent_url(client, self.redirect_uri, self.state, challenge)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                         name="contacts-login", daemon=True).start()

    def finish(self, text: str) -> dict:
        """Exchange what came back; ValueError when it is not ours."""
        return exchange(self.client, code_from(text, self.state), self.verifier,
                        self.redirect_uri)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _read_stdin(answers: queue.Queue) -> None:
    for line in sys.stdin:
        if line.strip():
            answers.put(("paste", line))


def cmd_login(a) -> int:
    client = load_client()
    if client is None:
        print(f"No OAuth client. Put Google's Desktop-app client JSON at\n  {client_path()}\n"
              "or set MEDIA_GOOGLE_OAUTH_CLIENT to its path.", file=sys.stderr)
        return 2
    try:
        port = int(a.port or os.environ.get("MEDIA_CONTACTS_OAUTH_PORT") or 0)
    except ValueError:
        port = 0
    login = Login(client, port)
    try:
        print("Open this address and sign in to Google (any device):\n")
        print(login.url + "\n")
        print("Google then sends you to " + login.redirect_uri + ". On this machine that "
              "finishes by itself.\nOn a phone that page won't load: copy the whole "
              "address from the address bar and paste it here, then Enter.\n")
        threading.Thread(target=_read_stdin, args=(login.answers,), daemon=True).start()
        deadline = time.monotonic() + LOGIN_WAIT_S
        while True:
            try:
                src, text = login.answers.get(timeout=max(1.0, deadline - time.monotonic()))
            except queue.Empty:
                print("Gave up waiting for the sign-in.", file=sys.stderr)
                return 1
            try:
                login.finish(text)
            except ValueError as e:
                print(f"{e}. Try again.", file=sys.stderr)
                continue
            except NotSignedIn as e:
                print(str(e), file=sys.stderr)
                return 1
            print(f"Signed in ({'redirect' if src == 'loopback' else 'pasted address'}). "
                  f"Token kept in {token_path()}.")
            break
    finally:
        login.close()
    return cmd_sync(argparse.Namespace(full=True, source="google"))


def status() -> dict:
    from . import contacts_android

    conn = _connect(create=False)
    count, per = 0, {}
    try:
        if conn is not None:
            count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            for kind in ("android", "google"):
                n = conn.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()[0]
                synced = _meta(conn, f"{kind}_synced_at")
                failed = _meta(conn, f"{kind}_failed_at")
                per[kind] = {"records": n, "synced_at": float(synced) if synced else None,
                             "failed_at": float(failed) if failed else None,
                             "last_error": _meta(conn, f"{kind}_last_error")}
            per.setdefault("google", {})["sync_token"] = bool(_meta(conn, "google_sync_token"))
    finally:
        if conn is not None:
            conn.close()
    tok = load_token()
    return {"store": str(db_path()), "contacts": count, "sources": sources(),
            "android": {"command": " ".join(contacts_android.command()),
                        "available": contacts_android.available(), **per.get("android", {})},
            "google": {"client": str(client_path()) if not os.environ.get("MEDIA_GOOGLE_OAUTH_CLIENT")
                       else "MEDIA_GOOGLE_OAUTH_CLIENT",
                       "client_found": load_client() is not None,
                       "token": str(token_path()), "signed_in": tok is not None,
                       "signed_in_at": tok.get("obtained_at") if tok else None,
                       **per.get("google", {})},
            "background": (os.environ.get("MEDIA_CONTACTS") or "1").strip() != "0"}


def _when(v):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v)) if isinstance(v, float) else v


def cmd_status(_a) -> int:
    s = status()
    for k in ("store", "contacts", "sources", "background"):
        print(f"{k:13} {s[k]}")
    for src in ("android", "google"):
        for k, v in s[src].items():
            print(f"{src + '.' + k:24} {_when(v) if k.endswith('_at') else v}")
    return 0 if s["sources"] else 1


def cmd_sync(a) -> int:
    which = getattr(a, "source", "") or ""
    if which:
        try:
            got = {"synced": [sync_android() if which == "android"
                              else sync_google(full=bool(getattr(a, "full", False)))],
                   "errors": {}}
        except Exception as e:  # noqa: BLE001 — said, with its source
            got = {"synced": [], "errors": {which: str(e)}}
    else:
        got = sync(full=bool(getattr(a, "full", False)))
        if not got["synced"] and not got["errors"]:
            print("no source to read: no phone tool here, and not signed in to Google",
                  file=sys.stderr)
            return 2
    for s in got["synced"]:
        if s["source"] == "android":
            print(f"android: {'unchanged' if s['unchanged'] else 'read'} {s['read']} "
                  f"contact(s) from the phone, {s['contacts']} contacts")
        else:
            print(f"google {'full' if s['full'] else 'changes'}: {s['changed']} changed, "
                  f"{s['deleted']} gone, {s['contacts']} contacts")
    for src, e in got["errors"].items():
        print(f"{src}: sync failed: {e}", file=sys.stderr)
    return 1 if got["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent_media_server.contacts",
                                 description="Contacts for agent-media (read-only): the "
                                             "phone's over adb, and Google's if signed in.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="sign in to Google (works from a phone)")
    p.add_argument("--port", type=int, default=0, help="loopback port (default: any free one)")
    p.set_defaults(fn=cmd_login)
    sub.add_parser("status", help="what is read, and when it last was").set_defaults(fn=cmd_status)
    p = sub.add_parser("sync", help="sync now")
    p.add_argument("--source", choices=("android", "google"), default="",
                   help="only this one (default: every source this host reads)")
    p.add_argument("--full", action="store_true", help="Google: everything, not the changes")
    p.set_defaults(fn=cmd_sync)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
