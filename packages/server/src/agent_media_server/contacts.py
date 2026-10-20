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
  `drupal`, optional and only once its connection is set
  (contacts_drupal.py): the CRM's people, read whole each interval. A CRM
  person joins the contact sharing their number (else an address, else the
  seed's link) and only fills what the others left empty; their other
  fields are the card's `crm` section (David, 17 Oct 2026: "Google wins").
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
* **One book per person** (David, 18 Oct 2026: "Where contacts live").
  Every read and write is of the current account's book (`account()`): a
  request's caller (auth.request_account — a device's enrolled account, the
  owner's for a code-paired one), else the one `as_account` names (the
  background sync, `--account`), else the owner's. The owner's book is the
  paths above, unchanged; another account's is the same files under
  `<state_dir>/accounts/<key>/` and `<config>/agent-media/accounts/<key>/`,
  so no query can forget to filter and nothing is migrated. The host's
  `MEDIA_*` settings, the phone over adb and the Matrix rooms are the
  owner's; another account's settings are its `contacts.json` there
  (`{sources, self, timeline}`), its Google sign-in and CRM connection
  its own files there. Google's OAuth *client* (the app's registration)
  is shared.
* **The bridged networks** (David, 19 Oct 2026: "draw in contacts from the
  other providers we have bridges for"). `bridges`, the owner's book only:
  the other party of each one-to-one bridged room this process holds
  (`matrix._ROOMS`), and every ghost in its group rooms (David, 19 Oct
  2026: "a full list of my bridged contacts"; someone met only in groups
  is labelled "Messenger groups" / "Instagram groups", Teams people
  "Teams"); David's own ghosts and the bots are left out. One record per
  ghost with its `handles` (`[{network, network_name, id}]`). One joins a
  contact by a kept Join (`merges`), else a shared
  number, else a shared address, else — only on a network whose names are
  the phone's own (`gmessages`, `MEDIA_CONTACTS_TRUSTED_NAMES`) — a name no
  other contact has; else it is a person of its own, and a same name is a
  suggestion on the card ("Same person?", `same` / `different`). A process
  with no rooms loaded (the CLI) keeps the last copy.
* **Exports** (`import`, per book): the friends list of a Facebook data
  download and the followers / following of an Instagram one
  (contacts_import.py; `POST /contacts/import`, `contacts import`), each
  export replacing its last import. They join as a bridge's people do,
  and an Instagram username joins the bridged Instagram ghost it names.
* **Write-back** (contacts_writeback.py): a Join puts the person's
  Facebook, Instagram and WhatsApp links on their Google entry, through
  the phone; only adds.
* **Labels**: the phone's contact groups (Google's labels, synced by
  Android) and, for a book signed in to Google, its `contactGroups`, as
  `labels` on each person; `GET /contacts?label=` and `GET
  /contacts/labels`.

Contacts are personal data: they stay on this host, and nothing here sends
them anywhere but back to a signed-in device.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import contextvars
import datetime as dt
import hashlib
import io
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
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

log = logging.getLogger("agent-media.server.contacts")

SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
#: What a sign-in asks for: the contacts, and the address it is (`email` is
#: not a sensitive scope, so it adds nothing to Google's review), so a book
#: can say whose Google it reads.
LOGIN_SCOPE = "openid email " + SCOPE
PEOPLE = "https://people.googleapis.com/v1"
PERSON_FIELDS = ("names,phoneNumbers,emailAddresses,addresses,biographies,metadata,memberships,"
                 "photos")
#: Google's own groups that are not a label: left out. "My Contacts" is one
#: (David, 19 Oct 2026), so it is not here; starred is.
_GOOGLE_HIDDEN = {"contactGroups/starred", "contactGroups/all",
                  "contactGroups/chatBuddies", "contactGroups/blocked"}
#: The names Google's own groups go by on the card.
_GOOGLE_SYSTEM = {"contactGroups/myContacts": "My Contacts", "contactGroups/friends": "Friends",
                  "contactGroups/family": "Family", "contactGroups/coworkers": "Coworkers"}
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
#: The sources, in the order they merge.
KINDS = ("android", "google", "bridges", "import", "drupal")
#: The sources whose people are only names and handles (a bridge's ghost,
#: an export's friend): they join by number, address, handle or a kept
#: Join, and a same name is only "Same person?".
WEAK = ("bridges", "import")
#: The networks whose names for people are the phone's own contact names
#: (mautrix-gmessages' `{{or .FullName .PhoneNumber}}`): a name there may
#: join a contact by itself. Elsewhere a same name is only a suggestion.
TRUSTED_NAMES = ("gmessages",)
#: The bridges whose ghosts are named by the person's number
#: (`@signal_=2b61…`, `@whatsapp_61412…`).
NUMBER_GHOSTS = ("signal", "whatsapp")
#: Instagram's network ids (mautrix-meta's and the older bridge's).
INSTAGRAM = ("instagramgo", "instagram")
#: Which uploaded address book (contacts_import.py, `upload` records) speaks
#: for a bridged network's people: Messenger's are Facebook's.
UPLOAD_NETWORKS = {"facebookgo": "facebook", "facebook": "facebook", "meta": "facebook",
                   "instagramgo": "instagram", "instagram": "instagram"}
#: Why a person joined by one (a source's `why` on the card).
UPLOAD_WHY = {"facebook": "Facebook's uploaded contacts",
              "instagram": "Instagram's uploaded contacts"}
#: A stable namespace for contact ids (uuid5 of "<kind>:<source id>").
_NS = uuid.UUID("5b0e6f8c-7d0a-4c55-9a4b-2f1d0c6e1a17")


# --- where things are -----------------------------------------------------------

# Whose book: set by `as_account` (the sync loop, the CLI); unset, the
# request's caller (auth.request_account), and outside a request the owner.
_ACCOUNT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "contacts_account", default=None)


def account() -> str:
    """The current book: "" for the owner's, else an account's key."""
    got = _ACCOUNT.get()
    if got is not None:
        return got
    from . import auth

    return auth.request_account()


def is_owner() -> bool:
    return account() == ""


@contextlib.contextmanager
def as_account(key: str):
    """Read and write `key`'s book in this block ("" the owner's)."""
    from . import auth

    key = key or ""
    if key and key != auth.NOBODY and not auth.ACCOUNT_KEY.fullmatch(key):
        raise ValueError(f"not an account key: {key!r}")
    tok = _ACCOUNT.set(key)
    try:
        yield key
    finally:
        _ACCOUNT.reset(tok)


def config_dir() -> Path:
    """`$XDG_CONFIG_HOME/agent-media`, where `secrets.env` is."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "agent-media"


def account_config_dir() -> Path:
    """Where the current book's own config is: the config dir for the
    owner, `accounts/<key>/` in it for anyone else."""
    key = account()
    return config_dir() if not key else config_dir() / "accounts" / key


def account_state_dir() -> Path:
    from agent_media_core._paths import state_dir

    key = account()
    return state_dir() if not key else state_dir() / "accounts" / key


def owner_env(name: str) -> str | None:
    """A host setting, which is the owner's: None in anyone else's book."""
    return os.environ.get(name) if is_owner() else None


def account_settings() -> dict:
    """Another account's `contacts.json` (`{sources, self, timeline}`); {}
    for the owner, whose settings are the environment."""
    if is_owner():
        return {}
    try:
        got = json.loads((account_config_dir() / "contacts.json").read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def setting(env: str, key: str) -> str | None:
    """The owner's `env`, else this account's `contacts.json` `key` (a list
    is joined with commas, as the env would be)."""
    if is_owner():
        return os.environ.get(env)
    v = account_settings().get(key)
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return None if v is None else str(v)


def accounts() -> list[str]:
    """Every book on this host: the owner's, and each account with a
    directory of its own (config or state)."""
    from agent_media_core._paths import state_dir
    from . import auth

    out = {""}
    for base in (config_dir() / "accounts", state_dir() / "accounts"):
        try:
            out |= {p.name for p in base.iterdir()
                    if p.is_dir() and auth.ACCOUNT_KEY.fullmatch(p.name)}
        except OSError:
            pass
    return sorted(out)


def client_path() -> Path:
    return config_dir() / "google-oauth-client.json"


def token_path() -> Path:
    return account_config_dir() / "google-contacts-token.json"


def db_path() -> Path:
    return account_state_dir() / "contacts.db"


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
CREATE TABLE IF NOT EXISTS drupal_links (
  uuid TEXT PRIMARY KEY, contact TEXT NOT NULL, at REAL);
CREATE TABLE IF NOT EXISTS merges (
  source TEXT NOT NULL, contact TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('join', 'dismiss')), at REAL,
  PRIMARY KEY (source, contact));
CREATE TABLE IF NOT EXISTS aliases (
  contact TEXT NOT NULL, address TEXT NOT NULL, real TEXT,
  first_seen REAL, last_seen REAL,
  PRIMARY KEY (contact, address));
CREATE TABLE IF NOT EXISTS found (
  sid TEXT PRIMARY KEY, contact TEXT NOT NULL, network TEXT NOT NULL,
  card TEXT NOT NULL, meta TEXT NOT NULL, at REAL);
CREATE TABLE IF NOT EXISTS person_level (
  contact TEXT PRIMARY KEY, level TEXT NOT NULL, at REAL);
CREATE TABLE IF NOT EXISTS photo_src (
  kind TEXT NOT NULL, sid TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
  file TEXT NOT NULL DEFAULT '', ctype TEXT NOT NULL DEFAULT '',
  at REAL, tries INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (kind, sid));
"""
#: What a schema bump keeps: David's own decisions, not what a source said,
#: the old chat addresses (`aliases`), which outlive their sources, and the
#: pictures fetched (`photo_src`, contacts_photos.py), costly to fetch again,
#: and who David picked from a search on a network (`found`, contacts_find.py),
#: and each person's speech level (`person_level`, contacts_level.py; `notify`
#: is its forerunner, kept until moved into it).
_KEPT = {"overrides", "drupal_links", "merges", "aliases", "photo_src", "found",
         "person_level", "notify"}
#: Bump when the cards are made differently from the same records: the
#: next sync rebuilds them (records kept, nothing read again).
CARDS_VERSION = "gateways-1"
#: Bump when what a row holds changes: the store is emptied (but for the
#: overrides and the seed's links) and read again.
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
        for t in have - _KEPT:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(_SCHEMA)
    if "notify" in have:      # the card's first setting, moved into person_level once
        from . import contacts_level

        contacts_level.migrate_notify(conn)
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
    if kind == "drupal":
        return {"kind": "drupal", "id": sid, "changed": meta.get("changed") or ""}
    if kind == "bridges":
        return {"kind": "bridges", "id": sid, "network": meta.get("network") or "",
                "network_name": meta.get("network_name") or ""}
    if kind == "import":
        return {"kind": "import", "id": sid, "export": meta.get("export") or "",
                "lists": meta.get("lists") or []}
    if kind == "found":
        return {"kind": "found", "id": sid, "network": meta.get("network") or "",
                "network_name": meta.get("network_name") or "",
                "why": meta.get("why") or ""}
    return {"kind": kind, "id": sid, "etag": meta.get("etag") or ""}


def _keys(card: dict) -> tuple[set, set]:
    """The numbers and addresses a card is matched by; an old chat gateway's
    address counts as the real one it encodes, and not at all when it
    encodes none."""
    gw = gateways()
    emails = set()
    for e in card.get("emails") or []:
        a = (e.get("value") or "").strip()
        if not a:
            continue
        if is_gateway(a, gw):
            real = gateway_real(a)
            if real:
                emails.add(real.lower())
        else:
            emails.add(a.lower())
    return ({p["value"] for p in card.get("phones") or [] if p.get("value")}, emails)


# --- old chat gateway addresses --------------------------------------------------
#
# Jabber (XMPP) transports to MSN, Yahoo, AIM, ICQ, Facebook chat and the
# like left addresses such as `someone%hotmail.com@msn.example.org` in
# Google Contacts: the real address is the local part, `%` for `@`. They are
# folded off the card's `emails` into `old_addresses`, the real address added
# (marked derived) when the card lacks it, and every one ever seen kept in
# `aliases` so the mail search still finds the old chats after they are
# deleted from Google (David, 19 Oct 2026).

#: The transports' hosts (fnmatch patterns, matched against the domain).
GATEWAYS = (
    "msn*.jabber.*", "yahoo*.jabber.*", "aim*.jabber.*", "icq*.jabber.*",
    "*.gajim.org", "*.jaim.at", "*.jabbim.cz", "j2j.*", "*.j2j.*",
    "gtalk2voip.com", "*.gtalk2voip.com", "chikkatalk.com", "*.chikkatalk.com",
    "*.talk.google.com",
)
#: A host's first label naming a transport: such a host is one when the
#: address's local part encodes an address (`user%hotmail.com`), so a real
#: `someone@yahoo.com.au` never is.
TRANSPORTS = {"msn", "yahoo", "aim", "icq", "fb", "facebook", "gg", "gadugadu",
              "irc", "skype", "qq", "myspace", "twitter", "sms", "gtalk"}


def gateways() -> tuple[str, ...]:
    """`MEDIA_CONTACTS_GATEWAYS` (the owner's) or `contacts.json`
    `gateways`: comma-separated patterns that replace `GATEWAYS`, or, each
    starting with `+`, are added to them."""
    raw = (setting("MEDIA_CONTACTS_GATEWAYS", "gateways") or "").strip()
    if not raw:
        return GATEWAYS
    got = [x.strip().lower() for x in raw.split(",") if x.strip()]
    add = [x[1:] for x in got if x.startswith("+") and x[1:]]
    own = [x for x in got if not x.startswith("+")]
    return tuple(own or GATEWAYS) + tuple(add)


def gateway_real(address: str) -> str | None:
    """The real address a gateway address encodes (`user%hotmail.com@…` →
    `user@hotmail.com`), else None."""
    local = address.rpartition("@")[0].strip()
    for sep in ("%40", "%", "\\40"):
        if sep in local:
            user, _, dom = local.rpartition(sep)
            if user and "." in dom and "@" not in user + dom and " " not in user + dom:
                return f"{user}@{dom}".lower()
    return None


def is_gateway(address: str, patterns: tuple[str, ...] | None = None) -> bool:
    """An old chat transport's address: its host matches a pattern, or its
    host starts with a transport's name and it encodes an address."""
    import fnmatch

    user, at, host = address.strip().lower().rpartition("@")
    if not at or not user or not host:
        return False
    if any(fnmatch.fnmatchcase(host, p) for p in (patterns or gateways())):
        return True
    return host.split(".")[0] in TRANSPORTS and host.count(".") >= 2 and bool(
        gateway_real(address))


def _fold_gateways(conn: sqlite3.Connection, cid: str, card: dict, now: float) -> dict:
    """The card with its gateway addresses folded off `emails` into
    `old_addresses` (with those `aliases` holds for it from before, last
    seen first), the real addresses they encode added as derived emails."""
    gw = gateways()
    real, old = [], []
    for e in card.get("emails") or []:
        a = (e.get("value") or "").strip()
        (old if a and is_gateway(a, gw) else real).append(e)
    have = {(e.get("value") or "").strip().lower() for e in real}
    seen = []
    for e in old:
        a = e["value"].strip()
        if a.lower() in {x.lower() for x in seen}:
            continue
        seen.append(a)
        r = gateway_real(a)
        conn.execute("INSERT INTO aliases (contact, address, real, first_seen, last_seen) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT(contact, address) DO UPDATE "
                     "SET last_seen=excluded.last_seen, real=excluded.real",
                     (cid, a.lower(), r, now, now))
        if r and r not in have:
            have.add(r)
            real.append({"value": r, "type": "derived", "derived": True, "from": a})
    known = [{"address": a, "real": r} for a, r in conn.execute(
        "SELECT address, real FROM aliases WHERE contact=? ORDER BY last_seen DESC, "
        "first_seen DESC, address", (cid,)) if a not in {x.lower() for x in seen}]
    if not seen and not known and not old:
        return card
    out = {**card, "emails": real}
    out["old_addresses"] = [{"address": a, "real": gateway_real(a)} for a in seen] + known
    return out


#: The card's list fields. Google's value wins where it has one (the
#: proposal's "Google wins"); the phone's fills the rest.
_FIELDS = ("names", "phones", "emails", "addresses", "organizations")


def _merge(google: dict, phone: dict) -> dict:
    out = {f: list(google.get(f) or []) or list(phone.get(f) or []) for f in _FIELDS}
    out["notes"] = google.get("notes") or phone.get("notes") or ""
    gname = google.get("name") or ""
    out["name"] = gname if gname and gname != "(no name)" else (phone.get("name") or gname)
    # Labels and a bridge's handles are every source's, not one's.
    labels = {fold(x): x for x in (phone.get("labels") or []) + (google.get("labels") or [])}
    if labels:
        out["labels"] = sorted(labels.values(), key=fold)
    handles = {h["id"]: h for h in (google.get("handles") or []) + (phone.get("handles") or [])
               if h.get("id")}
    if handles:
        out["handles"] = list(handles.values())
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
        if kind != "google":
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
    _rebuild_bridges(conn, recs, book)
    _rebuild_drupal(conn, recs, book)
    old = {cid: (raw, upd) for cid, raw, upd in conn.execute(
        "SELECT id, card, updated FROM contacts")}
    for t in ("contacts", "phones", "emails"):
        conn.execute(f"DELETE FROM {t}")
    now = time.time()
    for cid, entry in book.items():
        c = _fold_gateways(conn, cid, {**entry["card"], "sources": entry["sources"]}, now)
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
    _set_meta(conn, "cards", CARDS_VERSION)
    _bump(conn)


def refold() -> bool:
    """Rebuild the cards from the kept records when they were made by an
    older `CARDS_VERSION` (no source is read again); True when it did."""
    with _WRITE:
        conn = _connect(create=False)
        if conn is None:
            return False
        try:
            if _meta(conn, "cards") == CARDS_VERSION or not conn.execute(
                    "SELECT 1 FROM records LIMIT 1").fetchone():
                return False
            conn.execute("BEGIN IMMEDIATE")
            _rebuild(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    _fresh()
    return True


def trusted_names() -> set[str]:
    """`MEDIA_CONTACTS_TRUSTED_NAMES` (comma separated network ids), else
    `TRUSTED_NAMES`: the networks where a name alone may join a contact."""
    raw = (os.environ.get("MEDIA_CONTACTS_TRUSTED_NAMES") or "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()} if raw else set(TRUSTED_NAMES)


def _names_of(card: dict) -> set[str]:
    return {n for n in {fold(card.get("name") or "")}
            | {fold(x.get("display") or "") for x in card.get("names") or []} if n}


def _rebuild_bridges(conn: sqlite3.Connection, recs: list, book: dict) -> None:
    """The bridged networks' people (`bridges`), after Google and before the
    CRM. Each joins, in order: the contact a kept Join names; else the one
    contact sharing its number; else its address; else, on a trusted network
    only, the one contact with its name (and no kept "Not the same"); else
    it is its own person. A join only fills what the contact left empty."""
    merges: dict[str, list[tuple[str, str]]] = {}
    for source, cid, kind in conn.execute("SELECT source, contact, kind FROM merges"):
        merges.setdefault(source, []).append((cid, kind))
    trusted = trusted_names()
    pending = []
    for want in WEAK:                  # the bridges first: an export may join them
        for kind, sid, card, meta in recs:
            if kind == want:
                pending.append((kind, sid, json.loads(card), json.loads(meta)))

    by_phone: dict[str, set] = {}
    by_email: dict[str, set] = {}
    by_name: dict[str, set] = {}
    by_username: dict[str, set] = {}   # an Instagram person's username, or name

    def index(cid: str) -> None:
        card = book[cid]["card"]
        phones, emails = _keys(card)
        for p in phones:
            by_phone.setdefault(p, set()).add(cid)
        for e in emails:
            by_email.setdefault(e, set()).add(cid)
        for n in _names_of(card):
            by_name.setdefault(n, set()).add(cid)
        # A bridged Instagram ghost is named `{{or .DisplayName .Username}}`:
        # an export's username may be its handle's username, or its name.
        for h in card.get("handles") or []:
            if h.get("network") in INSTAGRAM and not h.get("id", "").startswith("instagram:"):
                for u in {fold(h.get("username") or "")} | _names_of(card):
                    if u:
                        by_username.setdefault(u, set()).add(cid)

    for cid in book:
        index(cid)

    # The address books David once let Facebook and Instagram sync (`upload`
    # records): an entry named as a network's person, holding a number or
    # an address of exactly one contact here, says they are that contact.
    uploads: dict[str, dict[str, list]] = {}
    for kind, sid, card, meta in recs:
        if kind == "upload":
            c = json.loads(card)
            net = json.loads(meta).get("export") or ""
            for n in _names_of(c):
                uploads.setdefault(net, {}).setdefault(n, []).append(_keys(c))

    def uploaded(kind: str, c: dict, meta: dict) -> set:
        net = (meta.get("export") if kind == "import" else
               UPLOAD_NETWORKS.get(meta.get("network") or ""))
        got: set = set()
        for n in _names_of(c):
            entries = (uploads.get(net) or {}).get(n) or []
            if len(entries) != 1:      # two uploaded people of that name: neither
                continue
            phones, emails = entries[0]
            for p in phones:
                got |= by_phone.get(p, set())
            for e in emails:
                got |= by_email.get(e, set())
        return got - bridged

    def place(kind: str, sid: str, c: dict, meta: dict, joined_to: str | None,
              why: str = "") -> None:
        src = _source(kind, sid, meta)
        if why:
            src["why"] = why
        if joined_to:
            entry = book[joined_to]
            book[joined_to] = {"card": _merge(entry["card"], c),
                               "sources": entry["sources"] + [src]}
            index(joined_to)
        else:
            cid = contact_id(kind, sid)
            book[cid] = {"card": c, "sources": [src]}
            bridged.add(cid)
            index(cid)

    # The people a bridge alone knows: a trusted name is the phone's own
    # contact name, so it never joins one of them.
    bridged: set[str] = set()

    # A kept Join may name another bridged person (Messenger and Instagram,
    # neither in the phone): those wait until the one they join is placed.
    later = []
    for kind, sid, c, meta in pending:
        joins = [cid for cid, k in merges.get(sid, []) if k == "join"]
        if joins:
            later.append((kind, sid, c, meta, joins[-1]))
            continue
        dismissed = {cid for cid, k in merges.get(sid, []) if k == "dismiss"}
        phones, emails = _keys(c)
        hits: set = set()
        for p in phones:
            hits |= by_phone.get(p, set())
        if not hits:
            for e in emails:
                hits |= by_email.get(e, set())
        if not hits and kind == "import":
            for h in c.get("handles") or []:
                if h.get("network") in INSTAGRAM and h.get("username"):
                    hits |= by_username.get(fold(h["username"]), set())
        why = ""
        if not hits and uploads:
            hits = uploaded(kind, c, meta) - dismissed
            why = UPLOAD_WHY.get(meta.get("export") if kind == "import" else
                                 UPLOAD_NETWORKS.get(meta.get("network") or ""), "") if hits else ""
        if not hits and kind == "bridges" and meta.get("network") in trusted:
            for n in _names_of(c):
                hits |= by_name.get(n, set()) - bridged
        hits -= dismissed
        one = next(iter(hits)) if len(hits) == 1 else None
        place(kind, sid, c, meta, one, why if one else "")
    for _round in range(len(later) + 1):
        if not later:
            break
        waiting = []
        for kind, sid, c, meta, cid in later:
            if cid in book:
                place(kind, sid, c, meta, cid)
            else:
                waiting.append((kind, sid, c, meta, cid))
        if len(waiting) == len(later):
            break                      # what they joined is gone: their own again
        later = waiting
    for kind, sid, c, meta, _cid in later:
        place(kind, sid, c, meta, None)
    # Who David picked from a search on a network (contacts_find.py): onto
    # the contact he searched from. A ghost a bridge also knows from a room
    # is placed there already (the pick is a kept Join too): not twice.
    placed = {s.get("id") for e in book.values() for s in e["sources"]
              if s.get("kind") in WEAK}
    for sid, cid, card, meta in conn.execute(
            "SELECT sid, contact, card, meta FROM found ORDER BY at"):
        if sid in placed:
            continue
        place("found", sid, json.loads(card), json.loads(meta), cid if cid in book else None)


def _rebuild_drupal(conn: sqlite3.Connection, recs: list, book: dict) -> None:
    """The CRM's people into the book, last: one joins the contact the
    seed linked it to, else the one sharing its number, else its address;
    what the others have wins, and its own fields are the `crm` section."""
    links = dict(conn.execute("SELECT uuid, contact FROM drupal_links"))
    by_phone: dict[str, set] = {}
    by_email: dict[str, set] = {}
    for cid, entry in book.items():
        phones, emails = _keys(entry["card"])
        for p in phones:
            by_phone.setdefault(p, set()).add(cid)
        for e in emails:
            by_email.setdefault(e, set()).add(cid)
    for kind, sid, card, meta in recs:
        if kind != "drupal":
            continue
        c = json.loads(card)
        src = _source(kind, sid, json.loads(meta))
        hits = {links[sid]} & set(book) if sid in links else set()
        if not hits:
            phones, emails = _keys(c)
            for p in phones:
                hits |= by_phone.get(p, set())
            if not hits:
                for e in emails:
                    hits |= by_email.get(e, set())
        hits = {h for h in hits if not any(s["kind"] == "drupal" for s in book[h]["sources"])}
        if len(hits) == 1:
            cid = next(iter(hits))
            merged = _merge(book[cid]["card"], c)
            merged["crm"] = c.get("crm") or {}
            book[cid] = {"card": merged, "sources": book[cid]["sources"] + [src]}
        else:
            book[contact_id(kind, sid)] = {"card": c, "sources": [src]}


def link_drupal(uuid: str, contact: str) -> None:
    """The seed made `uuid` in the CRM for `contact`: kept, so the two are
    one person whatever their numbers say later."""
    with _WRITE:
        conn = _connect()
        try:
            conn.execute("INSERT OR REPLACE INTO drupal_links (uuid, contact, at) VALUES (?, ?, ?)",
                         (uuid, contact, time.time()))
        finally:
            conn.close()


def drupal_uuid(cid: str) -> str | None:
    """The CRM person that is this contact, if any."""
    conn = _connect(create=False)
    if conn is None:
        return None
    try:
        got = conn.execute("SELECT card FROM contacts WHERE id=?", (cid,)).fetchone()
        if got:
            for s in json.loads(got[0]).get("sources") or []:
                if s.get("kind") == "drupal":
                    return s.get("id")
        row = conn.execute("SELECT uuid FROM drupal_links WHERE contact=?", (cid,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


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


def labels_of(person: dict, groups: dict[str, str] | None) -> list[str]:
    """A person's labels: their `memberships`' contact groups by name
    (`groups`, resource name to name), Google's own groups by theirs, the
    hidden ones (starred, all…) left out."""
    out = {}
    for m in person.get("memberships") or []:
        rn = ((m.get("contactGroupMembership") or {}).get("contactGroupResourceName") or "")
        if not rn or rn in _GOOGLE_HIDDEN:
            continue
        name = _GOOGLE_SYSTEM.get(rn) or (groups or {}).get(rn) or ""
        if name:
            out[fold(name)] = name
    return sorted(out.values(), key=fold)


def card_of(person: dict, default_region: str = "AU",
            groups: dict[str, str] | None = None) -> dict:
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
    out = {"name": name, "names": names, "phones": phones, "emails": emails,
           "addresses": addresses, "organizations": [], "notes": notes}
    got = labels_of(person, groups)
    if got:
        out["labels"] = got
    return out


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


class SignInExpired(NotSignedIn):
    """Google refused the refresh token (`invalid_grant`): revoked, or — while
    the Google project is in testing mode — the 7 days a test user's refresh
    token lasts are up. The book keeps its last copy and says "sign in again"."""


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
    """A client and a refresh token Google still takes (an expired one is
    not tried again until the next sign-in)."""
    tok = load_token()
    return load_client() is not None and tok is not None and not tok.get("expired_at")


def _email_of(body: dict) -> str:
    """The address in a token answer's `id_token` (straight from Google's
    token endpoint over TLS, so read, not verified), else ""."""
    raw = str(body.get("id_token") or "")
    try:
        part = raw.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return ""
    return str(claims.get("email") or "") if isinstance(claims, dict) else ""


def pkce() -> tuple[str, str]:
    """`(verifier, S256 challenge)`, RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def consent_url(client: dict, redirect_uri: str, state: str, challenge: str) -> str:
    return client["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect_uri,
        "response_type": "code", "scope": LOGIN_SCOPE, "state": state,
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


_ACCESS: dict = {}                    # {"token", "expires"}: the owner's, this process only
_ACCESSES: dict = {"": _ACCESS}       # every book's, by account


def _access() -> dict:
    return _ACCESSES.setdefault(account(), {})


def _store_access(body: dict) -> None:
    acc = _access()
    acc.clear()
    acc.update(token=body["access_token"],
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
    if _email_of(body):
        kept["email"] = _email_of(body)
    _save_token(kept)
    _store_access(body)
    return kept


def access_token() -> str:
    """A current access token, refreshed when it is near its end."""
    acc = _access()
    if acc.get("token") and acc.get("expires", 0) - 60 > time.time():
        return acc["token"]
    client, tok = load_client(), load_token()
    if client is None:
        raise NotSignedIn(f"no OAuth client ({client_path()})")
    if tok is None:
        raise NotSignedIn("not signed in (python -m agent_media_server.contacts login)")
    if tok.get("expired_at"):
        raise SignInExpired("Google's sign-in has expired; sign in again")
    status, body = _request("POST", client["token_uri"], form={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": client["client_id"],
        **({"client_secret": client["client_secret"]} if client["client_secret"] else {})})
    if status != 200 or not body.get("access_token"):
        err = body.get("error") or status
        if err == "invalid_grant":
            # Kept as expired, not deleted: the book says "sign in again"
            # and the sync stops trying until it is.
            _save_token({**tok, "expired_at": round(time.time(), 3)})
            raise SignInExpired("Google no longer takes the sign-in (revoked, or the "
                                "7 days a testing-mode sign-in lasts are up); sign in again")
        raise SyncError(f"token refresh failed ({err})")
    if body.get("refresh_token") and body["refresh_token"] != tok["refresh_token"]:
        _save_token({**tok, "refresh_token": body["refresh_token"]})
    _store_access(body)
    return _access()["token"]


# --- the sync -------------------------------------------------------------------

class _Expired(Exception):
    pass


def _groups() -> dict[str, str]:
    """The book's Google contact groups, resource name to name (one page:
    the API's most is 1000). A failure is no names, not no sync."""
    try:
        status, body = _request(
            "GET", f"{PEOPLE}/contactGroups?pageSize=1000&groupFields=name",
            headers={"Authorization": f"Bearer {access_token()}"})
    except (OSError, urllib.error.URLError, NotSignedIn) as e:
        log.info("contacts: google labels unread: %s", e)
        return {}
    if status != 200:
        log.info("contacts: google labels unread: %s", status)
        return {}
    return {g.get("resourceName") or "": g.get("formattedName") or g.get("name") or ""
            for g in body.get("contactGroups") or [] if g.get("resourceName")}


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
            _access().clear()                  # a token Google ended early
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
            # A sync token is only good for the fields it was made with: new
            # fields (the labels' memberships) read everything again.
            if _meta(conn, "google_fields") != PERSON_FIELDS:
                token = None
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
        groups = _groups() if any(p.get("memberships") for p in people) else {}
        reg = region()
        from . import contacts_photos
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
                                 (rn, json.dumps(card_of(p, reg, groups)),
                                  json.dumps({"etag": p.get("etag") or "",
                                              "photo": contacts_photos.google_url(p)})))
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
                _set_meta(conn, "google_fields", PERSON_FIELDS)
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


def sync_drupal() -> dict:
    """Read every person in the CRM (contacts_drupal.py). Unreachable or
    refused raises, and the last good copy stays."""
    from . import contacts_drupal

    with _SYNC_LOCK:
        try:
            recs = contacts_drupal.read(region())
        except (contacts_drupal.NotConfigured, contacts_drupal.CrmError, OSError,
                urllib.error.URLError, KeyError, ValueError) as e:
            _failed("drupal", e)
            raise
        digest = hashlib.sha256(json.dumps(recs, sort_keys=True).encode()).hexdigest()
        with _WRITE:
            conn = _connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                same = _meta(conn, "drupal_hash") == digest
                if not same:
                    conn.execute("DELETE FROM records WHERE kind='drupal'")
                    conn.executemany(
                        "INSERT INTO records (kind, sid, card, meta) VALUES ('drupal', ?, ?, ?)",
                        [(sid, json.dumps(r["card"]), json.dumps(r["meta"]))
                         for sid, r in recs.items()])
                    _rebuild(conn)
                    _set_meta(conn, "drupal_hash", digest)
                _set_meta(conn, "drupal_synced_at", str(round(time.time(), 3)))
                _set_meta(conn, "drupal_last_error", None)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
                conn.close()
        return {"source": "drupal", "unchanged": same, "read": len(recs), "contacts": total}


#: Rooms of one network a ghost must be in all of before it is taken for
#: David's own (Instagram puppets him into each of his chats). Fewer, and two
#: chats with one friend would make the friend him.
OWN_GHOST_ROOMS = 3


def _bridged_rooms() -> list:
    """The bridged rooms this process holds, loaded, with their network."""
    from . import matrix

    out = []
    for r in list(matrix._ROOMS.values()):
        if r.loaded and r.network and (r.network.get("id") or ""):
            out.append(r)
    return out


def own_ghosts(rooms: list | None = None) -> set[str]:
    """David's own puppets on the bridged networks: a ghost in every room
    of its network (at least `OWN_GHOST_ROOMS` of them), one named as the
    owner is in that room, or his own number's (`MEDIA_CONTACTS_SELF`)."""
    rooms = _bridged_rooms() if rooms is None else rooms
    self_phones, _ = _self_ids()
    reg = region()
    per_net: dict[str, list[set]] = {}
    out: set[str] = set()
    for r in rooms:
        with r.lock:
            members = dict(r.members)
        skip = _skip_of(r)
        mine = fold(members.get(r.owner) or "")
        others = {u for u in members if u not in skip}
        per_net.setdefault(r.network["id"], []).append(others)
        for u in others:
            dn = members.get(u) or ""
            if mine and fold(dn) == mine:
                out.add(u)
            elif self_phones and identities(u, dn, reg)["phones"] & self_phones:
                out.add(u)
    for sets in per_net.values():
        if len(sets) >= OWN_GHOST_ROOMS:
            out |= set.intersection(*sets)
    return out


#: The label a person met only in a bridged group chat carries, by network
#: id, so People can show them apart (David, 19 Oct 2026: a full list).
GROUP_LABELS = {"facebookgo": "Messenger groups", "facebook": "Messenger groups",
                "meta": "Messenger groups", "instagramgo": "Instagram groups",
                "instagram": "Instagram groups"}
#: Networks whose people all carry one label, whether met one to one or in
#: a group: Teams is work.
NETWORK_LABELS = {"msteams": "Teams", "teams": "Teams"}


def _is_ghost(uid: str) -> bool:
    """A bridge's puppet (`@facebook_1000…`, `@msteams_8=3a…`): a group's
    member that is not one is a Matrix account of its own, not a contact."""
    return "_" in (uid or "").lstrip("@").partition(":")[0]


def bridge_records(rooms: list | None = None) -> dict[str, dict]:
    """One record per person David chats with on a bridged network: the one
    other party of a one-to-one room, and — since 19 Oct 2026 ("a full list
    of my bridged contacts") — every ghost in a group room too, once his own
    ghosts and the bots are left out. Someone met only in groups carries
    their network's `GROUP_LABELS` label; Teams people carry "Teams"."""
    rooms = _bridged_rooms() if rooms is None else rooms
    mine = own_ghosts(rooms)
    reg = region()
    out: dict[str, dict] = {}
    direct: set[str] = set()
    for r in rooms:
        with r.lock:
            members = dict(r.members)
        skip = _skip_of(r) | mine
        others = [u for u in members if u not in skip]
        if len(others) == 1:
            direct.add(others[0])
        else:
            others = [u for u in others if _is_ghost(u)]
        net = r.network
        for uid in others:
            dn = (members.get(uid) or "").strip()
            rec = out.get(uid)
            if rec is None:
                who = identities(uid, dn, reg)
                name = dn if dn and dn != uid else uid.lstrip("@").partition(":")[0]
                rec = out[uid] = {
                    "card": {"name": name, "names": [{"display": name, "given": "", "family": ""}],
                             "phones": [{"value": p, "raw": p, "type": net.get("name") or net["id"]}
                                        for p in sorted(who["phones"])],
                             "emails": [{"value": e, "type": net.get("name") or net["id"]}
                                        for e in sorted(who["emails"])],
                             "addresses": [], "organizations": [], "notes": "",
                             "handles": [{"network": net["id"],
                                          "network_name": net.get("name") or "", "id": uid}]},
                    "meta": {"network": net["id"], "network_name": net.get("name") or "",
                             "rooms": []}}
            rec["meta"]["rooms"] = sorted(set(rec["meta"]["rooms"]) | {r.id})
    for uid, rec in out.items():
        net = rec["meta"]["network"]
        label = NETWORK_LABELS.get(net) or (GROUP_LABELS.get(net) if uid not in direct else None)
        if label:
            rec["card"]["labels"] = [label]
        if uid not in direct:
            rec["meta"]["groups_only"] = True
    return out


def sync_bridges() -> dict:
    """The bridged networks' people (`bridge_records`) into the owner's
    book. A process holding no bridged rooms (the CLI, or the canvas before
    its rooms load) keeps the last copy and writes nothing."""
    with _SYNC_LOCK:
        rooms = _bridged_rooms() if is_owner() else []
        if not rooms:
            conn = _connect()
            try:
                n = conn.execute("SELECT COUNT(*) FROM records WHERE kind='bridges'").fetchone()[0]
                total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            finally:
                conn.close()
            return {"source": "bridges", "unchanged": True, "rooms": 0, "read": n,
                    "contacts": total}
        recs = bridge_records(rooms)
        digest = hashlib.sha256(json.dumps(recs, sort_keys=True).encode()).hexdigest()
        with _WRITE:
            conn = _connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                same = _meta(conn, "bridges_hash") == digest
                if not same:
                    conn.execute("DELETE FROM records WHERE kind='bridges'")
                    conn.executemany(
                        "INSERT INTO records (kind, sid, card, meta) VALUES ('bridges', ?, ?, ?)",
                        [(sid, json.dumps(r["card"]), json.dumps(r["meta"]))
                         for sid, r in recs.items()])
                    _rebuild(conn)
                    _set_meta(conn, "bridges_hash", digest)
                _set_meta(conn, "bridges_synced_at", str(round(time.time(), 3)))
                _set_meta(conn, "bridges_last_error", None)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
                conn.close()
        return {"source": "bridges", "unchanged": same, "rooms": len(rooms), "read": len(recs),
                "contacts": total}


#: The largest export `POST /contacts/import` takes (the friends and
#: followers sections alone are well under this; a whole export is not).
IMPORT_MAX = 64 * 1024 * 1024


def import_export(data: bytes, name: str = "", *, via: str = "") -> dict:
    """A data export as it was sent (`POST /contacts/import`, the phone's
    Downloads): a .zip, an .mbox or one JSON file. Its people lists
    (contacts_import.py) and its past conversations (contacts_history.py),
    whichever it holds. contacts_import.NotAnExport when it holds neither."""
    from . import contacts_history, contacts_import

    if data[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise contacts_import.NotAnExport("not a zip that opens") from e
        return _import_zip(zf, name, via=via)
    hist = contacts_history.scan_bytes(data, name)
    profiles = contacts_import.profiles_bytes(data, name)
    try:
        found, counts = contacts_import.parse(data, name)
    except contacts_import.NotAnExport:
        if not hist and not profiles:
            raise
        found, counts = {}, {}
    return _import_found(found, counts, hist, via=via, profiles=profiles)


def import_path(path: Path, *, via: str = "") -> dict:
    """A data export on this host (`contacts import`, the import folder),
    read as it goes: a zip's list and message files and an .mbox's headers,
    never the whole file, so an export of any size with its media is
    fine. contacts_import.NotAnExport when it holds nothing this reads."""
    from . import contacts_history, contacts_import

    path = Path(path)
    with path.open("rb") as f:
        head = f.read(5)
    if head[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(path) as zf:
                return _import_zip(zf, path.name, via=via)
        except zipfile.BadZipFile as e:
            raise contacts_import.NotAnExport("not a zip that opens") from e
    if head == b"From " or path.suffix.lower() == ".mbox":
        hist = contacts_history.History()
        with path.open("rb") as f:
            contacts_history.scan_mbox(f, hist)
        if not hist:
            raise contacts_import.NotAnExport("no chats in that mbox (Takeout: Mail, the "
                                              "Chats label)")
        return _import_found({}, {}, hist, via=via)
    if path.stat().st_size > IMPORT_MAX:
        raise contacts_import.NotAnExport("too large for a JSON file: import the export's zip")
    return import_export(path.read_bytes(), path.name, via=via)


def _import_zip(zf: "zipfile.ZipFile", name: str, *, via: str = "") -> dict:
    from . import contacts_history, contacts_import

    found, counts, html = contacts_import.parse_zip(zf)
    hist = contacts_history.scan_zip(zf, name)
    profiles = contacts_import.profiles_zip(zf)
    if not counts and not hist and not profiles:
        raise contacts_import.NotAnExport(
            "that export is HTML: download it again as JSON" if html else
            "no friends, followers, messages, chats or profile in that zip")
    return _import_found(found, counts, hist, via=via, profiles=profiles)


def _import_found(found: dict, counts: dict, hist, *, via: str = "",
                  profiles: dict | None = None) -> dict:
    """The people lists found, then the history, into this book; the
    owner's own profile (contacts_import.save_owner_profiles) when the
    export holds it."""
    out = {"imported": dict(counts), "people": 0, "joined": 0, "contacts": 0}
    if profiles:
        from . import contacts_import

        try:
            said = contacts_import.save_owner_profiles(profiles)
        except OSError as e:
            log.warning("contacts: the owner's profile was not kept (%s)", type(e).__name__)
            said = {}
        if said:
            out["profile"] = said
            log.info("contacts: the owner's profile from %s: %s", ", ".join(sorted(said)),
                     "; ".join(f"{n} {', '.join(f) or 'nothing'}" for n, f in sorted(said.items())))
    if found:
        out.update(_import_people(found, counts))
    if hist:
        from . import contacts_history

        h = contacts_history.apply(hist)
        out["history"] = h
        for k in ("messenger_conversations", "instagram_conversations", "old_chats"):
            if h.get(k):
                out["imported"][k] = h[k]
        conn = _connect(create=False)
        if conn is not None:
            try:
                out["contacts"] = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            finally:
                conn.close()
    log.info("contacts[%s]: imported %s", account() or "owner",
             ", ".join(f"{k} {v}" for k, v in sorted(out["imported"].items())))
    # The reminder clock and the export alerts (contacts_exports.py); `via`
    # names where an automatic import found the file, which is then said.
    from . import contacts_exports

    try:
        contacts_exports.note_import(found.keys(), out["imported"], via=via)
    except Exception as e:  # noqa: BLE001 — the import itself is done
        log.warning("contacts: export bookkeeping failed (%s)", e)
    return out


def _import_people(found: dict, counts: dict) -> dict:
    """The friends and followers lists as the `import` source: each
    export's people replace that export's last import (a Join or a "Not the
    same" holds, being kept by the person's stable id). The counts, never a
    name."""
    with _SYNC_LOCK, _WRITE:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for export, recs in found.items():
                # An uploaded address book (`facebook_uploads`) makes no one:
                # its entries are `upload` records, evidence for _rebuild_bridges.
                kind, prefix = (("upload", export[:-len("_uploads")] + "-upload:")
                                if export.endswith("_uploads") else ("import", export + ":"))
                conn.execute("DELETE FROM records WHERE kind=? AND sid LIKE ?",
                             (kind, prefix + "%"))
                conn.executemany(
                    "INSERT INTO records (kind, sid, card, meta) VALUES (?, ?, ?, ?)",
                    [(kind, sid, json.dumps(r["card"]), json.dumps(r["meta"]))
                     for sid, r in recs.items()])
            _rebuild(conn)
            _set_meta(conn, "import_synced_at", str(round(time.time(), 3)))
            _set_meta(conn, "import_last_error", None)
            people = joined = 0
            for (raw,) in conn.execute("SELECT card FROM contacts"):
                srcs = json.loads(raw).get("sources") or []
                if any(s.get("kind") == "import" and ":" in (s.get("id") or "")
                       and "-history:" not in s.get("id", "") for s in srcs):
                    people += 1
                    joined += any(s.get("kind") != "import" for s in srcs)
            total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    _fresh()
    return {"people": people, "joined": joined, "contacts": total}


def sources() -> list[str]:
    """The sources this book reads: `MEDIA_CONTACTS_SOURCES` (comma
    separated; another account's `contacts.json` `sources`), else the phone
    when this host has the adb tool (the owner's book only), Google only
    when signed in, the bridged networks when this host reads bridges as
    the owner (`MATRIX_OWNER_BRIDGES`; the owner's book only), and the CRM
    only when its connection is set."""
    from . import contacts_android, contacts_drupal, matrix

    raw = (setting("MEDIA_CONTACTS_SOURCES", "sources") or "").strip()
    if raw:
        want = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        want = ["android", "google", "bridges", "drupal"]
    out = []
    for s in want:
        # The phone over adb is this host's, so the owner's: never another
        # account's book.
        if s == "android" and is_owner() and contacts_android.available():
            out.append(s)
        elif s == "google" and signed_in():
            out.append(s)
        elif s == "bridges" and is_owner() and matrix._bridges():
            out.append(s)
        elif s == "drupal" and contacts_drupal.configured():
            out.append(s)
    return out


def sync(full: bool = False) -> dict:
    """Every source this host reads, each on its own: one that fails is said
    (`errors`) and the others still sync."""
    done, errors = [], {}
    for s in sources():
        try:
            done.append(_SYNCS[s](full))
        except Exception as e:  # noqa: BLE001 — one source down is not all of them
            errors[s] = str(e) or type(e).__name__
    return {"synced": done, "errors": errors}


_SYNCS = {"android": lambda full: sync_android(), "google": lambda full: sync_google(full=full),
          "bridges": lambda full: sync_bridges(), "drupal": lambda full: sync_drupal()}


# --- reading the store ----------------------------------------------------------

def _row(cid: str, name: str, card: dict) -> dict:
    return {"id": cid, "name": name,
            "phones": [p["value"] for p in card.get("phones") or [] if p.get("value")],
            "emails": [e["value"] for e in card.get("emails") or [] if e.get("value")],
            "labels": card.get("labels") or []}


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
    for kind in KINDS:
        synced = _meta(conn, f"{kind}_synced_at") if conn is not None else None
        err = _meta(conn, f"{kind}_last_error") if conn is not None else None
        if synced or err or kind in sources():
            out.append({"kind": kind, "synced_at": float(synced) if synced else None,
                        "error": err})
    return out


#: `GET /contacts`'s orders: `name` (the default), `recent` (last talked to
#: first) and `most` (most talked to over the last 90 days first); a person
#: never talked to comes after everyone who was, by name.
SORTS = ("name", "recent", "most")


def _services_of(card: dict, act: dict | None) -> set[str]:
    """The timeline's channels a person is on: their days in the ledger and
    the bridged networks their handles are on."""
    from . import timeline

    got = set((act or {}).get("channels") or {})
    for h in card.get("handles") or []:
        ch = timeline._NETWORKS.get(str(h.get("network") or ""))
        if ch:
            got.add(ch)
    return got


def listing(q: str = "", limit: int | str | None = None,
            label: str | list[str] | tuple = "", mode: str = "any",
            sort: str = "", since: int | str | None = None, service: str = "") -> dict:
    """`GET /contacts`: everyone, or those `q` matches as it is typed (name,
    a phone's digits as written or dialled at home, an email); `label`, one
    label or several, only the people with any of them (`mode` "any", the
    default) or with every one (`mode` "all"); `since`, only those talked to
    in the last that many days; `service`, only those on that channel (a
    timeline channel: sms, messenger, …). Each row carries `last` {day,
    channel} and `count_90d` from the book's ledger (timeline.activity);
    `sort` is one of SORTS, after how well `q` matches when there is one."""
    from . import timeline

    try:
        n = max(1, min(int(limit or LIMIT), LIMIT_MAX))
    except (TypeError, ValueError):
        n = LIMIT
    try:
        days = int(since) if since not in (None, "") else 0
    except (TypeError, ValueError):
        days = 0
    order = (sort or "").strip().lower()
    order = order if order in SORTS else "name"
    service = (service or "").strip().lower()
    picked = [label] if isinstance(label, str) else list(label or [])
    want = {fold(x) for x in picked if x and fold(x)}
    every = (mode or "").strip().lower() == "all"
    try:
        acts = timeline.activity()
    except Exception:  # noqa: BLE001 — a ledger that cannot be read is no activity
        log.exception("contacts: the timeline's activity")
        acts = {}
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat() if days > 0 else ""
    from . import contacts_photos

    conn = _connect(create=False)
    rows: list[tuple] = []
    try:
        if conn is not None:
            pics = contacts_photos.index(conn)
            for cid, name, raw in conn.execute("SELECT id, name, card FROM contacts"):
                card = json.loads(raw)
                if want:
                    has = {fold(x) for x in card.get("labels") or []}
                    if not (want <= has if every else want & has):
                        continue
                act = acts.get(cid)
                last = (act or {}).get("last")
                if cutoff and not (last and last["day"] >= cutoff):
                    continue
                if service and service not in _services_of(card, act):
                    continue
                score = _matches(q, name, card) if q else 2
                if score:
                    row = _row(cid, name, card)
                    row["photo"] = contacts_photos.field(cid, card, pics)
                    row["last"] = last
                    row["count_90d"] = (act or {}).get("count_90d", 0)
                    rows.append((score, fold(name), row))
        states = _source_states(conn)
    finally:
        if conn is not None:
            conn.close()
    rows.sort(key=lambda r: r[1])
    if order == "recent":
        rows.sort(key=lambda r: (r[2]["last"] or {}).get("day") or "", reverse=True)
    elif order == "most":
        rows.sort(key=lambda r: (r[2]["count_90d"], (r[2]["last"] or {}).get("day") or ""),
                  reverse=True)
    rows.sort(key=lambda r: -r[0])
    synced = [s["synced_at"] for s in states if s["synced_at"]]
    return {"contacts": [r[2] for r in rows[:n]], "total": len(rows), "sort": order,
            "talked": bool(acts),
            "synced_at": max(synced) if synced else None, "sources": states}


#: Where a card's "Their emails" goes: a mail search, `{query}` the
#: URL-encoded `from:(a) OR to:(a) …` over every address. Per book:
#: `MEDIA_CONTACTS_MAIL_URL` (the owner's) or `contacts.json` `mail_url`;
#: "off" leaves the link out.
MAIL_URL = "https://mail.google.com/mail/u/0/#search/{query}"


#: The longest mail search link made: addresses past it are left out
#: (`left_out`), the card's own first, then the old ones last seen first.
MAIL_URL_MAX = 2000


def mail_link(emails: list, aliases: list | None = None) -> dict | None:
    """`{url, label}` for a search of the mail to and from these addresses
    and the old chat addresses (`aliases`), or None (no address, the link
    switched off, a template without `{query}`); `left_out` counts what
    did not fit in `MAIL_URL_MAX`."""
    got: list[str] = []
    for e in list(emails or []) + list(aliases or []):
        a = (e.get("value") if isinstance(e, dict) else e) or ""
        a = str(a).strip()
        if a and "@" in a and a.lower() not in {x.lower() for x in got}:
            got.append(a)
    if not got:
        return None
    tpl = (setting("MEDIA_CONTACTS_MAIL_URL", "mail_url") or "").strip() or MAIL_URL
    if tpl.lower() in ("off", "0", "none", "no") or "{query}" not in tpl:
        return None
    url, used = "", 0
    for i in range(1, len(got) + 1):
        query = " OR ".join(f"from:({a}) OR to:({a})" for a in got[:i])
        u = tpl.replace("{query}", urllib.parse.quote(query, safe=""))
        if i > 1 and len(u) > MAIL_URL_MAX:
            break
        url, used = u, i
    host = urllib.parse.urlsplit(url).hostname or ""
    label = "Gmail" if host == "mail.google.com" else (host or "Mail")
    out = {"url": url, "label": label}
    if used < len(got):
        out["left_out"] = len(got) - used
    return out


def _card_mail(c: dict) -> dict | None:
    """The card's `mail`: the browser search (`url`, `label`, `left_out`;
    mail_link), `mailto` for "Email them", and `recent: true` when their
    newest mail can be listed (`GET /contacts/<id>/mail`, contacts_mail.py).
    None when there is none of these."""
    from . import contacts_mail

    out = dict(mail_link(c.get("emails") or [],
                         [x.get("address") for x in c.get("old_addresses") or []]) or {})
    to = contacts_mail.mailto(c)
    if to:
        out["mailto"] = to
    if contacts_mail.addresses(c) and contacts_mail.available():
        out["recent"] = True
    return out or None


def card(cid: str) -> dict | None:
    """`GET /contacts/<id>`'s contact: the merged card, its sources, and the
    rooms that are this person's."""
    from . import contacts_edit, contacts_find, contacts_level, contacts_photos

    conn = _connect(create=False)
    if conn is None:
        return None
    try:
        got = conn.execute("SELECT name, card, updated FROM contacts WHERE id=?", (cid,)).fetchone()
        pics = contacts_photos.index(conn) if got else {}
        said_none = contacts_find.dismissed_for(conn, cid) if got else set()
        level = contacts_level.of(conn, cid) if got else None
    finally:
        conn.close()
    if not got:
        return None
    c = json.loads(got[1])
    return {"id": cid, "crm": _crm_of(c), "name": got[0], "names": c.get("names") or [],
            "photo": contacts_photos.field(cid, c, pics),
            "find": contacts_find.findable(c, said_none),
            "phones": c.get("phones") or [], "emails": c.get("emails") or [],
            "addresses": c.get("addresses") or [], "organizations": c.get("organizations") or [],
            "notes": c.get("notes") or "", "sources": c.get("sources") or [],
            "labels": c.get("labels") or [], "handles": c.get("handles") or [],
            "suggestions": suggestions(cid),
            "old_addresses": c.get("old_addresses") or [],
            "mail": _card_mail(c),
            # Edit from the card (contacts_edit.py), and the person in Google.
            "edit": contacts_edit.editable(c), "google_url": contacts_edit.google_url(c) or None,
            # Their speech level (contacts_level.py): None is "default".
            "level": level,
            "updated": round(got[2], 3) if got[2] else None, "rooms": rooms_of(cid)}


def labels() -> dict:
    """`GET /contacts/labels`: every label on someone in the book, with how
    many people have it, by name."""
    conn = _connect(create=False)
    counts: dict[str, list] = {}
    try:
        if conn is not None:
            for (raw,) in conn.execute("SELECT card FROM contacts"):
                for x in json.loads(raw).get("labels") or []:
                    counts.setdefault(fold(x), [x, 0])[1] += 1
    finally:
        if conn is not None:
            conn.close()
    return {"labels": [{"name": name, "count": n}
                       for name, n in sorted(counts.values(), key=lambda v: fold(v[0]))]}


# --- "Same person?" -------------------------------------------------------------

def _bridge_sids(card: dict) -> list[str]:
    """The card's weak records (a bridge's ghost, an export's person): what
    a Join or a "Not the same" is kept against."""
    return [s["id"] for s in card.get("sources") or [] if s.get("kind") in WEAK]


def _bridge_only(card: dict) -> bool:
    """Known only from a bridge or an export: a name joins nobody here."""
    srcs = card.get("sources") or []
    return bool(srcs) and all(s.get("kind") in WEAK for s in srcs)


def suggestions(cid: str) -> list[dict]:
    """Other contacts with this one's name, or a nickname of it with the same
    surname (contacts_review.py), where one of the two is known only from a
    bridge or an export (a name joins nobody there by itself), the two do not
    both have numbers of their own, and David has not said they are not the
    same: `[{id, name, why: "same name" | "nickname"}]`."""
    from . import contacts_review

    idx = _index()
    if idx is None or cid not in idx["display"]:
        return []
    return [{"id": other, "name": idx["display"].get(other) or "", "why": why}
            for other, why in contacts_review.candidates(idx, cid)]


def _fresh() -> None:
    """This process's matcher reads the store again on its next use: after
    a decision made here, the card asked for next must already show it."""
    idx = _INDEXES.get(account())
    if idx is not None:
        idx["checked"] = 0.0


class NotBridged(ValueError):
    """Neither contact is known only from a bridge: nothing to join."""


def _pair(conn: sqlite3.Connection, a: str, b: str) -> tuple[dict, str, dict, str]:
    rows = {cid: json.loads(raw) for cid, raw in conn.execute(
        "SELECT id, card FROM contacts WHERE id IN (?, ?)", (a, b))}
    if a not in rows or b not in rows or a == b:
        raise KeyError(b if a in rows else a)
    return rows[a], a, rows[b], b


def same(cid: str, other: str) -> dict:
    """`POST /contacts/<id>/same {"other"}`: David says the two are one
    person. The one known only from a bridge joins the other (kept in
    `merges`, so it holds whatever the names do later); `{id}` is who is left.
    KeyError when either is not in the book, NotBridged when neither is a
    bridge's alone."""
    with _WRITE:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ca, a, cb, b = _pair(conn, cid, other)
            if _bridge_only(ca):
                joining, into = ca, b
            elif _bridge_only(cb):
                joining, into = cb, a
            else:
                raise NotBridged("neither is known only from a bridge")
            now = time.time()
            for sid in _bridge_sids(joining):
                conn.execute("DELETE FROM merges WHERE source=? AND kind='join'", (sid,))
                conn.execute("INSERT OR REPLACE INTO merges (source, contact, kind, at) "
                             "VALUES (?, ?, 'join', ?)", (sid, into, now))
            _rebuild(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    _fresh()
    # Their Facebook and the like go onto the Google entry, through the
    # phone (contacts_writeback.py).
    from . import contacts_writeback

    contacts_writeback.queue(into)
    return {"id": into}


def different(cid: str, other: str) -> dict:
    """`POST /contacts/<id>/different {"other"}`: not the same person; the
    suggestion goes and a trusted name never joins them."""
    with _WRITE:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ca, a, cb, b = _pair(conn, cid, other)
            pairs = [(sid, b) for sid in _bridge_sids(ca)] + [(sid, a) for sid in _bridge_sids(cb)]
            if not pairs:
                raise NotBridged("neither is known from a bridge")
            now = time.time()
            for sid, into in pairs:
                conn.execute("INSERT OR REPLACE INTO merges (source, contact, kind, at) "
                             "VALUES (?, ?, 'dismiss', ?)", (sid, into, now))
            _rebuild(conn)             # a trusted name that joined them lets go
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    _fresh()
    return {"id": cid}


def _crm_of(c: dict) -> dict | None:
    """The card's CRM section: the person there, and the fields only the
    CRM has. None when the CRM does not know them."""
    from . import contacts_drupal

    uuid = next((s.get("id") for s in c.get("sources") or [] if s.get("kind") == "drupal"), None)
    if not uuid:
        return None
    return {"id": uuid, "url": contacts_drupal.contact_url(uuid), "fields": c.get("crm") or {}}


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
    prefix, sep, rest = local.partition("_")
    # Only these bridges name a ghost by its number: Messenger's and
    # Instagram's are account ids (`@facebook_100012345678901`), digits that
    # would read as a number in no country (19 Oct 2026).
    if sep and prefix in NUMBER_GHOSTS:
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


_INDEX: dict = {"gen": None, "checked": 0.0}      # the owner's
_INDEXES: dict = {"": _INDEX}                      # every book's, by account
_ROOM_MEMO: dict = {}
#: How long the store's generation is trusted before it is read again: a
#: sync from the CLI (another process) shows within this.
_GEN_TTL_S = 2.0


def _index() -> dict | None:
    """Phones, emails and names to contact ids, and the overrides; rebuilt
    when the store changes. None when there is no store."""
    now = time.monotonic()
    idx = _INDEXES.setdefault(account(), {"gen": None, "checked": 0.0})
    if idx.get("gen") is not None and now - idx["checked"] < _GEN_TTL_S:
        return idx
    conn = _connect(create=False)
    if conn is None:
        idx.update(gen=None, checked=now)
        return None
    try:
        gen = _meta(conn, "gen") or "0"
        if gen == idx.get("gen"):
            idx["checked"] = now
            return idx
        phones: dict[str, set] = {}
        emails: dict[str, set] = {}
        names: dict[str, set] = {}
        display: dict[str, str] = {}
        card_names: dict[str, set] = {}
        handles: dict[str, str] = {}
        bridge_only: set[str] = set()
        holder: dict[str, str] = {}          # a bridge record's sid → its contact
        for cid, num in conn.execute("SELECT contact, e164 FROM phones"):
            phones.setdefault(num, set()).add(cid)
        for cid, em in conn.execute("SELECT contact, email FROM emails"):
            emails.setdefault(em, set()).add(cid)
        for cid, name, raw in conn.execute("SELECT id, name, card FROM contacts"):
            display[cid] = name
            c = json.loads(raw)
            card_names[cid] = {fold(name)} | {fold(x.get("display") or "")
                                              for x in c.get("names") or []}
            card_names[cid].discard("")
            for n in card_names[cid]:
                names.setdefault(n, set()).add(cid)
            for h in c.get("handles") or []:
                if h.get("id"):
                    handles[h["id"]] = cid
            for sid in _bridge_sids(c):
                holder[sid] = cid
            if _bridge_only(c):
                bridge_only.add(cid)
        dismissed = {(holder[s], other) for s, other, kind in conn.execute(
            "SELECT source, contact, kind FROM merges") if kind == "dismiss" and s in holder}
        joins: dict[str, str] = {}
        splits: dict[str, set] = {}
        for room, cid, kind in conn.execute("SELECT room, contact, kind FROM overrides"):
            if kind == "join":
                joins[room] = cid
            else:
                splits.setdefault(room, set()).add(cid)
    finally:
        conn.close()
    idx.clear()
    idx.update(gen=gen, checked=now, phones=phones, emails=emails, names=names,
               display=display, joins=joins, splits=splits, card_names=card_names,
               handles=handles, bridge_only=bridge_only, dismissed=dismissed)
    _ROOM_MEMO.clear()
    return idx


def _self_ids() -> tuple[set, set]:
    """`MEDIA_CONTACTS_SELF` (another account's `contacts.json` `self`):
    David's own numbers and addresses, comma separated. Texts sent from the phone itself come in as the number's
    puppet (no double puppeting), so his own number is in every SMS room."""
    phones, emails = set(), set()
    for x in (setting("MEDIA_CONTACTS_SELF", "self") or "").split(","):
        x = x.strip()
        if "@" in x:
            emails.add(x.lower())
        elif x and (num := e164(x, region())):
            phones.add(num)
    return phones, emails


def match_room(room_id: str, members: dict[str, str], skip: set[str],
               network: str | None = None) -> dict | None:
    """`{id, name}` of the one contact a room is with, or None.

    Each member other than `skip` (the owner, the agent, the bridge bots) is
    matched by the ghost a contact holds (`handles`, the bridged networks'
    people), else phone, else email, else — the bridge's name for them being
    the phone's own contact name, so only on a trusted network (or one not
    named) — a name no other contact has. A room is a contact's when its
    members match exactly one; a group of several people is no one's. A
    manual join wins; a split takes that contact out."""
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
    by_name = not network or network in trusted_names()
    found: set[str] = set()
    for uid, dn in members.items():
        if uid in skip:
            continue
        if uid in idx["handles"]:
            got = {idx["handles"][uid]} - split
            if got:
                found |= got
                continue
        who = identities(uid, dn, reg)
        got: set[str] = set()
        for num in who["phones"] - self_phones:
            got |= idx["phones"].get(num, set())
        if not got:
            for em in who["emails"] - self_emails:
                got |= idx["emails"].get(em, set())
        if not got and who["name"] and by_name:
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
    remembered per room until its members or the store change.

    The Matrix rooms this server reads are the owner's, so they are joined
    to the owner's book only: in anyone else's, no room is a contact's."""
    if not is_owner():
        return None
    idx = _index()
    if idx is None:
        return None
    with r.lock:
        members = dict(r.members)
    network = (r.network or {}).get("id") or None
    key = (idx["gen"], r.id, tuple(sorted(members.items())), network,
           os.environ.get("MEDIA_CONTACTS_SELF") or "")
    if key in _ROOM_MEMO:
        return _ROOM_MEMO[key]
    got = match_room(r.id, members, _skip_of(r), network)
    if len(_ROOM_MEMO) > 2000:
        _ROOM_MEMO.clear()
    _ROOM_MEMO[key] = got
    return got


def for_member(r, uid: str) -> dict | None:
    """`{id, name}` of the contact one member of a room is (20 Oct 2026: a
    sender's picture and card in a group chat), or None: matched as a room
    with that member alone is (`match_room`'s handle, number, address, name
    rules, the room's own joins aside). The owner's book only; never the
    owner, the agent or a bridge's bot."""
    if not is_owner():
        return None
    idx = _index()
    if idx is None:
        return None
    skip = _skip_of(r)
    if uid in skip:
        return None
    with r.lock:
        name = r.members.get(uid) or ""
    network = (r.network or {}).get("id") or None
    key = (idx["gen"], "member", uid, name, network,
           os.environ.get("MEDIA_CONTACTS_SELF") or "")
    if key in _ROOM_MEMO:
        return _ROOM_MEMO[key]
    got = match_room(f"{r.id}|{uid}", {uid: name}, skip, network)
    if len(_ROOM_MEMO) > 2000:
        _ROOM_MEMO.clear()
    _ROOM_MEMO[key] = got
    return got


def rooms_of(cid: str) -> list[dict]:
    """The Matrix rooms (sessions) that are this contact's, newest first."""
    from . import matrix

    out = []
    if not is_owner():
        return out
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
    _INDEXES.clear()
    _INDEXES[""] = _INDEX
    _ROOM_MEMO.clear()
    _ACCESS.clear()
    _ACCESSES.clear()
    _ACCESSES[""] = _ACCESS
    _PENDING.clear()
    from . import contacts_find

    contacts_find._reset_for_tests()


# --- the background sync --------------------------------------------------------

_STARTED = threading.Event()


def start() -> None:
    """The canvas's sync loop, once per process: the phone every
    `MEDIA_CONTACTS_INTERVAL_S` (900), Google and the CRM too when set up,
    then the timeline's rollup (timeline.py; `MEDIA_TIMELINE=0` leaves it
    off); idle on a host with no source; off with `MEDIA_CONTACTS=0`. The
    speech levels read on this server ask this book for a room's person from
    here on (contacts_level.register), and the rooms' quiet seed becomes the
    rooms' fallback once (matrix.unseed_quiet)."""
    from . import contacts_level, matrix

    contacts_level.register()
    matrix.unseed_quiet()
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
        for key in accounts():
            with as_account(key):
                _sync_book(key)
        time.sleep(max(30.0, interval))


def _sync_book(key: str) -> None:
    """One book's sync and rollup, in the loop: said, never raised."""
    who = f"contacts[{key}]" if key else "contacts"
    try:
        if refold():
            print(f"{who}: cards made again ({CARDS_VERSION})", file=sys.stderr)
        got = sync()
        for s in got["synced"]:
            if s.get("changed") or s.get("deleted") or s.get("unchanged") is False:
                print(f"{who}: {s['source']}: {s['contacts']} contacts", file=sys.stderr)
        for s, e in got["errors"].items():
            # The phone off Wi-Fi is the usual one: the last copy serves.
            print(f"{who}: {s} unread ({e})", file=sys.stderr)
        if (os.environ.get("MEDIA_CONTACTS_PHOTOS") or "1").strip() != "0":
            # People's pictures (contacts_photos.py): what is new or due.
            from . import contacts_photos

            ph = contacts_photos.refresh()
            if ph["fetched"] or ph["dropped"]:
                print(f"{who}: pictures: {ph['kept']} kept, {ph['none']} none, "
                      f"{ph['failed']} failed, {ph['waiting']} waiting", file=sys.stderr)
        if not key:
            # The export cycle (contacts_exports.py): files in Downloads,
            # the "ready" mail, the reminder. The owner's book only.
            from . import contacts_exports

            x = contacts_exports.run()
            w = x.get("watch") or {}
            bad = {k: v for k, v in (w.get("errors") or {}).items() if k != "phone"}
            if w.get("imported") or bad:
                # The phone off Wi-Fi is said by its own source already.
                print(f"{who}: exports: {len(w.get('imported') or [])} imported, "
                      f"{len(bad)} not ({'; '.join(bad.values())[:200]})", file=sys.stderr)
            # Joins the phone could not take yet (contacts_writeback.py).
            from . import contacts_writeback

            wb = contacts_writeback.flush()
            if wb["written"] or wb.get("error"):
                print(f"{who}: google write-back: {wb['written']} written, "
                      f"{wb['waiting']} waiting {wb.get('error') or ''}".rstrip(), file=sys.stderr)
        if (os.environ.get("MEDIA_TIMELINE") or "1").strip() != "0":
            from . import timeline

            t = timeline.rollup()
            if t["changed"] or t["errors"] or t["source_errors"]:
                print(f"timeline{f'[{key}]' if key else ''}: {t['changed']} changed, "
                      f"{t['pushed']} pushed, {t['errors']} failed "
                      f"{t['source_errors'] or ''}".rstrip(), file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — never take the canvas down
        print(f"{who}: sync failed ({e})", file=sys.stderr)


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


# --- the sign-in, from the app (§6.25 "Google, per book") ----------------------
#
# The app on the phone runs the loopback listener (Google's Desktop client
# allows any `http://127.0.0.1:<port>`) and opens the consent page in a
# Custom Tab; the server keeps the PKCE verifier, so it never leaves here,
# and the token lands in the caller's book.

#: How long a started sign-in waits for its finish.
APP_LOGIN_TTL_S = 900.0
#: Started sign-ins, by state: `{account, verifier, redirect_uri, expires}`.
_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()


def start_login(port) -> dict:
    """`POST /contacts/google/login {port}`: the consent URL for this book,
    redirecting to the app's loopback listener on `port`."""
    client = load_client()
    if client is None:
        raise NotSignedIn("this server has no Google OAuth client")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError('expected {"port"}: the app\'s loopback port') from None
    if not 1024 <= port <= 65535:
        raise ValueError("port must be 1024-65535")
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce()
    redirect = f"http://127.0.0.1:{port}"
    now = time.time()
    with _PENDING_LOCK:
        for k in [k for k, v in _PENDING.items() if v["expires"] < now]:
            del _PENDING[k]
        _PENDING[state] = {"account": account(), "verifier": verifier,
                           "redirect_uri": redirect, "expires": now + APP_LOGIN_TTL_S}
    return {"url": consent_url(client, redirect, state, challenge), "state": state,
            "redirect_uri": redirect, "expires_in": int(APP_LOGIN_TTL_S)}


def finish_login(state: str, code: str = "", url: str = "") -> dict:
    """`POST /contacts/google/finish {state, code | url}`: the code (or the
    whole redirect address) for a token in this book, then a sync of it.
    ValueError for an unknown, spent or expired state, or an address that is
    not this sign-in's; PermissionError for another account's state."""
    state = str(state or "")
    with _PENDING_LOCK:
        p = _PENDING.get(state)
        if p is None or p["expires"] < time.time():
            _PENDING.pop(state, None)
            raise ValueError("that sign-in has expired or was never started; start again")
        if p["account"] != account():
            raise PermissionError("that sign-in is not this account's")
        got = code_from(url or code, state)
        del _PENDING[state]                    # one use, before Google sees it
    client = load_client()
    if client is None:
        raise NotSignedIn("this server has no Google OAuth client")
    exchange(client, got, p["verifier"], p["redirect_uri"])
    _kick_sync(account())
    return google_state()


def _kick_sync(key: str) -> None:
    """A full Google sync of `key`'s book, off the request's thread."""
    def run():
        with as_account(key):
            try:
                sync_google(full=True)
            except Exception as e:  # noqa: BLE001 — said in the book's state
                log.info("contacts[%s]: google sync after sign-in: %s", key or "owner", e)
    threading.Thread(target=run, name="contacts-google-signin", daemon=True).start()


def google_state() -> dict:
    """`GET /contacts/google`: this book's Google — `{available, signed_in,
    email, expired, signed_in_at, last_sync, last_error, contacts}` — and
    `exports`, where each network's export is requested (contacts_exports)."""
    tok = load_token()
    conn = _connect(create=False)
    synced = err = None
    n = 0
    try:
        if conn is not None:
            synced = _meta(conn, "google_synced_at")
            err = _meta(conn, "google_last_error")
            n = conn.execute("SELECT COUNT(*) FROM records WHERE kind='google'").fetchone()[0]
    finally:
        if conn is not None:
            conn.close()
    expired = bool(tok and tok.get("expired_at"))
    from .contacts_exports import export_links

    return {"available": load_client() is not None, "exports": export_links(),
            "signed_in": bool(tok) and not expired, "expired": expired,
            "email": (tok or {}).get("email") or None,
            "signed_in_at": (tok or {}).get("obtained_at"),
            "last_sync": float(synced) if synced else None,
            "last_error": err, "contacts": n}


def sign_out() -> dict:
    """`POST /contacts/google/signout`: this book forgets Google — the token
    (revoked at Google when it can be), and Google's records in the book."""
    tok = load_token()
    if tok:
        try:
            _request("POST", "https://oauth2.googleapis.com/revoke",
                     form={"token": tok["refresh_token"]}, timeout=10.0)
        except (OSError, urllib.error.URLError):
            pass                               # forgotten here either way
    try:
        token_path().unlink()
    except FileNotFoundError:
        pass
    _access().clear()
    with _WRITE:
        conn = _connect(create=False)
        if conn is not None:
            try:
                conn.execute("BEGIN IMMEDIATE")
                gone = conn.execute("DELETE FROM records WHERE kind='google'").rowcount
                for k in ("google_sync_token", "google_synced_at", "google_last_error",
                          "google_failed_at"):
                    _set_meta(conn, k, None)
                if gone:
                    _rebuild(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
    return google_state()


def status() -> dict:
    from . import contacts_android

    conn = _connect(create=False)
    count, per = 0, {}
    try:
        if conn is not None:
            count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            for kind in KINDS:
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
    return {"account": account() or "owner", "store": str(db_path()), "contacts": count,
            "sources": sources(),
            "android": {"command": " ".join(contacts_android.command()),
                        "available": contacts_android.available(), **per.get("android", {})},
            "google": {"client": str(client_path()) if not os.environ.get("MEDIA_GOOGLE_OAUTH_CLIENT")
                       else "MEDIA_GOOGLE_OAUTH_CLIENT",
                       "client_found": load_client() is not None,
                       "token": str(token_path()),
                       "signed_in": tok is not None and not tok.get("expired_at"),
                       "expired": bool(tok and tok.get("expired_at")),
                       "email": (tok or {}).get("email") or None,
                       "signed_in_at": tok.get("obtained_at") if tok else None,
                       **per.get("google", {})},
            "bridges": {"owner_bridges": sorted(_owner_bridges()), **per.get("bridges", {})},
            "import": {**per.get("import", {})},
            "drupal": {"connection": str(_drupal_config_path()), "configured": _drupal_configured(),
                       **per.get("drupal", {})},
            "background": (os.environ.get("MEDIA_CONTACTS") or "1").strip() != "0"}


def _owner_bridges() -> set[str]:
    from . import matrix

    return matrix._bridges() if is_owner() else set()


def _drupal_config_path():
    from . import contacts_drupal

    return "MEDIA_DRUPAL_CRM" if owner_env("MEDIA_DRUPAL_CRM") else contacts_drupal.config_path()


def _drupal_configured() -> bool:
    from . import contacts_drupal

    return contacts_drupal.configured()


def _when(v):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v)) if isinstance(v, float) else v


def cmd_status(_a) -> int:
    s = status()
    for k in ("account", "store", "contacts", "sources", "background"):
        print(f"{k:13} {s[k]}")
    for src in KINDS:
        for k, v in s[src].items():
            print(f"{src + '.' + k:24} {_when(v) if k.endswith('_at') else v}")
    return 0 if s["sources"] else 1


def cmd_sync(a) -> int:
    which = getattr(a, "source", "") or ""
    if which:
        try:
            got = {"synced": [_SYNCS[which](bool(getattr(a, "full", False)))],
                   "errors": {}}
        except Exception as e:  # noqa: BLE001 — said, with its source
            got = {"synced": [], "errors": {which: str(e)}}
    else:
        got = sync(full=bool(getattr(a, "full", False)))
        if not got["synced"] and not got["errors"]:
            print("no source to read: no phone tool here, not signed in to Google, "
                  "and no CRM connection",
                  file=sys.stderr)
            return 2
    for s in got["synced"]:
        if s["source"] == "android":
            print(f"android: {'unchanged' if s['unchanged'] else 'read'} {s['read']} "
                  f"contact(s) from the phone, {s['contacts']} contacts")
        elif s["source"] == "drupal":
            print(f"drupal: {'unchanged' if s['unchanged'] else 'read'} {s['read']} "
                  f"person(s) from the CRM, {s['contacts']} contacts")
        elif s["source"] == "bridges":
            if not s["rooms"]:
                print(f"bridges: no bridged rooms in this process (the canvas reads them), "
                      f"kept {s['read']} person(s), {s['contacts']} contacts")
            else:
                print(f"bridges: {'unchanged' if s['unchanged'] else 'read'} {s['read']} "
                      f"person(s) from {s['rooms']} room(s), {s['contacts']} contacts")
        else:
            print(f"google {'full' if s['full'] else 'changes'}: {s['changed']} changed, "
                  f"{s['deleted']} gone, {s['contacts']} contacts")
    for src, e in got["errors"].items():
        print(f"{src}: sync failed: {e}", file=sys.stderr)
    return 1 if got["errors"] else 0


def cmd_import(a) -> int:
    from . import contacts_import

    path = Path(a.file).expanduser()
    try:
        got = import_path(path)
    except OSError as e:
        print(f"contacts: {e}", file=sys.stderr)
        return 2
    except contacts_import.NotAnExport as e:
        print(f"contacts: {e}", file=sys.stderr)
        return 2
    lists = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(got["imported"].items()))
    line = f"imported {lists or 'nothing new'}"
    if got.get("people"):
        line += (f": {got['people']} people from exports, {got['joined']} of them joined "
                 "someone already here")
    print(f"{line}; {got['contacts']} contacts")
    for net, fields in sorted((got.get("profile") or {}).items()):
        print(f"your {net.capitalize()} profile: {', '.join(fields) or 'nothing filled'}")
    h = got.get("history")
    if h:
        print(f"history: {h['days']} days added or raised in the timeline, "
              f"{h['new_people']} new people, {h['groups_left_out']} group conversations "
              f"left out, {h['unmatched']} with no one to match")
        if h["unknown_self"]:
            print(f"history: {h['unknown_self']} conversations where you were not found: set "
                  "MEDIA_CONTACTS_SELF_NAME to your name as the export writes it",
                  file=sys.stderr)
        if h["chats_without_the_label"]:
            print(f"history: {h['chats_without_the_label']} mails without the Chat label "
                  "left out", file=sys.stderr)
    return 0


# --- seeding the CRM ------------------------------------------------------------

def _seedable(card: dict) -> bool:
    """Worth a CRM person: a name, and a number or an address to reach them."""
    name = card.get("name") or ""
    return bool(name and name != "(no name)") and bool(
        any(p.get("value") or p.get("raw") for p in card.get("phones") or [])
        or any(e.get("value") for e in card.get("emails") or []))


def seed(dry_run: bool = True, limit: int = 0) -> dict:
    """Add the contacts the CRM does not hold to it (the proposal: "The CRM
    is for everyone … seeded from Google"). Idempotent, keyed by the
    contact's id: one with a CRM source, or linked by an earlier seed, is
    skipped, and each person made is linked at once, before their numbers
    are added. A real run reads the CRM first, so a person already there
    by number is joined, not made twice. A dry run writes nothing and reads
    only the store."""
    from . import contacts_drupal

    if not dry_run:
        if not contacts_drupal.configured():
            raise contacts_drupal.NotConfigured(
                f"no CRM connection ({contacts_drupal.config_path()})")
        sync_drupal()
    conn = _connect(create=False)
    todo, held, thin = [], 0, 0
    try:
        if conn is not None:
            linked = {c for (c,) in conn.execute("SELECT contact FROM drupal_links")}
            for cid, name, raw in conn.execute("SELECT id, name, card FROM contacts ORDER BY name"):
                c = json.loads(raw)
                if cid in linked or any(x.get("kind") == "drupal" for x in c.get("sources") or []):
                    held += 1
                elif not _seedable(c):
                    thin += 1
                else:
                    todo.append((cid, c))
    finally:
        if conn is not None:
            conn.close()
    if limit and limit > 0:
        todo = todo[:limit]
    made, errors = [], []
    if not dry_run:
        for cid, c in todo:
            try:
                uuid = contacts_drupal.create_person(c)
                link_drupal(uuid, cid)
                contacts_drupal.add_methods(uuid, c)
                made.append({"contact": cid, "uuid": uuid, "name": c.get("name")})
            except Exception as e:  # noqa: BLE001 — one refused is said; the rest go on
                errors.append({"contact": cid, "name": c.get("name"), "error": str(e)})
        if made:
            sync_drupal()
    return {"dry_run": dry_run, "would_add" if dry_run else "added":
            [c.get("name") for _, c in todo] if dry_run else made,
            "already": held, "skipped_no_way_to_reach": thin, "errors": errors}


def cmd_seed(a) -> int:
    from . import contacts_drupal

    try:
        got = seed(dry_run=not a.go, limit=a.limit)
    except (contacts_drupal.NotConfigured, contacts_drupal.CrmError, OSError) as e:
        print(f"seed: {e}", file=sys.stderr)
        return 1
    if got["dry_run"]:
        names = got["would_add"]
        print(f"would add {len(names)} person(s) to the CRM; {got['already']} already there, "
              f"{got['skipped_no_way_to_reach']} without a number or address left out")
        for n in names[:20]:
            print(f"  {n}")
        if len(names) > 20:
            print(f"  … and {len(names) - 20} more")
        print("nothing written: --go to add them (--limit 1 to try one first)")
        return 0
    print(f"added {len(got['added'])} person(s) to the CRM; {got['already']} already there")
    for e in got["errors"]:
        print(f"  {e['name']}: {e['error']}", file=sys.stderr)
    return 1 if got["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent_media_server.contacts",
                                 description="Contacts for agent-media: the phone's over "
                                             "adb, Google's if signed in, the Drupal CRM's "
                                             "if connected, and their timeline.")
    ap.add_argument("--account", default="",
                    help="whose book: an account key, or an enrolled account's email "
                         "(default: the owner's; `accounts` lists them)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("accounts", help="the books on this host, and the accounts "
                                    "enrolled here").set_defaults(fn=cmd_accounts)
    p = sub.add_parser("login", help="sign in to Google (works from a phone)")
    p.add_argument("--port", type=int, default=0, help="loopback port (default: any free one)")
    p.set_defaults(fn=cmd_login)
    sub.add_parser("status", help="what is read, and when it last was").set_defaults(fn=cmd_status)
    p = sub.add_parser("sync", help="sync now")
    p.add_argument("--source", choices=[k for k in KINDS if k in _SYNCS], default="",
                   help="only this one (default: every source this host reads)")
    p.add_argument("--full", action="store_true", help="Google: everything, not the changes")
    p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("import", help="people and past conversations from a data export: "
                                      "Facebook or Instagram (.zip or JSON), Google Takeout's "
                                      "Chats (.zip or .mbox); any size, read as it goes")
    p.add_argument("file", help="the export's .zip, an .mbox, your_friends.json, "
                                "followers_1.json, following.json or a message_1.json")
    p.set_defaults(fn=cmd_import)
    p = sub.add_parser("seed", help="add the people the CRM lacks to it (a dry run unless --go)")
    p.add_argument("--go", action="store_true", help="write to the CRM (default: say what would be)")
    p.add_argument("--limit", type=int, default=0, help="at most this many (0: all)")
    p.set_defaults(fn=cmd_seed)
    from . import timeline

    timeline.add_commands(sub)
    from . import timeline_backfill

    timeline_backfill.add_commands(sub)
    from . import contacts_exports

    contacts_exports.add_commands(sub)
    a = ap.parse_args(argv)
    try:
        key = resolve_account(a.account)
    except ValueError as e:
        print(f"contacts: {e}", file=sys.stderr)
        return 2
    with as_account(key):
        return a.fn(a)


def enrolled_accounts() -> list[dict]:
    """The accounts with a device here, and the book each reads:
    `[{key, account, iss, sub, devices}]` ("" is the owner's)."""
    from . import auth, devices

    seen: dict[tuple, dict] = {}
    for d in devices._load(fresh=True):
        if not d.get("sub") or devices.is_jobs_worker(d):
            continue
        k = (str(d.get("iss") or "").rstrip("/"), str(d["sub"]))
        row = seen.setdefault(k, {"key": auth.device_account(d), "account": d.get("account") or "",
                                  "iss": k[0], "sub": k[1], "devices": 0})
        row["devices"] += 1
    return list(seen.values())


def resolve_account(who: str) -> str:
    """`--account`: "" (the owner), an account key, or an enrolled account's
    email or sub. Unknown is an error, never a fresh empty book."""
    from . import auth

    who = (who or "").strip()
    if not who or who == "owner":
        return ""
    if auth.ACCOUNT_KEY.fullmatch(who):
        return who
    hits = {r["key"] for r in enrolled_accounts()
            if who.lower() in (r["account"].lower(), r["sub"].lower())}
    if len(hits) == 1:
        return hits.pop()
    raise ValueError(f"no account here is {who!r}" if not hits
                     else f"{who!r} is more than one account; give its key")


def cmd_accounts(_a) -> int:
    rows = {r["key"]: r for r in enrolled_accounts()}
    for key in sorted(set(accounts()) | set(rows)):
        r = rows.get(key)
        label = "owner" if not key else key
        extra = f"  {r['account'] or r['sub']} ({r['devices']} device(s))" if r else ""
        print(f"{label}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
