"""Every "Same person?" in one place (David, 19 Oct 2026: a review screen
rather than card by card), and the nickname suggestions.

A suggestion is two contacts that may be one person, where one of the two
is known only from a bridge or an export (contacts.WEAK): only such a one
can be joined (`POST /contacts/<id>/same`) or let go (`/different`). Two
reasons make one:

* **The same name**, as before (contacts.suggestions).
* **A nickname of the same name with the same surname**: "Dave Smith" and
  "David Smith", "Liz Wong" and "Elizabeth Wong". `NICKNAMES` is a small
  table of common English short forms; each maps to the names it may stand
  for. Only ever a suggestion, never a join.

Never a suggestion: two contacts that both have numbers and share none (two
people of one name with phones of their own are two people), or a pair David
said were not the same.

`pairs()` is `GET /contacts/suggestions`: every open pair once, each side's
name, sources, labels and last contact (timeline.activity), and why.
"""

from __future__ import annotations

import json

#: Short form → the full names it may stand for (folded). Kept small and
#: common: a guess outside it is no better than chance.
NICKNAMES: dict[str, tuple[str, ...]] = {
    "abby": ("abigail",), "al": ("alan", "albert", "alexander", "alfred"),
    "alex": ("alexander", "alexandra", "alexis"), "andy": ("andrew",),
    "ant": ("anthony",), "barb": ("barbara",), "ben": ("benjamin",),
    "beth": ("elizabeth", "bethany"), "betty": ("elizabeth",), "bill": ("william",),
    "billy": ("william",), "bob": ("robert",), "bobby": ("robert",),
    "cath": ("catherine", "cathy"), "cathy": ("catherine",), "charlie": ("charles", "charlotte"),
    "chris": ("christopher", "christine", "christina", "christian"),
    "dan": ("daniel",), "danny": ("daniel",), "dave": ("david",), "davey": ("david",),
    "deb": ("deborah",), "debbie": ("deborah",), "dick": ("richard",),
    "don": ("donald",), "ed": ("edward", "edwin"), "eddie": ("edward",),
    "fred": ("frederick",), "gerry": ("gerald",), "greg": ("gregory",),
    "harry": ("henry", "harold"), "jack": ("john",), "jake": ("jacob",),
    "jen": ("jennifer",), "jenny": ("jennifer",), "jim": ("james",), "jimmy": ("james",),
    "jo": ("joanne", "josephine", "joanna"), "joe": ("joseph",), "johnny": ("john",),
    "jon": ("jonathan",), "kate": ("katherine", "catherine", "kathryn"),
    "kathy": ("katherine", "kathryn"), "katie": ("katherine", "kathryn"),
    "ken": ("kenneth",), "kim": ("kimberley", "kimberly"), "larry": ("lawrence",),
    "liz": ("elizabeth",), "lizzie": ("elizabeth",), "matt": ("matthew",),
    "meg": ("margaret", "megan"), "mick": ("michael",), "mike": ("michael",),
    "mikey": ("michael",), "nat": ("natalie", "nathan", "nathaniel"),
    "nick": ("nicholas",), "nicky": ("nicholas", "nicola"), "pat": ("patrick", "patricia"),
    "patty": ("patricia",), "peggy": ("margaret",), "pete": ("peter",), "phil": ("phillip", "philip"),
    "ray": ("raymond",), "rick": ("richard",), "rob": ("robert",), "robbie": ("robert",),
    "ron": ("ronald",), "sam": ("samuel", "samantha"), "sandy": ("sandra", "alexandra"),
    "steph": ("stephanie",), "steve": ("steven", "stephen"), "sue": ("susan", "suzanne"),
    "susie": ("susan",), "ted": ("edward", "theodore"), "tim": ("timothy",),
    "tom": ("thomas",), "tommy": ("thomas",), "tony": ("anthony",), "trish": ("patricia",),
    "vic": ("victor", "victoria"), "vicky": ("victoria",), "will": ("william",),
}


def _first_last(name: str) -> tuple[str, str] | None:
    """A folded name's first and last word; None for a one-word name (a
    nickname alone, with no surname to share, suggests nobody)."""
    words = name.split()
    if len(words) < 2:
        return None
    return words[0], words[-1]


def nick_keys(name: str) -> set[str]:
    """The `full-first surname` keys a folded name may stand for: its own,
    and, for a short form in `NICKNAMES`, each full name's."""
    fl = _first_last(name)
    if not fl:
        return set()
    first, last = fl
    return {f"{first} {last}"} | {f"{full} {last}" for full in NICKNAMES.get(first, ())}


_CACHE: dict = {}


def _derived(idx: dict) -> dict:
    """The nickname index and each contact's numbers, kept while the
    matcher's generation holds."""
    key = (id(idx), idx.get("gen"))
    got = _CACHE.get("v")
    if got and got[0] == key:
        return got[1]
    nicks: dict[str, set] = {}
    for cid, names in idx.get("card_names", {}).items():
        for n in names:
            if n in PLACEHOLDER_NAMES:
                continue
            for k in nick_keys(n):
                nicks.setdefault(k, set()).add(cid)
    phones: dict[str, set] = {}
    for num, cids in idx.get("phones", {}).items():
        for cid in cids:
            phones.setdefault(cid, set()).add(num)
    out = {"nicks": nicks, "phones": phones}
    _CACHE["v"] = (key, out)
    return out


#: Names a network gives an account it no longer shows (deleted,
#: deactivated, blocked): hundreds of different people share each one, so
#: they never suggest anyone. Folded, as the matcher's names are.
PLACEHOLDER_NAMES = frozenset({
    "facebook user", "facebook member", "messenger user", "instagram user",
    "instagram member", "whatsapp user", "deleted user", "deleted account",
    "unknown", "unknown user", "user",
})


def candidates(idx: dict, cid: str) -> list[tuple[str, str]]:
    """`[(other, why)]` for contact `cid`, by the matcher's index
    (contacts._index): "same name" first, then "nickname". A placeholder
    name (`PLACEHOLDER_NAMES`) suggests no one."""
    d = _derived(idx)
    names = {n for n in idx["card_names"].get(cid, set()) if n not in PLACEHOLDER_NAMES}
    same = {o for n in names for o in idx["names"].get(n, ())}
    nick = {o for n in names for k in nick_keys(n) for o in d["nicks"].get(k, ())}
    out = []
    mine = d["phones"].get(cid, set())
    for other in sorted((same | nick) - {cid}):
        if cid not in idx["bridge_only"] and other not in idx["bridge_only"]:
            continue
        if (cid, other) in idx["dismissed"] or (other, cid) in idx["dismissed"]:
            continue
        theirs = d["phones"].get(other, set())
        if mine and theirs and not (mine & theirs):
            continue                    # numbers of their own: two people
        out.append((other, "same name" if other in same else "nickname"))
    return out


def _sources(card: dict) -> list[str]:
    """Where a card comes from, said short: Phone, Google, Messenger…"""
    out = []
    for s in card.get("sources") or []:
        k = s.get("kind")
        if k == "android":
            got = "Phone"
        elif k == "google":
            got = "Google"
        elif k == "drupal":
            got = "CRM"
        elif k == "bridges":
            got = s.get("network_name") or s.get("network") or "Bridge"
        elif k == "import":
            got = {"facebook": "Facebook export", "instagram": "Instagram export"}.get(
                s.get("export") or "", "Export")
        else:
            got = str(k or "")
        if got and got not in out:
            out.append(got)
    return out


def pairs() -> dict:
    """`GET /contacts/suggestions`: `{suggestions: [{a, b, why}], count}`,
    each pair once (the side known only from a bridge or an export first),
    each side `{id, name, sources, labels, last}`."""
    from . import contacts, timeline

    idx = contacts._index()
    if idx is None:
        return {"suggestions": [], "count": 0}
    seen: set = set()
    found: list[tuple[str, str, str]] = []
    for cid in sorted(idx["bridge_only"]):
        for other, why in candidates(idx, cid):
            key = frozenset((cid, other))
            if key in seen:
                continue
            seen.add(key)
            found.append((cid, other, why))
    if not found:
        return {"suggestions": [], "count": 0}
    want = {c for a, b, _ in found for c in (a, b)}
    cards: dict[str, dict] = {}
    conn = contacts._connect(create=False)
    if conn is not None:
        try:
            ids = sorted(want)
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                for cid, raw in conn.execute(
                        f"SELECT id, card FROM contacts WHERE id IN ({','.join('?' * len(part))})",
                        part):
                    cards[cid] = json.loads(raw)
        finally:
            conn.close()
    try:
        act = timeline.activity()
    except Exception:  # noqa: BLE001 — the last contact is a nicety
        act = {}

    def side(cid: str) -> dict:
        c = cards.get(cid) or {}
        return {"id": cid, "name": idx["display"].get(cid) or "",
                "sources": _sources(c), "labels": c.get("labels") or [],
                "last": (act.get(cid) or {}).get("last")}

    out = [{"a": side(a), "b": side(b), "why": why} for a, b, why in found
           if a in cards and b in cards]
    # Same names before nicknames; within each, whoever was talked to last.
    out.sort(key=lambda p: (p["why"] != "same name",
                            -max(int((p[s]["last"] or {}).get("day", "0").replace("-", "") or 0)
                                 for s in ("a", "b")),
                            contacts.fold(p["a"]["name"])))
    return {"suggestions": out, "count": len(out)}
