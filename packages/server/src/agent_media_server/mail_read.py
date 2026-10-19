"""One email read inside Sasonica (contract §6.25 "Reading one", step 1 of
docs/proposals/2026-10-20-mail-in-sasonica.md).

`GET /mail/message?id=<notmuch message id>` answers one message from this
host's notmuch: its headers, a body safe to show and its attachments;
`GET /mail/attachment?id=&part=` one attachment's bytes; `POST /mail/say`
reads it aloud, only when David asks (a Play button, never by itself).

The owner's book only: it is this host's mail. Nothing is kept: the file is
read and dropped, and no subject, address or body is logged or written.

The body: the text/plain part when there is one (David, 20 Oct 2026); else
the HTML, made safe here with an allow-list (`sanitise`): no script, style,
form or frame, no attribute but a few (href to http/https/mailto/tel, an
image's src, alt and size, a cell's span), and no remote resource. A
picture from the web is a placeholder unless `images=1` asks for them for
this one message ("Show images"); one carried in the mail itself (cid:) is
put in as data, then, since fetching it tells nobody anything.
"""

from __future__ import annotations

import base64
import email.parser
import email.policy
import email.utils
import html
import json
import re
from html.parser import HTMLParser

#: The most of a body sent: a long newsletter is cut, not refused.
BODY_MAX = 512 * 1024
#: An attachment's most, and a cid: picture's (each, then all of them).
ATTACH_MAX = 25 * 1024 * 1024
CID_MAX, CID_TOTAL = 2 * 1024 * 1024, 8 * 1024 * 1024
#: What `POST /mail/say` reads at most, as `/org/say`.
SAY_MAX = 6000

_ID_OK = re.compile(r"[^\s\x00-\x1f\x7f]{1,998}")


class NotFound(Exception):
    pass


def valid_id(mid: str) -> bool:
    """A notmuch message id as notmuch prints it: no space, no control
    character, at most a header line long."""
    return bool(mid) and bool(_ID_OK.fullmatch(mid))


def _query(mid: str) -> str:
    # notmuch's query language: a quoted term, a quote inside doubled.
    return 'id:"' + mid.replace('"', '""') + '"'


def _file(mid: str) -> str:
    """The message's first readable file. NotFound when notmuch knows none."""
    from . import timeline

    if not valid_id(mid):
        raise NotFound("no such message")
    got = timeline._notmuch(["search", "--format=json", "--output=files", _query(mid)], 60)
    if got is None:
        raise RuntimeError("notmuch did not answer")
    try:
        files = [str(x) for x in json.loads(got or "[]") if x]
    except ValueError:
        raise RuntimeError("notmuch answered something else") from None
    for p in files:
        try:
            with open(p, "rb"):
                return p
        except OSError:
            continue
    raise NotFound("no such message")


def _parse(mid: str) -> email.message.EmailMessage:
    with open(_file(mid), "rb") as f:
        return email.parser.BytesParser(policy=email.policy.default).parse(f)


def _leaves(msg) -> list:
    """Every non-multipart part, in walk order: a part's number is its
    index here, the same in `message` and `attachment`."""
    return [p for p in msg.walk() if not p.is_multipart()]


def _is_attachment(p) -> bool:
    if p.is_attachment():
        return True
    if p.get_content_maintype() in ("text", "multipart") and not p.get_filename():
        return False
    return bool(p.get_filename()) and p.get_content_maintype() != "text"


def _text(p) -> str:
    try:
        return str(p.get_content())
    except (LookupError, ValueError, AssertionError):
        raw = p.get_payload(decode=True) or b""
        return raw.decode("utf-8", errors="replace")


def _safe_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", name or "").strip(" .")
    return name[:180] or "attachment"


def _addrs(value) -> list[dict]:
    out = []
    for name, addr in email.utils.getaddresses([str(value or "")]):
        if addr or name:
            out.append({"name": name.strip(), "address": addr.strip()})
    return out


# ── Making HTML safe ──────────────────────────────────────────────────────

#: Kept, with nothing but the attributes below.
_TAGS = {"p", "br", "div", "span", "b", "strong", "i", "em", "u", "s", "small",
         "sub", "sup", "a", "ul", "ol", "li", "blockquote", "pre", "code", "hr",
         "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tfoot",
         "tr", "td", "th", "caption", "img", "dl", "dt", "dd", "center", "font"}
#: Gone with everything inside them.
_DROP = {"script", "style", "head", "title", "noscript", "template", "iframe",
         "frame", "frameset", "object", "embed", "applet", "svg", "math", "select",
         "textarea", "button", "form", "audio", "video", "canvas", "link", "meta",
         "base", "input"}
_VOID = {"br", "hr", "img", "wbr", "input", "meta", "link", "base"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
          "blockquote", "pre", "hr", "table", "dt", "dd"}
_LINK_OK = ("http://", "https://", "mailto:", "tel:")


def _scheme_ok(url: str, allowed: tuple) -> bool:
    u = re.sub(r"[\x00-\x20\x7f]+", "", url or "").lower()
    return u.startswith(allowed)


class _Clean(HTMLParser):
    def __init__(self, images: bool, cids: dict[str, str]):
        super().__init__(convert_charrefs=True)
        self.images, self.cids = images, cids
        self.out: list[str] = []
        self.text: list[str] = []
        self.open: list[str] = []
        self.dropping = 0
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _DROP:
            if tag not in _VOID:
                self.dropping += 1
            return
        if self.dropping or tag not in _TAGS:
            if tag in _BLOCK and not self.dropping:
                self.text.append("\n")
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        keep: list[tuple[str, str]] = []
        if tag == "a":
            if _scheme_ok(a.get("href", ""), _LINK_OK):
                keep += [("href", a["href"].strip()), ("rel", "noopener noreferrer"),
                         ("target", "_blank")]
        elif tag == "img":
            src = a.get("src", "").strip()
            alt = a.get("alt", "")
            if src.lower().startswith("cid:") and self.images and src[4:].strip("<> ") in self.cids:
                keep.append(("src", self.cids[src[4:].strip("<> ")]))
            elif self.images and _scheme_ok(src, ("https://",)):
                keep.append(("src", src))
            else:
                # A picture not shown: a placeholder with its words, if any.
                self.blocked += 1 if src else 0
                label = alt.strip() or "image"
                self.out.append(f'<span class="mail-image-off">[{html.escape(label[:80])}]</span>')
                return
            if alt:
                keep.append(("alt", alt[:200]))
            for k in ("width", "height"):
                if a.get(k, "").isdigit() and int(a[k]) <= 4000:
                    keep.append((k, a[k]))
        elif tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if a.get(k, "").isdigit() and int(a[k]) <= 100:
                    keep.append((k, a[k]))
        if tag == "font" or tag == "center":
            tag = "span" if tag == "font" else "div"
        attrs_s = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in keep)
        self.out.append(f"<{tag}{attrs_s}>")
        if tag in _BLOCK:
            self.text.append("\n")
        if tag not in _VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        t = tag.lower()
        if t not in _VOID and self.open and self.open[-1] == ("span" if t == "font" else "div" if t == "center" else t):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _DROP:
            if tag not in _VOID and self.dropping:
                self.dropping -= 1
            return
        if self.dropping:
            return
        tag = "span" if tag == "font" else "div" if tag == "center" else tag
        if tag in _BLOCK:
            self.text.append("\n")
        if tag not in self.open:
            return                                  # a stray end: nothing opened it
        while self.open:
            t = self.open.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if self.dropping:
            return
        self.out.append(html.escape(data, quote=False))
        self.text.append(data)

    def result(self) -> str:
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        return "".join(self.out)


def sanitise(raw: str, *, images: bool = False, cids: dict[str, str] | None = None) -> tuple[str, int]:
    """`(safe_html, blocked)`: `raw` with only the allow-listed tags and
    attributes, scripts and styles gone with their contents, links only to
    http(s)/mailto/tel, and pictures from the web placeholders unless
    `images` (`blocked` counts those left out)."""
    p = _Clean(images, cids or {})
    p.feed(raw or "")
    p.close()
    return p.result(), p.blocked


def html_text(raw: str) -> str:
    """The words of an HTML body, a line per block, for reading aloud."""
    p = _Clean(False, {})
    p.feed(raw or "")
    p.close()
    t = "".join(p.text)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()


# ── The routes' work ──────────────────────────────────────────────────────

def _bodies(msg) -> tuple[str | None, str | None]:
    plain = msg.get_body(preferencelist=("plain",))
    rich = msg.get_body(preferencelist=("html",))
    return (_text(plain) if plain is not None else None,
            _text(rich) if rich is not None else None)


def _cid_map(msg, leaves: list) -> dict[str, str]:
    """Pictures carried in the mail (Content-ID), as data: URLs, within the
    caps."""
    out, total = {}, 0
    for p in leaves:
        cid = str(p.get("Content-ID") or "").strip().strip("<>")
        if not cid or p.get_content_maintype() != "image":
            continue
        data = p.get_payload(decode=True) or b""
        if len(data) > CID_MAX or total + len(data) > CID_TOTAL:
            continue
        total += len(data)
        ctype = re.sub(r"[^a-z0-9.+/-]", "", p.get_content_type().lower())
        out[cid] = f"data:{ctype};base64,{base64.b64encode(data).decode()}"
    return out


def message(mid: str, *, images: bool = False, fmt: str = "") -> dict:
    """`GET /mail/message`'s answer: the plain part when there is one, the
    HTML when there is no plain part or `fmt` is "html" and there is one
    (`formats` says which the message has). NotFound, RuntimeError
    (notmuch)."""
    from . import contacts_mail, timeline

    msg = _parse(mid)
    leaves = _leaves(msg)
    plain, rich = _bodies(msg)
    out: dict = {"id": mid,
                 "from": _addrs(msg.get("From")), "to": _addrs(msg.get("To")),
                 "cc": _addrs(msg.get("Cc")),
                 "subject": str(msg.get("Subject") or "").strip(),
                 "date": None, "text": None, "html": None, "images_blocked": 0,
                 "truncated": False,
                 "gmail_url": contacts_mail.message_url(mid)}
    try:
        d = email.utils.parsedate_to_datetime(str(msg.get("Date") or ""))
        out["date"] = int(d.timestamp()) if d else None
    except (TypeError, ValueError):
        pass
    mine = timeline.mail_self()
    out["mine"] = any(a["address"].lower() in mine for a in out["from"])
    has_plain = plain is not None and bool(plain.strip())
    out["formats"] = [f for f, ok in (("text", has_plain), ("html", rich is not None)) if ok]
    if has_plain and not (fmt == "html" and rich is not None):
        out["text"] = plain[:BODY_MAX]
        out["truncated"] = len(plain) > BODY_MAX
    elif rich is not None:
        safe, blocked = sanitise(rich, images=images,
                                 cids=_cid_map(msg, leaves) if images else None)
        out["html"] = safe[:BODY_MAX]
        out["truncated"] = len(safe) > BODY_MAX
        out["images_blocked"] = blocked
    out["attachments"] = [
        {"part": i, "name": _safe_name(p.get_filename() or ""),
         "type": p.get_content_type(),
         "size": len(p.get_payload(decode=True) or b"")}
        for i, p in enumerate(leaves) if _is_attachment(p)]
    return out


def attachment(mid: str, part: int) -> tuple[bytes, str, str]:
    """`(data, content_type, filename)` for `GET /mail/attachment`.
    NotFound for a part that is not an attachment of this message."""
    msg = _parse(mid)
    leaves = _leaves(msg)
    if not (0 <= part < len(leaves)) or not _is_attachment(leaves[part]):
        raise NotFound("no such attachment")
    p = leaves[part]
    data = p.get_payload(decode=True) or b""
    if len(data) > ATTACH_MAX:
        raise NotFound("too large to send")
    ctype = re.sub(r"[^a-z0-9.+/-]", "", p.get_content_type().lower()) or "application/octet-stream"
    return data, ctype, _safe_name(p.get_filename() or "")


def spoken(mid: str) -> str:
    """What `POST /mail/say` reads: who it is from, its subject, then its
    words (the plain part, else the HTML's), capped."""
    msg = _parse(mid)
    plain, rich = _bodies(msg)
    who = _addrs(msg.get("From"))
    name = (who[0]["name"] or who[0]["address"].partition("@")[0]) if who else ""
    body = plain if plain and plain.strip() else html_text(rich or "")
    # Quoted replies ("> …") are the earlier mail, not this one.
    body = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith(">"))
    subject = str(msg.get("Subject") or "").strip()
    head = ". ".join(x for x in (f"From {name}" if name else "", subject) if x)
    text = (head + ".\n\n" + body.strip()).strip() if head else body.strip()
    return text[:SAY_MAX]
