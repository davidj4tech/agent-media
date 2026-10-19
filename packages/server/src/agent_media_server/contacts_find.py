"""Find a person on a bridged network, one search per tap (contract §6.25).

David, 19 Oct 2026: a "Find on Messenger" action on a person's card, for
someone known without a Messenger handle (an imported Facebook friend, a
phone contact). Strictly his own act: one search per tap, never batched,
scheduled or run in the background, and at most one every `RATE_S` across
the server as a safety net — a bridge is an unofficial client, and a burst
of searches is what gets an account noticed.

* `POST /contacts/<id>/find {"network": "messenger"}` runs ONE user search
  through that network's bridge (bridgev2's provisioning `POST
  /v3/search_users {query}`, bridges.py) for the person's name, and answers
  up to `SHOW` candidates: `{id, name, handle, avatar}` — `handle` the
  ghost's Matrix id, `avatar` a small `data:` picture from the homeserver
  (never from the network's own pages). `pick` names a candidate only when
  exactly one has exactly the person's name: the app shows it chosen, and
  David still confirms it.
* `POST /contacts/<id>/find {"network", "confirm": <candidate id>}` records
  that candidate (one of the last search's, for that person, within
  `PENDING_TTL_S`) as theirs: a `found` row and a kept Join (contacts.py),
  so the card gains the handle — as a bridged person's join would — and the
  ghost's avatar becomes their picture (contacts_photos.py).
* `{"network", "none": true}` is "None of these": kept, the action goes.

The owner's book only: the bridges are the owner's.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time

#: The networks a card can be searched on: the bridge (its appservice id,
#: `MEDIA_CONTACTS_FIND_<NETWORK>_BRIDGE` names another) and the network ids
#: a handle of it may carry.
NETWORKS = {
    "messenger": {"bridge": "facebook", "network": "facebookgo", "name": "Messenger",
                  "handles": ("facebookgo", "facebook", "meta", "messenger")},
}
#: At most one search this often, across the server.
RATE_S = 10.0
#: Candidates answered, at most.
SHOW = 5
#: How long a search's candidates may be confirmed.
PENDING_TTL_S = 900.0
SEARCH_TIMEOUT_S = 30.0

_LOCK = threading.Lock()
_LAST = [0.0]
_PENDING: dict[tuple[str, str, str], dict] = {}


class TooSoon(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"one search every {int(RATE_S)} s")
        self.retry_after = retry_after


class Unavailable(Exception):
    """The network's bridge cannot search now (none, not signed in, down)."""


class NotOwner(Exception):
    """Another account's book: the bridges are the owner's."""


def _dismissed_source(network: str) -> str:
    return f"find:{network}"


def findable(card: dict, dismissed: set[str]) -> list[str]:
    """The networks this card may be searched on: none of its handles is
    one of the network's, and "None of these" was not said for it."""
    from . import contacts

    if not contacts.is_owner():
        return []
    have = {h.get("network") for h in card.get("handles") or []}
    have |= {s.get("network") for s in card.get("sources") or []}
    return [n for n, spec in NETWORKS.items()
            if not have & set(spec["handles"]) and n not in dismissed]


def dismissed_for(conn, cid: str) -> set[str]:
    out = set()
    for (source,) in conn.execute(
            "SELECT source FROM merges WHERE contact=? AND kind='dismiss' "
            "AND source LIKE 'find:%'", (cid,)):
        out.add(source.partition(":")[2])
    return out


def _bridge(network: str):
    from . import bridges

    spec = NETWORKS[network]
    bid = os.environ.get(f"MEDIA_CONTACTS_FIND_{network.upper()}_BRIDGE") or spec["bridge"]
    b = bridges.find(bid)
    if b is None:
        raise Unavailable(f"no {spec['name']} bridge here")
    return b


def _avatar(mxc: str) -> str | None:
    from . import contacts_photos

    if not (mxc or "").startswith("mxc://") or "/" not in mxc[6:]:
        return None
    try:
        got = contacts_photos.thumbnail(contacts_photos.fetch_mxc(mxc))
    except (OSError, ValueError):
        return None
    if not got:
        return None
    return f"data:{got[1]};base64,{base64.b64encode(got[0]).decode()}"


def search(cid: str, network: str, now: float | None = None) -> dict:
    """ONE search for the person on `network`: `{network, query,
    candidates, pick}`. KeyError (no such person), ValueError (a network not
    offered), TooSoon, Unavailable, NotOwner."""
    from . import bridges, contacts

    if network not in NETWORKS:
        raise ValueError(f"not a network a card is searched on: {network}")
    if not contacts.is_owner():
        raise NotOwner("the bridges are the owner's")
    card = contacts.card(cid)
    if card is None:
        raise KeyError(cid)
    query = (card.get("name") or "").strip()
    if not query or query == "(no name)":
        raise ValueError("this person has no name to search for")
    now = time.time() if now is None else now
    with _LOCK:
        wait = _LAST[0] + RATE_S - now
        if wait > 0:
            raise TooSoon(round(wait, 1))
        _LAST[0] = now
    b = _bridge(network)
    try:
        got = bridges._call(b, "POST", "/search_users", {"query": query},
                            timeout=SEARCH_TIMEOUT_S)
    except bridges.BridgeError as e:
        raise Unavailable(str(e) or "the bridge refused") from None
    except bridges.Unreachable as e:
        raise Unavailable(f"the bridge did not answer ({e})") from None
    results = [r for r in got.get("results") or [] if isinstance(r, dict) and r.get("id")]
    cands = []
    for r in results[:SHOW]:
        cands.append({"id": str(r["id"]), "name": str(r.get("name") or ""),
                      "handle": str(r.get("mxid") or ""),
                      "avatar_mxc": str(r.get("avatar_url") or "")})
    want = contacts.fold(query)
    exact = [c for c in cands if contacts.fold(c["name"]) == want]
    pick = exact[0]["id"] if len(exact) == 1 else None
    with _LOCK:
        for k in [k for k, v in _PENDING.items() if v["at"] + PENDING_TTL_S < now]:
            del _PENDING[k]
        _PENDING[(contacts.account(), cid, network)] = {
            "at": now, "by_id": {c["id"]: c for c in cands}}
    out = []
    for c in cands:
        out.append({"id": c["id"], "name": c["name"], "handle": c["handle"] or None,
                    "avatar": _avatar(c["avatar_mxc"])})
    return {"network": network, "query": query, "candidates": out, "pick": pick}


def confirm(cid: str, network: str, candidate: str, now: float | None = None) -> dict:
    """The candidate is this person: kept, with the handle and its picture.
    KeyError (no such person), LookupError (not one of the last search's)."""
    from . import contacts, contacts_photos

    if network not in NETWORKS:
        raise ValueError(f"not a network a card is searched on: {network}")
    if not contacts.is_owner():
        raise NotOwner("the bridges are the owner's")
    now = time.time() if now is None else now
    with _LOCK:
        p = _PENDING.get((contacts.account(), cid, network))
        c = (p or {}).get("by_id", {}).get(candidate) if p and p["at"] + PENDING_TTL_S >= now \
            else None
    if c is None:
        raise LookupError("not one of this person's last search: search again")
    spec = NETWORKS[network]
    sid = c["handle"] or f"{spec['network']}:{c['id']}"
    name = c["name"] or sid
    card = {"name": name, "names": [{"display": name, "given": "", "family": ""}],
            "phones": [], "emails": [], "addresses": [], "organizations": [], "notes": "",
            "handles": [{"network": spec["network"], "network_name": spec["name"], "id": sid}]}
    meta = {"network": spec["network"], "network_name": spec["name"], "remote_id": c["id"],
            "avatar": c["avatar_mxc"], "why": f"found on {spec['name']}"}
    with contacts._WRITE:
        conn = contacts._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM contacts WHERE id=?", (cid,)).fetchone() is None:
                raise KeyError(cid)
            conn.execute("INSERT OR REPLACE INTO found (sid, contact, network, card, meta, at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (sid, cid, network, json.dumps(card), json.dumps(meta), now))
            # The ghost met in a room later joins them too.
            conn.execute("DELETE FROM merges WHERE source=? AND kind='join'", (sid,))
            conn.execute("INSERT OR REPLACE INTO merges (source, contact, kind, at) "
                         "VALUES (?, ?, 'join', ?)", (sid, cid, now))
            conn.execute("DELETE FROM merges WHERE source=? AND contact=?",
                         (_dismissed_source(network), cid))
            contacts._rebuild(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    with _LOCK:
        _PENDING.pop((contacts.account(), cid, network), None)
    contacts._fresh()
    # Their picture now, not at the next pass (one fetch, the ghost's).
    try:
        contacts_photos.refresh(budget=1, only={("found", sid)})
    except Exception:  # noqa: BLE001 — the next pass takes it
        pass
    try:
        from . import contacts_writeback

        contacts_writeback.queue(cid)
    except Exception:  # noqa: BLE001 — a write-back is never the pick's failure
        pass
    return {"id": cid, "handle": sid}


def none_of_these(cid: str, network: str) -> dict:
    """"None of these": kept, and the card stops offering the search."""
    from . import contacts

    if network not in NETWORKS:
        raise ValueError(f"not a network a card is searched on: {network}")
    if not contacts.is_owner():
        raise NotOwner("the bridges are the owner's")
    with contacts._WRITE:
        conn = contacts._connect()
        try:
            if conn.execute("SELECT 1 FROM contacts WHERE id=?", (cid,)).fetchone() is None:
                raise KeyError(cid)
            conn.execute("INSERT OR REPLACE INTO merges (source, contact, kind, at) "
                         "VALUES (?, ?, 'dismiss', ?)",
                         (_dismissed_source(network), cid, time.time()))
        finally:
            conn.close()
    with _LOCK:
        _PENDING.pop((contacts.account(), cid, network), None)
    return {"id": cid}


def _reset_for_tests() -> None:
    with _LOCK:
        _LAST[0] = 0.0
        _PENDING.clear()
