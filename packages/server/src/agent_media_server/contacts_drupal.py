"""The Drupal CRM, over JSON:API (contacts.py's "drupal" source).

docs/proposals/2026-10-17-contacts.md, step 3 and the timeline. The CRM
is reached at its own address — its public HTTPS one, not a tailnet listener
(David, 18 Oct 2026: "we're working towards not relying on tailnet for the
software") — signing in as a machine with Simple OAuth's
`client_credentials` grant. Nothing about the CRM is baked in: the
connection is `~/.config/agent-media/drupal-crm.json` (or
`MEDIA_DRUPAL_CRM`, a path or the JSON itself), 0600:

    {"url": "https://crm.example.com", "client_id": "…",
     "client_secret": "…", "scope": "agent_media_contacts"}

The access token is cached beside it (`drupal-crm-token.json`, 0600) until
a minute before it ends. A host without the file has no CRM and never asks.

* `read()`: every `crm_contact` person, with their emails, telephones and
  addresses (`crm_contact_method` entities), as cards the way contacts.py
  keeps them, plus a `crm` section: the person's other fields, which Google
  has no place for.
* `create_person()`, `add_methods()`: the seed's writes (contacts.seed).
* `get()`, `post()`, `patch()`: JSON:API for timeline.py's `crm_activity`.

The CRM's module is the contrib `crm` (Drupal 11): a person's phones and
addresses are contact-method entities referenced from the person, not
fields on it.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

JSONAPI = "application/vnd.api+json"
#: A page of JSON:API, the most Drupal answers by default.
PAGE = 50
#: A token this close to its end is asked for again.
EARLY_S = 60.0
#: Attributes of a person that are the card's own fields or Drupal's
#: bookkeeping: everything else is the CRM section.
_KNOWN = {"name", "full_name", "status", "created", "changed", "langcode", "uuid",
          "revision_timestamp", "revision_log", "revision_translation_affected",
          "default_langcode", "start_date", "end_date", "age", "relationship_statistics"}
_METHODS = {"emails": "email", "telephones": "telephone", "addresses": "address"}

_TOKEN_LOCK = threading.Lock()


class NotConfigured(Exception):
    """No CRM connection on this host."""


class CrmError(Exception):
    """The CRM refused or failed a request: said, with its status."""

    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


# --- where things are -----------------------------------------------------------

def config_path() -> Path:
    from .contacts import config_dir

    return config_dir() / "drupal-crm.json"


def token_path() -> Path:
    from .contacts import config_dir

    return config_dir() / "drupal-crm-token.json"


def settings() -> dict | None:
    """The connection, or None: `MEDIA_DRUPAL_CRM` (a path, or the JSON),
    else the file. Needs `url`, `client_id` and `client_secret`."""
    raw = (os.environ.get("MEDIA_DRUPAL_CRM") or "").strip()
    try:
        if raw.startswith("{"):
            got = json.loads(raw)
        else:
            p = Path(raw).expanduser() if raw else config_path()
            if not p.is_file():
                return None
            got = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(got, dict):
        return None
    url = str(got.get("url") or "").strip().rstrip("/")
    if not url or not got.get("client_id") or not got.get("client_secret"):
        return None
    return {"url": url, "client_id": str(got["client_id"]),
            "client_secret": str(got["client_secret"]), "scope": str(got.get("scope") or "")}


def configured() -> bool:
    return settings() is not None


# --- HTTP -----------------------------------------------------------------------

def _http(method: str, url: str, *, body: dict | None = None, form: dict | None = None,
          headers: dict | None = None, timeout: float = 20.0) -> tuple[int, dict]:
    """`(status, JSON body)`; an HTTP error is a status. The one seam the
    tests replace."""
    hdrs = {"Accept": JSONAPI, **(headers or {})}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        hdrs["Accept"] = "application/json"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = JSONAPI
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        out = {"raw": raw[:500].decode("utf-8", "replace")}
    return status, out if isinstance(out, dict) else {"value": out}


def _why(status: int, body: dict) -> str:
    errs = body.get("errors") or []
    if errs and isinstance(errs, list):
        e = errs[0] or {}
        return f"{status} {e.get('title') or ''}: {e.get('detail') or ''}".strip(": ")
    return f"{status} {body.get('error_description') or body.get('error') or body.get('message') or ''}".strip()


# --- the token ------------------------------------------------------------------

def _save_token(tok: dict) -> None:
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)
    os.chmod(p, 0o600)


def _cached() -> dict | None:
    try:
        tok = json.loads(token_path().read_text())
    except (OSError, ValueError):
        return None
    return tok if isinstance(tok, dict) and tok.get("access_token") else None


def token(fresh: bool = False) -> str:
    """An access token: the cached one while it lasts, else a new one from
    `POST /oauth/token` (client_credentials)."""
    cfg = settings()
    if cfg is None:
        raise NotConfigured(f"no CRM connection ({config_path()})")
    with _TOKEN_LOCK:
        tok = None if fresh else _cached()
        if tok and tok.get("url") == cfg["url"] and \
                float(tok.get("expires_at") or 0) - EARLY_S > time.time():
            return tok["access_token"]
        form = {"grant_type": "client_credentials", "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"]}
        if cfg["scope"]:
            form["scope"] = cfg["scope"]
        status, body = _http("POST", cfg["url"] + "/oauth/token", form=form)
        if status != 200 or not body.get("access_token"):
            raise CrmError(f"the CRM refused the sign-in ({_why(status, body)})", status)
        _save_token({"url": cfg["url"], "access_token": body["access_token"],
                     "expires_at": time.time() + float(body.get("expires_in") or 300)})
        return body["access_token"]


def _api(method: str, path: str, body: dict | None = None) -> dict:
    """One JSON:API request, signed in; a 401 asks for a new token once."""
    cfg = settings()
    if cfg is None:
        raise NotConfigured(f"no CRM connection ({config_path()})")
    url = path if path.startswith("http") else cfg["url"] + path
    for attempt in (0, 1):
        status, got = _http(method, url, body=body,
                            headers={"Authorization": f"Bearer {token(fresh=bool(attempt))}"})
        if status == 401 and not attempt:
            continue
        if status >= 400:
            raise CrmError(f"{method} {path}: {_why(status, got)}", status)
        return got
    raise CrmError(f"{method} {path}: 401", 401)  # pragma: no cover — the loop answers


def get(path: str) -> dict:
    return _api("GET", path)


def post(path: str, body: dict) -> dict:
    return _api("POST", path, body)


def patch(path: str, body: dict) -> dict:
    return _api("PATCH", path, body)


def pages(path: str):
    """Every resource of a collection, following `links.next`; yields
    `(data item, included by (type, id))` per page."""
    url = path
    while url:
        doc = get(url)
        inc = {(x.get("type"), x.get("id")): x for x in doc.get("included") or []}
        for item in doc.get("data") or []:
            yield item, inc
        nxt = ((doc.get("links") or {}).get("next") or {})
        url = nxt.get("href") if isinstance(nxt, dict) else nxt


def contact_url(uuid: str) -> str | None:
    """The person's page in the CRM, for the card's link."""
    cfg = settings()
    return f"{cfg['url']}/crm/contact/{uuid}" if cfg else None


# --- a CRM person into a card ---------------------------------------------------

def _detail(rel: dict) -> str:
    """A method's label (Home, Mobile, Work): its `detail` config entity."""
    d = ((rel.get("detail") or {}).get("data") or {})
    meta = d.get("meta") or {}
    key = meta.get("drupal_internal__target_id") or ""
    return str(key).replace("_", " ").capitalize() if key else ""


def _address_text(a: dict) -> str:
    if not isinstance(a, dict):
        return str(a or "")
    line = ", ".join(x for x in (a.get("address_line1"), a.get("address_line2")) if x)
    place = " ".join(x for x in (a.get("locality"), a.get("administrative_area"),
                                 a.get("postal_code")) if x)
    return "\n".join(x for x in (a.get("organization"), line, place,
                                 a.get("country_code") if a.get("country_code") not in
                                 (None, "", "AU") else "") if x)


def _plain(v):
    """A field's value as the card shows it: text, a number, or a list of
    them; a structured value (a name, a date range) flattened."""
    if isinstance(v, dict):
        if "value" in v and len(v) <= 3:
            return v.get("value")
        return ", ".join(str(x) for x in v.values() if x not in (None, "", [], {}))
    if isinstance(v, list):
        return [_plain(x) for x in v if x not in (None, "", [], {})]
    return v


def card_of(item: dict, included: dict, default_region: str = "AU") -> dict:
    """A `crm_contact` person as contacts.py keeps it, with the CRM section."""
    from .contacts import e164

    attrs = item.get("attributes") or {}
    rels = item.get("relationships") or {}
    full = attrs.get("full_name") or {}
    if isinstance(full, list):
        full = full[0] if full else {}
    given, family = (full.get("given") or ""), (full.get("family") or "")
    display = " ".join(x for x in (given, full.get("middle") or "", family) if x) \
        or (attrs.get("name") or "")
    names = [{"display": display, "given": given, "family": family}] if display else []
    phones, emails, addresses = [], [], []
    for field, bundle in _METHODS.items():
        data = (rels.get(field) or {}).get("data") or []
        for ref in data if isinstance(data, list) else [data]:
            m = included.get((ref.get("type"), ref.get("id")))
            if not m:
                continue
            a = m.get("attributes") or {}
            kind = _detail(m.get("relationships") or {})
            if bundle == "telephone":
                raw = a.get("telephone") or ""
                if raw:
                    phones.append({"value": e164(raw, default_region), "raw": raw, "type": kind})
            elif bundle == "email":
                if a.get("email"):
                    emails.append({"value": a["email"].strip(), "type": kind})
            else:
                text = _address_text(a.get("address"))
                if text:
                    addresses.append({"formatted": text, "type": kind})
    crm = {}
    for k, v in attrs.items():
        if k in _KNOWN or k.startswith(("drupal_internal__", "revision_")):
            continue
        v = _plain(v)
        if v in (None, "", [], {}):
            continue
        crm[k[6:] if k.startswith("field_") else k] = v
    name = display or next((e["value"] for e in emails), "") \
        or next((p["raw"] for p in phones), "") or "(no name)"
    return {"name": name, "names": names, "phones": phones, "emails": emails,
            "addresses": addresses, "organizations": [], "notes": "", "crm": crm}


def read(default_region: str = "AU") -> dict:
    """Every person in the CRM: `{uuid: {"card", "meta": {"changed"}}}`."""
    path = (f"/jsonapi/crm_contact/person?include={','.join(_METHODS)}"
            f"&page[limit]={PAGE}&sort=drupal_internal__id")
    out = {}
    for item, inc in pages(path):
        out[item["id"]] = {"card": card_of(item, inc, default_region),
                           "meta": {"changed": (item.get("attributes") or {}).get("changed") or ""}}
    return out


# --- writes (the seed) ----------------------------------------------------------

def create_person(card: dict) -> str:
    """A new person from a card: its name only; `add_methods` adds the rest.
    Answers the new uuid."""
    n = (card.get("names") or [{}])[0]
    attrs = {"name": card.get("name") or "(no name)"}
    if n.get("given") or n.get("family"):
        attrs["full_name"] = {"given": n.get("given") or "", "family": n.get("family") or ""}
    got = post("/jsonapi/crm_contact/person",
               {"data": {"type": "crm_contact--person", "attributes": attrs}})
    return got["data"]["id"]


def add_methods(uuid: str, card: dict) -> dict:
    """The card's phones and emails as the person's contact methods, then
    referenced from the person (the first is the primary). Addresses are
    left out: the card holds them as text, and the CRM's address field
    wants its parts."""
    made = {"telephones": [], "emails": []}
    owner = {"crm_contact": {"data": {"type": "crm_contact--person", "id": uuid}}}
    for p in card.get("phones") or []:
        value = p.get("value") or p.get("raw")
        if value:
            got = post("/jsonapi/crm_contact_method/telephone",
                       {"data": {"type": "crm_contact_method--telephone",
                                 "attributes": {"telephone": value}, "relationships": owner}})
            made["telephones"].append(got["data"]["id"])
    for e in card.get("emails") or []:
        if e.get("value"):
            got = post("/jsonapi/crm_contact_method/email",
                       {"data": {"type": "crm_contact_method--email",
                                 "attributes": {"email": e["value"]}, "relationships": owner}})
            made["emails"].append(got["data"]["id"])
    for field, ids in made.items():
        if ids:
            bundle = _METHODS[field]
            patch(f"/jsonapi/crm_contact/person/{uuid}/relationships/{field}",
                  {"data": [{"type": f"crm_contact_method--{bundle}", "id": i} for i in ids]})
    return made
