"""A person's recent mail, read live from this host's notmuch index
(contract §6.25 "Their emails").

The Gmail app ignores search links (an ACTION_SEARCH is dropped, a
mail.google.com/#search/… link opens the inbox: tried on David's phone,
20 Oct 2026), so the card lists the newest mail to and from the person
itself, each with a link to that one message, beside the browser search
(contacts.mail_link) and a mailto: to write to them.

The owner's book only: it is this host's mail. Nothing is kept: notmuch's
answer is read in memory and dropped, no subject or address is logged or
written anywhere, and a subject leaves the server only in answer to this
route. The same rule as the timeline's email (timeline.py): never a list's
or a machine's mail, nor spam, deleted mail or the Chats label (Google
Talk, which the timeline counts as chat).
"""

from __future__ import annotations

import email.utils
import json
import urllib.parse

#: The most a request may ask for.
LIMIT_MAX = 20

#: Messages asked of notmuch per one wanted: room for the lists and machine
#: mail that are then left out.
OVERSCAN = 3


def available() -> bool:
    """Whether the card can list recent mail: the owner's book, notmuch with
    a database, and the email reading not switched off
    (`MEDIA_TIMELINE_MAIL=off` or `MEDIA_CONTACTS_MAIL_RECENT=0`)."""
    from . import contacts, timeline

    if not contacts.is_owner():
        return False
    if (contacts.owner_env("MEDIA_CONTACTS_MAIL_RECENT") or "").strip() == "0":
        return False
    if (contacts.owner_env("MEDIA_TIMELINE_MAIL") or "").strip().lower() in ("off", "imap"):
        return False
    return timeline._notmuch_ok()


def addresses(c: dict) -> list[str]:
    """The card's addresses, then its old chat addresses (raw and decoded),
    each once, lower case."""
    out: list[str] = []
    for e in c.get("emails") or []:
        a = (e.get("value") if isinstance(e, dict) else e) or ""
        out.append(str(a))
    for x in c.get("old_addresses") or []:
        out += [str(x.get("address") or ""), str(x.get("real") or "")]
    seen, got = set(), []
    for a in out:
        a = a.strip().lower()
        if "@" in a and a not in seen and not any(ch in a for ch in "()\"\\ "):
            seen.add(a)
            got.append(a)
    return got


def mailto(c: dict) -> str | None:
    """`mailto:` the card's first email, for "Email them"."""
    for e in c.get("emails") or []:
        a = str((e.get("value") if isinstance(e, dict) else e) or "").strip()
        if "@" in a:
            return "mailto:" + urllib.parse.quote(a, safe="@.+-_")
    return None


def message_url(message_id: str) -> str | None:
    """The book's mail template (contacts.MAIL_URL, `MEDIA_CONTACTS_MAIL_URL`)
    searching for this one message by its Message-ID."""
    from . import contacts

    tpl = (contacts.setting("MEDIA_CONTACTS_MAIL_URL", "mail_url") or "").strip() or contacts.MAIL_URL
    if tpl.lower() in ("off", "0", "none", "no") or "{query}" not in tpl or not message_id:
        return None
    return tpl.replace("{query}", urllib.parse.quote(f"rfc822msgid:{message_id}", safe=""))


def _query(addrs: list[str]) -> str:
    return " or ".join(f"from:{a} or to:{a} or cc:{a}" for a in addrs)


def recent(c: dict, limit: int = 5) -> list[dict]:
    """The newest mail to and from the person on card `c`, at most `limit`:
    `[{id, date, from, mine, subject, gmail_url}]`, newest first. Raises
    RuntimeError when notmuch does not answer."""
    from . import timeline

    addrs = addresses(c)
    if not addrs:
        return []
    limit = max(1, min(LIMIT_MAX, int(limit or 5)))
    got = timeline._notmuch(["search", "--format=json", "--output=messages",
                             "--sort=newest-first", f"--limit={limit * OVERSCAN}",
                             f"({_query(addrs)}) and not tag:spam and not tag:deleted"], 60)
    if got is None:
        raise RuntimeError("notmuch did not answer")
    try:
        ids = [str(x) for x in json.loads(got or "[]") if x]
    except ValueError:
        raise RuntimeError("notmuch answered something else") from None
    if not ids:
        return []
    msgs = timeline.notmuch_show(" or ".join(f"id:{i}" for i in ids), 60)
    mine = timeline.mail_self()
    out = []
    for m in sorted(msgs, key=lambda m: -float(m.get("timestamp") or 0)):
        if timeline._chat(m):
            continue
        h = m.get("headers") or {}
        name, frm = email.utils.parseaddr(str(h.get("From") or ""))
        frm = timeline._addr(frm)
        if timeline._BULK_LOCAL.match(frm.partition("@")[0]):
            continue
        head = timeline._header_block(m.get("filename") or [])
        if head is not None and timeline._bulk(head):
            continue
        out.append({"id": str(m.get("id") or ""),   # GET /mail/message (mail_read.py)
                    "date": int(float(m.get("timestamp") or 0)),
                    "from": name.strip() or frm.partition("@")[0],
                    "mine": frm in mine,
                    "subject": str(h.get("Subject") or "").strip(),
                    "gmail_url": message_url(str(m.get("id") or ""))})
        if len(out) >= limit:
            break
    return out
