"""The export cycle, automated as far as it safely goes (David, 19 Oct 2026:
"how might we automate the exports?" → yes).

Meta's website is never driven: requesting an export stays David's tap.
Everything around it is done here, in the contacts sync loop, for the
owner's book only (an alert reaches the owner's phone, so another
account's book would tell the wrong person):

* **The reminder.** Once a network has been imported, its export is due
  again `MEDIA_CONTACTS_EXPORT_REMIND_DAYS` (90; 0 off) after the last
  import: a `contacts.export.<network>` alert at warn, with the link and
  the exact choices, cleared by the next import.
* **The "ready" mail** (opt-in: `MEDIA_CONTACTS_EXPORT_MAIL=1` and the
  timeline's IMAP connection, timeline-imap.json). Mail from
  facebookmail.com or mail.instagram.com whose subject says the
  information is ready to download raises `contacts.export.ready.<network>`
  with the download link read from the mail when it is Meta's own (a
  facebook.com, instagram.com or accountscenter link), else Accounts
  Center. Cleared by the next import of that network.
* **The import itself.** Export zips that land in the phone's Download
  folder (`facebook-*.zip`, `instagram-*.zip`, `meta-*.zip`, read over
  agent-phone-adb, never deleted there) or in `MEDIA_CONTACTS_IMPORT_DIR`
  are imported once each (by path, size and time), and the counts are
  said as a `contacts.import` digest, which the phone shows.
  `MEDIA_CONTACTS_EXPORT_WATCH=0` turns this off; a phone off Wi-Fi is
  just skipped. The state is `exports.json` in the book's state dir.

Nothing here logs a name.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import email.header
import email.utils
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DYI_URL = "https://accountscentre.facebook.com/info_and_permissions/dyi"
REMIND_DAYS = 90
NETWORKS = {"facebook": "Facebook", "instagram": "Instagram"}
CHOICES = {
    "facebook": "Some of your information → Connections → Friends and followers; "
                "All time, Format JSON",
    "instagram": "Some of your information → Connections → Followers and following; "
                 "All time, Format JSON",
}
#: Settings → Contacts → Import a file's links (`GET /contacts/google`
#: `exports`): what each export brings, said as the reminder says it.
LABELS = {"facebook": "Facebook friends", "instagram": "Instagram followers and following"}
TAKEOUT_URL = "https://takeout.google.com/"
#: The past conversations (contacts_history.py): asked for once, not on the
#: reminder's clock. Message exports are large, so they are imported from
#: this host's import folder or `contacts import`, not over the phone.
HISTORY = (
    {"network": "messenger", "label": "Messenger conversations", "url": DYI_URL,
     "steps": "Download your information → Some of your information → Your activity "
              "across Facebook → Messages only; All time, Format JSON, Media quality Low "
              "→ Download to device."},
    {"network": "instagram_messages", "label": "Instagram conversations", "url": DYI_URL,
     "steps": "Download your information → the Instagram account → Some of your "
              "information → Your Instagram activity → Messages only; All time, Format "
              "JSON, Media quality Low → Download to device."},
    {"network": "chats", "label": "Old Google Talk chats", "url": TAKEOUT_URL,
     "steps": "Google Takeout → Deselect all → Mail → All Mail data included → only "
              "the Chats label → Next step → Export once, .zip → Create export."},
)
HISTORY_WHERE = ("Conversations can be large: put the zip in the import folder on the "
                 "server (MEDIA_CONTACTS_IMPORT_DIR) or run media contacts import <file>; "
                 "up to 64 MB can be picked here or left in the phone's Downloads.")


def export_links() -> list[dict]:
    """`[{network, label, url, steps}]`: where each export is requested,
    with the same choices the reminder names, then the past conversations
    (contacts_history.py)."""
    return [{"network": net, "label": LABELS[net], "url": DYI_URL,
             "steps": (f"Download your information → {CHOICES[net]} → Download to device. "
                       "When it's ready, download it on the phone; it is imported from "
                       "Downloads by itself, or pick it here.")}
            for net in NETWORKS] + [{**h, "steps": f"{h['steps']} {HISTORY_WHERE}"}
                                    for h in HISTORY]


PHONE_DIR = "/sdcard/Download"
EXPORT_NAME = re.compile(r"^(facebook|instagram|meta|takeout)-.*\.zip$", re.I)
LOCAL_NAME = re.compile(
    r"^((facebook|instagram|meta|takeout)-.*\.zip|your_friends\.json|friends\.json|"
    r"followers_\d+\.json|following\.json|.*\.mbox)$", re.I)
MAIL_FROM = ("facebookmail.com", "mail.instagram.com")
MAIL_SUBJECT = re.compile(
    r"(information|data|files?|download).{0,40}(ready|available)|ready to download", re.I)
LINK_HOSTS = re.compile(
    r"^https://([a-z0-9-]+\.)*(facebook\.com|instagram\.com|accountscenter\.facebook\.com|"
    r"accountscentre\.facebook\.com|meta\.com)(/|$)", re.I)
URL = re.compile(r"https://[^\s\"'<>)\]]+")
MAIL_MAX = 512 * 1024
LIST_TIMEOUT_S = 30.0
PULL_TIMEOUT_S = 300.0


# --- state -------------------------------------------------------------------------

def _path() -> Path:
    from .contacts import account_state_dir

    return account_state_dir() / "exports.json"


def load() -> dict:
    try:
        got = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def save(state: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _on(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "off", "no", "false")


def remind_days() -> float:
    try:
        return float(os.environ.get("MEDIA_CONTACTS_EXPORT_REMIND_DAYS") or REMIND_DAYS)
    except ValueError:
        return float(REMIND_DAYS)


def _alert(body: dict) -> None:
    from . import alerts

    ok, got = alerts.report(body)
    if not ok:
        print(f"contacts: export alert refused ({got.get('error')})", file=sys.stderr)


# --- after an import -----------------------------------------------------------------

def note_import(networks, counts: dict, *, via: str = "", now: float | None = None) -> None:
    """An import of `networks` happened (contacts.import_export): the
    reminder clock restarts and its alerts clear. Owner's book only."""
    from .contacts import is_owner

    if not is_owner():
        return
    now = time.time() if now is None else now
    state = load()
    last = state.setdefault("imported", {})
    raised = state.setdefault("raised", {})
    for net in networks:
        if net not in NETWORKS:
            continue
        last[net] = now
        for aid in (f"contacts.export.{net}", f"contacts.export.ready.{net}"):
            if raised.pop(aid, None):
                _alert({"id": aid, "level": "ok",
                        "title": f"{NETWORKS[net]} imported"})
    save(state)
    if via:
        lists = ", ".join(f"{_LIST_NAMES.get(k, k)} {v}" for k, v in sorted(counts.items()))
        _alert({"id": "contacts.import", "kind": "digest", "level": "warn",
                "title": f"Imported {lists}" if lists else "Imported an export",
                "detail": f"From {via}. The people are in People, under their labels; "
                          "past conversations are in each one's timeline."})


_LIST_NAMES = {"facebook_friends": "Facebook friends",
               "instagram_followers": "Instagram followers",
               "instagram_following": "Instagram following",
               "messenger_conversations": "Messenger conversations",
               "instagram_conversations": "Instagram conversations",
               "old_chats": "people from old chats"}


# --- the reminder --------------------------------------------------------------------

def remind(*, now: float | None = None) -> list[str]:
    """Raise each network whose export is due; the ids raised."""
    now = time.time() if now is None else now
    days = remind_days()
    if days <= 0:
        return []
    state = load()
    raised = state.setdefault("raised", {})
    out = []
    for net, at in (state.get("imported") or {}).items():
        if net not in NETWORKS or not isinstance(at, (int, float)):
            continue
        aid = f"contacts.export.{net}"
        if now - at < days * 86400 or raised.get(aid):
            continue
        name = NETWORKS[net]
        _alert({"id": aid, "level": "warn",
                "title": f"Time to refresh your {name} contacts",
                "detail": (f"Your last {name} export was imported "
                           f"{int((now - at) // 86400)} days ago. Request a new one: "
                           f"{CHOICES[net]}. When it's ready, download it on the phone; "
                           "it is imported from Downloads by itself."),
                "fix": DYI_URL})
        raised[aid] = now
        out.append(aid)
    if out:
        save(state)
    return out


# --- the "ready" mail ----------------------------------------------------------------

def _network_of(sender: str) -> str:
    return "instagram" if "instagram" in sender else "facebook"


def _link(msg) -> str | None:
    """Meta's own download link from the mail's text, or None."""
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        try:
            text = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001 — a part that will not decode has no link
            continue
        for u in URL.findall(text.decode("utf-8", "replace").replace("&amp;", "&")):
            u = u.rstrip(".,;")
            if LINK_HOSTS.match(u) and re.search(r"dyi|download|information", u, re.I):
                return u
    return None


def mail(*, now: float | None = None) -> list[str]:
    """Raise a "ready" alert for each new Meta export mail; the ids raised.
    Off unless `MEDIA_CONTACTS_EXPORT_MAIL` and the IMAP connection."""
    from . import timeline

    if not _on("MEDIA_CONTACTS_EXPORT_MAIL", False):
        return []
    cfg = timeline.imap_settings()
    if cfg is None:
        return []
    now = time.time() if now is None else now
    state = load()
    seen = state.setdefault("mail_seen", {})
    raised = state.setdefault("raised", {})
    since = dt.date.fromtimestamp(state.get("mail_checked") or (now - 3 * 86400))
    out = []
    box = timeline._imap(cfg)
    try:
        box.login(cfg["user"], cfg["password"])
        box.select(f'"{cfg["folder"]}"', readonly=True)
        for dom in MAIL_FROM:
            typ, data = box.search(None, "FROM", f'"{dom}"', "SINCE", timeline._imap_date(since))
            ids = (data[0] or b"").split() if typ == "OK" and data else []
            for mid in ids[-50:]:
                typ, got = box.fetch(mid, "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS "
                                          "(FROM SUBJECT MESSAGE-ID DATE)])")
                if typ != "OK":
                    continue
                head = next((p for p in got if isinstance(p, tuple)), None)
                if head is None:
                    continue
                size = re.search(rb"RFC822\.SIZE (\d+)", head[0] or b"")
                h = email.message_from_bytes(head[1])
                doms = [a.lower().rpartition("@")[2] for _, a in
                        email.utils.getaddresses(h.get_all("From") or [])]
                sender = next((x for x in doms if any(x == d or x.endswith("." + d)
                                                      for d in MAIL_FROM)), None)
                if sender is None:
                    continue
                subject = str(email.header.make_header(
                    email.header.decode_header(h.get("Subject") or "")))
                key = (h.get("Message-ID") or f"{mid.decode()}|{subject}").strip()
                if key in seen or not MAIL_SUBJECT.search(subject):
                    continue
                seen[key] = now
                link = None
                if size and int(size.group(1)) <= MAIL_MAX:
                    typ, body = box.fetch(mid, "(BODY.PEEK[])")
                    whole = next((p for p in body if isinstance(p, tuple)), None) \
                        if typ == "OK" else None
                    if whole is not None:
                        link = _link(email.message_from_bytes(whole[1]))
                net = _network_of(sender)
                aid = f"contacts.export.ready.{net}"
                _alert({"id": aid, "level": "warn",
                        "title": f"Your {NETWORKS[net]} export is ready",
                        "detail": ("Download it on the phone: it is imported from "
                                   "Downloads by itself." if link else
                                   "Your export is ready: open Accounts Center to download "
                                   "it on the phone; it is imported from Downloads by itself."),
                        "fix": link or DYI_URL})
                raised[aid] = now
                out.append(aid)
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001 — closing is best effort
            pass
    # Keep the seen list short: a month of keys is plenty.
    state["mail_seen"] = {k: v for k, v in seen.items() if now - v < 31 * 86400}
    state["mail_checked"] = now
    save(state)
    return out


# --- the files themselves -------------------------------------------------------------

def _phone(script: str, timeout: float) -> str:
    from . import contacts_android

    got = subprocess.run(contacts_android.command() + [script], capture_output=True,
                         text=True, timeout=timeout)
    if got.returncode != 0:
        raise RuntimeError((got.stderr or got.stdout or "no answer").strip()[-200:])
    return got.stdout


def phone_files() -> list[tuple[str, int, int]]:
    """`(path, mtime, size)` of the export zips in the phone's Download."""
    out = _phone(f"for f in {PHONE_DIR}/facebook-*.zip {PHONE_DIR}/instagram-*.zip "
                 f"{PHONE_DIR}/meta-*.zip {PHONE_DIR}/takeout-*.zip; do [ -f \"$f\" ] && stat -c '%Y %s %n' \"$f\"; "
                 "done; echo END", LIST_TIMEOUT_S)
    if "END" not in out:
        raise RuntimeError("the listing did not finish")
    files = []
    for line in out.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            if EXPORT_NAME.match(parts[2].rsplit("/", 1)[-1]):
                files.append((parts[2], int(parts[0]), int(parts[1])))
    return files


def phone_read(path: str) -> bytes:
    """One file off the phone (base64 over the shell, so nothing binary
    crosses it); the phone's copy stays."""
    if "'" in path:
        raise ValueError("a quote in the file name")
    return base64.b64decode(_phone(f"base64 '{path}' | tr -d '\\n\\r'", PULL_TIMEOUT_S)
                            .strip(), validate=False)


def local_files(folder: Path) -> list[tuple[str, int, int]]:
    out = []
    try:
        for p in folder.iterdir():
            if p.is_file() and LOCAL_NAME.match(p.name):
                st = p.stat()
                out.append((str(p), int(st.st_mtime), st.st_size))
    except OSError:
        pass
    return out


def watch(*, now: float | None = None) -> dict:
    """Import each export file not seen before: `{imported: [names],
    skipped, errors}`. Owner's book only; the phone only when this host
    has the adb tool."""
    from . import contacts, contacts_android, contacts_import

    got = {"imported": [], "skipped": 0, "errors": {}}
    if not contacts.is_owner() or not _on("MEDIA_CONTACTS_EXPORT_WATCH", True):
        return got
    places: list[tuple[str, list, object]] = []
    if contacts_android.available():
        try:
            places.append(("the phone's Downloads", phone_files(), phone_read))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            got["errors"]["phone"] = str(e)[:200]   # off Wi-Fi: next time
    folder = (os.environ.get("MEDIA_CONTACTS_IMPORT_DIR") or "").strip()
    if folder:
        # A file here is read as it goes (contacts.import_path): any size.
        places.append((folder, local_files(Path(folder).expanduser()), None))
    if not places:
        return got
    state = load()
    seen = state.setdefault("files", {})
    changed = False
    for where, files, read in places:
        for path, mtime, size in files:
            mark = f"{mtime}:{size}"
            if seen.get(path) == mark:
                continue
            seen[path] = mark
            changed = True
            name = path.rsplit("/", 1)[-1]
            if read is not None and size > contacts.IMPORT_MAX:
                # Over the phone's shell a file is read whole: a large one
                # (a message history) belongs in the import folder.
                got["skipped"] += 1
                got["errors"][name] = ("too large to read over the phone: put it in the "
                                       "server's import folder or run contacts import")
                continue
            try:
                save(state)   # seen before importing: a bad file is tried once
                if read is None:
                    contacts.import_path(Path(path), via=where)
                else:
                    contacts.import_export(read(path), name, via=where)
                got["imported"].append(name)
            except contacts_import.NotAnExport as e:
                got["skipped"] += 1
                got["errors"][name] = str(e)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                seen.pop(path, None)   # could not read it: try again next time
                got["errors"][name] = str(e)[:200]
    if changed:
        save(state)
    return got


def run() -> dict:
    """The loop's one call for the book in hand: said, never raised."""
    out = {"reminded": [], "mail": [], "watch": {}}
    from .contacts import is_owner

    if not is_owner():
        return out
    for key, fn in (("watch", watch), ("mail", mail), ("reminded", remind)):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001 — never stop the sync loop
            print(f"contacts: exports {key} failed ({e})", file=sys.stderr)
    return out
