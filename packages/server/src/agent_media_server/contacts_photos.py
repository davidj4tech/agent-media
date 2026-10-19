"""People's pictures: a small thumbnail per person, kept per book.

David, 19 Oct 2026: "does it get profile pictures as well?" … "I'd really
like to get Facebook profile photos too". Two sources, in this order:

1. **Google** — the People API's `photos` (`PERSON_FIELDS` asks for them),
   for a book signed in to Google; a default one (Google's coloured letter)
   is no picture. Every contact synced to the phone from Google is here, so
   this is also the phone's pictures: the phone itself cannot give them —
   its contacts provider keeps a thumbnail as a database blob, `content
   query` prints it only as `BLOB` and `content read` has no file for it
   ("No files supported by provider"), so adb never sees the bytes.
2. **The bridges** — a ghost's own avatar on the homeserver (`avatar_url`
   from its profile, an `mxc://`), fetched as a 256 px thumbnail through the
   homeserver's authenticated media. Messenger, Instagram, WhatsApp, Teams
   and SMS ghosts alike, one-to-one and group: this is where Facebook
   profile pictures come from, as the bridge already has them — nothing is
   ever fetched from Facebook's own pages. The owner's book only (the
   bridges are the owner's).

**What is kept.** `photo_src` in the book's `contacts.db` (kept through a
schema bump, as a cache that is costly to fill): per source record `(kind,
sid)` its `ref` (the Google URL, the ghost's mxc), the file it gave (a
content hash) and when to look again. The picture itself is
`<book state>/photos/<sha256>.jpg` (0600, the folder 0700): a JPEG at most
`SIZE` px a side when Pillow is there, else the bytes as the source sent
them (an image, at most `MAX_BYTES`). Nothing else of the source is kept.

**When.** `refresh()` runs after each sync of the book (contacts.py's
loop): a Google record whose URL changed, or a ghost not looked at for
`RECHECK_S`, is fetched, at most `BUDGET` per pass; a failure waits longer
each time (`_backoff`), never more than a week. A card's picture is its
first source, in `KIND_ORDER`, that has one.

**Served** as `GET /contacts/<id>/photo` (app.py, §6.25): the caller's book
only, an ETag (the hash) and Last-Modified, 404 when there is none; every
list row and card carries `photo: {url, etag}` when there is one.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("agent-media.server.contacts_photos")

#: Which source's picture a card shows, best first.
KIND_ORDER = ("google", "bridges", "found")
#: The longest side kept, in px.
SIZE = 256
#: The largest picture taken from a source.
MAX_BYTES = 2 * 1024 * 1024
#: Fetches per pass, all sources together.
BUDGET = 60
#: How often a ghost's avatar is looked at again (its mxc may change).
RECHECK_S = 7 * 86400.0
#: A failed fetch waits `_backoff(tries)`, at most this.
BACKOFF_MAX_S = 7 * 86400.0
TIMEOUT_S = 15.0

_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"),
          (b"GIF8", "image/gif"))


def photos_dir() -> Path:
    from .contacts import account_state_dir

    return account_state_dir() / "photos"


def _backoff(tries: int) -> float:
    return min(900.0 * (2 ** max(0, tries - 1)), BACKOFF_MAX_S)


def sniff(data: bytes) -> str | None:
    """The image type `data` is, or None: JPEG, PNG, GIF, WebP."""
    for magic, ctype in _MAGIC:
        if data.startswith(magic):
            return ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def thumbnail(data: bytes) -> tuple[bytes, str] | None:
    """`(bytes, type)` as kept: a JPEG at most `SIZE` px a side when Pillow
    can read it, else the source's own bytes when they are an image of at
    most `MAX_BYTES`; None when it is not an image."""
    ctype = sniff(data)
    if ctype is None or len(data) > MAX_BYTES:
        return None
    try:
        from PIL import Image
    except ImportError:
        return data, ctype
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
            im = bg
        im.thumbnail((SIZE, SIZE))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 — an image Pillow cannot read: as sent
        return data, ctype


def _keep(data: bytes) -> tuple[str, str] | None:
    """Thumbnail `data` and write it once, by its hash: `(file, type)`."""
    got = thumbnail(data)
    if got is None:
        return None
    out, ctype = got
    name = hashlib.sha256(out).hexdigest()
    d = photos_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    path = d / name
    if not path.exists():
        tmp = d / f".{name}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(out)
        os.replace(tmp, path)
    return name, ctype


def _http(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("too large")
    return data


# --- the sources ----------------------------------------------------------------

def google_url(person: dict) -> str:
    """A People API person's picture URL, sized to `SIZE`; "" for none or a
    default one (Google's letter on a colour)."""
    best = ""
    for p in person.get("photos") or []:
        if p.get("default") or not p.get("url"):
            continue
        if (p.get("metadata") or {}).get("primary") or not best:
            best = p["url"]
    if not best:
        return ""
    # Google's photo URLs end in a size option (`=s100`, `=s100-p`): ask
    # for ours instead.
    return re.sub(r"=s\d+(-[a-z0-9-]+)?$", "", best) + f"=s{SIZE}"


def fetch_google(url: str) -> bytes:
    return _http(url)


def _homeserver() -> str:
    return (os.environ.get("MATRIX_HOMESERVER") or "").rstrip("/")


def _token() -> str:
    return os.environ.get("MATRIX_OWNER_TOKEN") or os.environ.get("MATRIX_ACCESS_TOKEN") or ""


def ghost_avatar(user_id: str) -> str:
    """A ghost's `avatar_url` (an mxc) from its profile, "" for none."""
    hs = _homeserver()
    if not hs:
        raise OSError("no MATRIX_HOMESERVER")
    tok = _token()
    url = f"{hs}/_matrix/client/v3/profile/{urllib.parse.quote(user_id, safe='')}/avatar_url"
    try:
        body = json.loads(_http(url, {"Authorization": f"Bearer {tok}"} if tok else {}))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""
        raise
    mxc = body.get("avatar_url") or "" if isinstance(body, dict) else ""
    return mxc if mxc.startswith("mxc://") and "/" in mxc[6:] else ""


def fetch_mxc(mxc: str) -> bytes:
    """An mxc's picture: a thumbnail through authenticated media, else the
    whole of it through the old media API."""
    hs = _homeserver()
    if not hs:
        raise OSError("no MATRIX_HOMESERVER")
    server, media = mxc[6:].split("/", 1)
    tail = f"{urllib.parse.quote(server, safe='')}/{urllib.parse.quote(media, safe='')}"
    tok = _token()
    size = f"width={SIZE}&height={SIZE}&method=scale"
    tries = []
    if tok:
        tries += [(f"{hs}/_matrix/client/v1/media/thumbnail/{tail}?{size}", tok),
                  (f"{hs}/_matrix/client/v1/media/download/{tail}", tok)]
    tries.append((f"{hs}/_matrix/media/v3/thumbnail/{tail}?{size}", ""))
    last: Exception = OSError("no way to fetch it")
    for url, t in tries:
        try:
            return _http(url, {"Authorization": f"Bearer {t}"} if t else {})
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
    raise last


# --- the store ------------------------------------------------------------------

def _rows(conn) -> dict[tuple[str, str], dict]:
    return {(k, s): {"ref": r, "file": f, "ctype": c, "at": a, "tries": t, "next_at": n}
            for k, s, r, f, c, a, t, n in conn.execute(
                "SELECT kind, sid, ref, file, ctype, at, tries, next_at FROM photo_src")}


def _put(conn, kind: str, sid: str, ref: str, file: str, ctype: str, at: float,
         tries: int, next_at: float) -> None:
    conn.execute("INSERT OR REPLACE INTO photo_src (kind, sid, ref, file, ctype, at, tries, "
                 "next_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (kind, sid, ref, file, ctype, at, tries, next_at))


def _wanted(conn, owner: bool) -> list[tuple[str, str, str]]:
    """`(kind, sid, ref)` of every source record that may have a picture: a
    Google record's URL (from its meta) as it is now, a ghost's ref the one
    last seen (it is read again when due)."""
    out = []
    for kind, sid, meta in conn.execute(
            "SELECT kind, sid, meta FROM records WHERE kind IN ('google', 'bridges')"):
        if kind == "bridges" and not owner:
            continue
        m = json.loads(meta or "{}")
        out.append((kind, sid, (m.get("photo") or "") if kind == "google" else ""))
    if owner:
        # Who David picked from a search (contacts_find.py): a ghost too.
        out += [("found", sid, "") for (sid,) in conn.execute("SELECT sid FROM found")
                if sid.startswith("@")]
    return out


_GHOSTS = ("bridges", "found")


def refresh(budget: int | None = None, now: float | None = None,
            only: set | None = None) -> dict:
    """Fetch what is new or due, at most `budget`; drop what no source has
    any more. Counts, never names. `only` — `(kind, sid)` pairs — looks at
    just those (a pick just made), dropping nothing."""
    from . import contacts

    budget = BUDGET if budget is None else budget
    now = time.time() if now is None else now
    owner = contacts.is_owner()
    counts = {"fetched": 0, "kept": 0, "none": 0, "failed": 0, "dropped": 0, "waiting": 0}
    conn = contacts._connect()
    try:
        have = _rows(conn)
        want = _wanted(conn, owner)
    finally:
        conn.close()
    todo: list[tuple[str, str, str]] = []
    alive = set()
    if only is not None:
        want = [w for w in want if (w[0], w[1]) in only]
    for kind, sid, ref in want:
        alive.add((kind, sid))
        row = have.get((kind, sid))
        if kind == "google":
            if not ref:
                if row is None or row["ref"] or row["file"]:
                    todo.append((kind, sid, ""))          # none, or none any more
            elif row is None or row["ref"] != ref or (row["tries"] and row["next_at"] <= now) \
                    or (row["file"] and not (photos_dir() / row["file"]).exists()):
                todo.append((kind, sid, ref))
        elif row is None or row["next_at"] <= now:
            todo.append((kind, sid, row["ref"] if row else ""))
    # Never looked at first, then the longest due.
    todo.sort(key=lambda t: ((t[0], t[1]) in have,
                             (have.get((t[0], t[1])) or {}).get("next_at") or 0.0))
    results = []
    for kind, sid, ref in todo:
        if kind == "google" and not ref:
            results.append((kind, sid, "", "", "", 0, 0.0))
            counts["none"] += 1
            continue
        if counts["fetched"] >= budget:
            counts["waiting"] += 1
            continue
        counts["fetched"] += 1
        tries = ((have.get((kind, sid)) or {}).get("tries") or 0)
        try:
            if kind == "google":
                data = fetch_google(ref)
            else:
                ref = ghost_avatar(sid)
                if not ref:
                    results.append((kind, sid, "", "", "", 0, now + RECHECK_S))
                    counts["none"] += 1
                    continue
                old = have.get((kind, sid))
                if old and old["ref"] == ref and old["file"] and \
                        (photos_dir() / old["file"]).exists():
                    results.append((kind, sid, ref, old["file"], old["ctype"], 0,
                                    now + RECHECK_S))
                    counts["kept"] += 1
                    continue
                data = fetch_mxc(ref)
            got = _keep(data)
            if got is None:
                raise ValueError("not an image")
            results.append((kind, sid, ref, got[0], got[1], 0,
                            now + (RECHECK_S if kind in _GHOSTS else 30 * 86400.0)))
            counts["kept"] += 1
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.info("contacts: a %s picture unread: %s", kind, e)
            old = have.get((kind, sid)) or {}
            results.append((kind, sid, old.get("ref") or ref, old.get("file") or "",
                            old.get("ctype") or "", tries + 1, now + _backoff(tries + 1)))
            counts["failed"] += 1
    with contacts._WRITE:
        conn = contacts._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for kind, sid, ref, file, ctype, tries, next_at in results:
                _put(conn, kind, sid, ref, file, ctype, now, tries, next_at)
            for key in (set(have) - alive) if only is None else ():
                if key[0] in _GHOSTS and not owner:
                    continue
                conn.execute("DELETE FROM photo_src WHERE kind=? AND sid=?", key)
                counts["dropped"] += 1
            used = {f for (f,) in conn.execute("SELECT file FROM photo_src WHERE file != ''")}
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    # Files no source points at any more.
    try:
        for p in photos_dir().iterdir():
            if p.is_file() and not p.name.startswith(".") and p.name not in used:
                p.unlink()
    except OSError:
        pass
    return counts


# --- reading --------------------------------------------------------------------

def index(conn) -> dict[tuple[str, str], tuple[str, str]]:
    """`(kind, sid)` to `(file, type)` for every source that has a picture."""
    try:
        return {(k, s): (f, c) for k, s, f, c in conn.execute(
            "SELECT kind, sid, file, ctype FROM photo_src WHERE file != ''")}
    except Exception:  # noqa: BLE001 — a store from before pictures: none
        return {}


def best(card: dict, idx: dict) -> tuple[str, str] | None:
    """The card's picture, `(file, type)`: its first source in `KIND_ORDER`
    that has one."""
    srcs = card.get("sources") or []
    for kind in KIND_ORDER:
        for s in srcs:
            if s.get("kind") == kind and (kind, s.get("id") or "") in idx:
                return idx[(kind, s["id"])]
    return None


def field(cid: str, card: dict, idx: dict) -> dict | None:
    """A row's or card's `photo`: `{url, etag}`, or None."""
    got = best(card, idx)
    if not got:
        return None
    return {"url": f"/contacts/{cid}/photo", "etag": got[0][:16]}


def photo(cid: str) -> tuple[bytes, str, str, float] | None:
    """`GET /contacts/<id>/photo`: `(bytes, type, etag, mtime)`, or None."""
    from . import contacts

    conn = contacts._connect(create=False)
    if conn is None:
        return None
    try:
        got = conn.execute("SELECT card FROM contacts WHERE id=?", (cid,)).fetchone()
        idx = index(conn) if got else {}
    finally:
        conn.close()
    if not got:
        return None
    b = best(json.loads(got[0]), idx)
    if not b:
        return None
    path = photos_dir() / b[0]
    try:
        data = path.read_bytes()
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return data, b[1] or sniff(data) or "application/octet-stream", b[0][:16], mtime
