"""Mirror the complete private Rye booking feed into its Google calendar.

The website is the record; Google is where David sees (and sometimes moves)
his visits. "Ask, don't overwrite" (David, 22 Oct 2026):

- A booking moved or resized on the website: Google follows.
- A booking moved or resized in Google (its times there differ from what the
  mirror last wrote, while the feed still has the old time): Google is kept as
  he left it and an alert asks "Change the booking on the website?", once —
  not every ten minutes. Its actions: `apply` changes the website
  (`calendar.rye.site_command`; offered only when that is set), `keep` puts
  the website's time back in Google.
- A booking deleted in Google: never cancelled on the website by itself. The
  alert asks; the mirror leaves it deleted until he decides (`cancel` cancels
  it on the website, `keep` puts it back in Google).
- Both changed: the website wins, and he is told.
- A CANCELLED entry in the feed: skipped, and its Google copy removed. An
  entry that cannot be read is skipped and its Google copy left alone; one
  booking's failure never stops the rest (nor the availability publication
  that runs this first).

What the mirror last wrote per booking is kept in
`$XDG_STATE_HOME/agent-media/rye-bookings.json` (owner-only), which is how a
website edit is told from a Google one. Alerts and that file carry the event
title's client first name (as Google already shows it) and times — never
contact details, addresses or notes.
"""
from __future__ import annotations

import base64
import configparser
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request
from zoneinfo import ZoneInfo

from . import calendar_write

UID = re.compile(r"rye-booking-(\d+)@ryeremedial\.com\.au")
MAX_BYTES = 20 * 1024 * 1024
ZONE = ZoneInfo("Australia/Melbourne")
ASK = "calendar.rye.booking."
TOLD = "calendar.rye.told."
HEALTH = "calendar.rye.mirror"
#: How long a "the website won" notice stays up unless acked first.
TOLD_S = 24 * 3600


# --- the feed -------------------------------------------------------------------

def read_feed(body: bytes) -> dict:
    """Validate the envelope, then each entry on its own.

    `{"events": [...], "cancelled": {uid}, "held": {uid}, "unknown": n}`:
    `held` are entries with a booking UID that could not be read (their Google
    copies are left alone); `unknown` counts entries without one, which turns
    off removing absent bookings for this run (an unreadable UID could be any
    of them)."""
    from icalendar import Calendar
    if not body.strip().endswith(b"END:VCALENDAR"):
        raise ValueError("Incomplete booking feed")
    cal = Calendar.from_ical(body)
    if str(cal.get("PRODID")) != "-//Rye Remedial//Bookings//EN" or str(cal.get("METHOD")) != "PUBLISH":
        raise ValueError("Unexpected booking feed")
    events, cancelled, held, unknown, seen = {}, set(), set(), 0, set()
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID") or "")
        if not UID.fullmatch(uid):
            unknown += 1
            continue
        if uid in seen:
            # Two entries for one booking: neither is trusted.
            events.pop(uid, None)
            cancelled.discard(uid)
            held.add(uid)
            continue
        seen.add(uid)
        status = str(ev.get("STATUS") or "")
        if status == "CANCELLED":
            cancelled.add(uid)
            continue
        try:
            start, end = ev.decoded("DTSTART"), ev.decoded("DTEND")
            title = str(ev.get("SUMMARY") or "").strip()
            if (not title or not isinstance(start, dt.datetime) or not isinstance(end, dt.datetime)
                    or start.tzinfo is None or end.tzinfo is None or end <= start
                    or status not in ("CONFIRMED", "TENTATIVE") or ev.get("RRULE")):
                raise ValueError()
        except Exception:
            held.add(uid)
            continue
        events[uid] = {"uid": uid, "title": title, "start": start.isoformat(), "end": end.isoformat(),
                       "status": status, "location": str(ev.get("LOCATION") or ""),
                       "source": "rye-booking", "busy": True,
                       "description": "Synced from Rye Remedial bookings. Change this booking on the website."}
    return {"events": list(events.values()), "cancelled": cancelled, "held": held - set(events),
            "unknown": unknown}


def parse_feed(body: bytes) -> list[dict]:
    """The readable, live bookings of a feed (read_feed's `events`)."""
    return read_feed(body)["events"]


def _read(url: str) -> dict:
    try:
        if not url.startswith("https://ryeremedial.com.au/book/calendar/"):
            raise ValueError()
        req = urllib.request.Request(url, headers={"User-Agent": "Sasonica-Calendar/1.0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError()
        return read_feed(body)
    except Exception:
        raise RuntimeError("Booking feed unavailable or incomplete; Google entries retained") from None


def _local_owned(pair: str) -> list[dict]:
    """Only this adapter's entries in this pair's successfully synced folder."""
    from icalendar import Calendar
    from agent_media_core import config
    settings = config.load().get("calendar", {})
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(settings.get("vdirsyncer_config", "~/.config/vdirsyncer/config")).expanduser())
    local = parser["storage " + json.loads(parser["pair " + pair]["a"])]
    if json.loads(local["type"]) != "filesystem":
        raise ValueError("Booking sync requires a local filesystem calendar")
    folder = Path(json.loads(local["path"])).expanduser()
    if not folder.is_dir():
        raise ValueError("Booking calendar local folder unavailable")
    result = []
    for path in folder.glob("*.ics"):
        for ev in Calendar.from_ical(path.read_bytes()).walk("VEVENT"):
            uid = str(ev.get("UID") or "")
            if (UID.fullmatch(uid) and str(ev.get("X-SASONICA-MANAGED")) == "1"
                    and str(ev.get("X-SASONICA-SOURCE")) == "rye-booking"):
                start = ev.decoded("DTSTART")
                if not isinstance(start, dt.datetime) or start.tzinfo is None:
                    raise ValueError("Invalid managed booking time")
                result.append({"uid": uid, "start": start})
    return result


def _sync_local(pair: str) -> None:
    done = subprocess.run(["vdirsyncer", "sync", pair], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=120)
    if done.returncode:
        raise RuntimeError("Booking calendar sync failed; retry required")


# --- what the mirror last wrote ---------------------------------------------------

def _state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "agent-media" / "rye-bookings.json"


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("bookings", {})
    data.setdefault("told", {})
    return data


def _save_state(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rye-bookings.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _span(start: str, end: str) -> list[str]:
    """Times as comparable instants (UTC ISO); an all-day value stays a date."""
    def norm(v: str) -> str:
        try:
            t = dt.datetime.fromisoformat(v)
        except (TypeError, ValueError):
            return str(v or "")
        if len(v) == 10 or t.tzinfo is None:
            return v
        return t.astimezone(dt.timezone.utc).isoformat()
    return [norm(start), norm(end)]


# --- the notices ---------------------------------------------------------------

def _notify(body: dict) -> None:
    """Report to the alert store (server-contract.md §6.17): a phone
    notification, Home's Alerts card and an Org inbox TODO on a raise."""
    from . import alerts
    alerts.report(body)


def _number(uid: str) -> str:
    m = UID.fullmatch(uid)
    if not m:
        raise ValueError("Not a Rye booking")
    return m.group(1)


def _who(title: str) -> str:
    """The client's name as the event title shows it ("Service: Name")."""
    return title.rpartition(": ")[2].strip() or "a client"


def _when(span: list[str]) -> str:
    try:
        a = dt.datetime.fromisoformat(span[0]).astimezone(ZONE)
        b = dt.datetime.fromisoformat(span[1]).astimezone(ZONE)
    except (TypeError, ValueError, IndexError):
        return "a time the mirror cannot read (all day?)"
    t = lambda x: x.strftime("%I:%M%p").lstrip("0").replace(":00", "").lower()
    first = t(a)[:-2] if t(a)[-2:] == t(b)[-2:] else t(a)  # 7–8pm, 11am–12:30pm
    return f"{first}–{t(b)} {a.strftime('%a')} {a.day} {a.strftime('%b')}"


def _site_ready() -> bool:
    from agent_media_core import config
    return bool(config.load().get("calendar", {}).get("rye", {}).get("site_command"))


def _fix(n: str, apply: str) -> str:
    return (f"{apply}: media calendar bookings-decide --booking {n} --decision {'apply' if apply.startswith('Change') else 'cancel'}"
            f" · Put the website's booking back in Google: media calendar bookings-decide --booking {n} --decision keep")


def _ask_moved(uid: str, s: dict) -> None:
    n, who = _number(uid), s["who"]
    actions = [{"id": "keep", "label": "Keep the website's time"}]
    if _site_ready():
        actions.insert(0, {"id": "apply", "label": "Change the website"})
    _notify({"id": ASK + n, "level": "warn", "host": "rye-bookings",
             "title": f"You moved {who} to {_when(s['pending']['google'])} in Google. "
                      "Change the booking on the website?",
             "detail": f"The website has {_when(s['feed'])}. Google keeps your time until you "
                       "choose; changing the website does not email the client.",
             "fix": _fix(n, "Change the website"), "actions": actions})


def _ask_deleted(uid: str, s: dict) -> None:
    n, who = _number(uid), s["who"]
    actions = [{"id": "keep", "label": "Put it back in Google"}]
    if _site_ready():
        actions.insert(0, {"id": "cancel", "label": "Cancel on the website"})
    _notify({"id": ASK + n, "level": "warn", "host": "rye-bookings",
             "title": f"You deleted {who}'s booking ({_when(s['feed'])}) in Google. "
                      "Cancel it on the website?",
             "detail": "Nothing is cancelled on the website until you say, and Google stays "
                       "without it meanwhile. Cancelling emails the client, as the website's "
                       "own Cancel does.",
             "fix": _fix(n, "Cancel it on the website"), "actions": actions})


def _settle(uid: str) -> None:
    _notify({"id": ASK + _number(uid), "level": "ok", "host": "rye-bookings"})


def _tell(state: dict, uid: str, text: str, now: float) -> None:
    n = _number(uid)
    _notify({"id": TOLD + n, "level": "warn", "host": "rye-bookings", "title": text,
             "detail": "The website is the record when both changed. Change it there if "
                       "Google's time was the one you wanted."})
    state["told"][uid] = now + TOLD_S


# --- the mirror ------------------------------------------------------------------

def _put(event: dict, pair: str) -> int:
    """Write the website's version; 1 if Google changed."""
    current = calendar_write.write(event, calendar=pair, sync=False)
    if current.get("exists") and not current.get("matches"):
        calendar_write.write(event, calendar=pair, etag=current["etag"], sync=False)
        return 1
    return 1 if current.get("created") else 0


def _record(books: dict, event: dict, span: list[str]) -> None:
    books[event["uid"]] = {"feed": span, "wrote": span, "who": _who(event["title"])}


def _one(event: dict, state: dict, pair: str, now: float) -> int:
    """One live booking. Returns how many Google writes it made."""
    books, uid = state["bookings"], event["uid"]
    feed = _span(event["start"], event["end"])
    s = books.get(uid)
    if s is None or not s.get("wrote"):
        # First sight (or a "keep" decision): the website's version.
        changed = _put(event, pair)
        _record(books, event, feed)
        return changed
    s["who"] = _who(event["title"])
    feed_moved = feed != s["feed"]
    pending = s.get("pending")
    cur = calendar_write.inspect_event(uid, calendar=pair)
    if not cur.get("exists"):
        if feed_moved:
            # The website changed a booking deleted in Google: the website wins.
            changed = _put(event, pair)
            _record(books, event, feed)
            if pending:
                _settle(uid)
            _tell(state, uid, f"{s['who']}'s booking, deleted in Google, was changed on the "
                              f"website to {_when(feed)}: it is back in Google.", now)
            return changed
        if not pending or pending.get("kind") != "deleted":
            s["pending"] = {"kind": "deleted", "since": now}
            _ask_deleted(uid, s)
        return 0
    google = _span(cur.get("start", ""), cur.get("end", ""))
    if google == s["wrote"]:
        if pending:
            # Put back in Google by hand: nothing to ask any more.
            s.pop("pending", None)
            _settle(uid)
        changed = _put(event, pair)
        _record(books, event, feed)
        return changed
    if feed_moved:
        changed = _put(event, pair)
        _record(books, event, feed)
        if pending:
            _settle(uid)
        if google != feed:
            _tell(state, uid, f"{s['who']} was changed on the website to {_when(feed)} while "
                              f"you had moved them to {_when(google)} in Google: the website's "
                              "time is in Google now.", now)
        return changed
    # Moved in Google only: keep it, ask once (again only if he moves it again).
    if not pending or pending.get("kind") != "moved" or pending.get("google") != google:
        s["pending"] = {"kind": "moved", "google": google, "since": now}
        _ask_moved(uid, s)
    return 0


def _remove(uid: str, state: dict, pair: str, now: float, why: str) -> int:
    s = state["bookings"].pop(uid, None)
    if s and s.get("pending"):
        _settle(uid)
        _tell(state, uid, f"{s.get('who') or 'A client'}'s booking was {why} on the website; "
                          "its Google copy is gone.", now)
    current = calendar_write.inspect_event(uid, calendar=pair)
    if current.get("exists"):
        calendar_write.write({"uid": uid}, calendar=pair, etag=current["etag"],
                             delete=True, sync=False)
        return 1
    return 0


def _config() -> tuple[str, str]:
    from agent_media_core import config
    settings = config.load().get("calendar", {}).get("rye", {})
    pair, url = str(settings.get("write_pair") or ""), str(settings.get("feed_url") or "")
    if not pair or not url:
        raise ValueError("Rye booking calendar destination is not configured")
    return pair, url


def sync(*, sync_local: bool = True) -> dict:
    pair, url = _config()
    feed = _read(url)
    _sync_local(pair)
    old = _local_owned(pair)
    state = _load_state()
    now = time.time()
    keep = {e["uid"] for e in feed["events"]}
    changed = errors = 0
    try:
        for event in feed["events"]:
            try:
                changed += _one(event, state, pair, now)
            except Exception:
                errors += 1  # one booking never stops the rest; nothing private logged
        for uid in sorted(feed["cancelled"]):
            try:
                changed += _remove(uid, state, pair, now, "cancelled")
            except Exception:
                errors += 1
        # A missing future booking means cancellation, but only after a read in
        # which every entry was attributable to a booking.
        utc_now = dt.datetime.now(dt.timezone.utc)
        if not feed["unknown"]:
            for event in old:
                uid = event["uid"]
                if uid in keep or uid in feed["cancelled"] or uid in feed["held"] or event["start"] <= utc_now:
                    continue
                try:
                    changed += _remove(uid, state, pair, now, "removed")
                except Exception:
                    errors += 1
        for uid, until in list(state["told"].items()):
            if until <= now:
                state["told"].pop(uid)
                _notify({"id": TOLD + _number(uid), "level": "ok", "host": "rye-bookings"})
    finally:
        _save_state(state)
    skipped = len(feed["held"]) + feed["unknown"]
    if errors or skipped:
        _notify({"id": HEALTH, "level": "warn", "host": "rye-bookings",
                 "title": f"Rye booking mirror: {errors} booking(s) not mirrored, "
                          f"{skipped} feed entr{'y' if skipped == 1 else 'ies'} unreadable",
                 "detail": "The rest were mirrored. Unreadable entries keep their Google copies.",
                 "fix": "media calendar bookings-sync"})
    else:
        _notify({"id": HEALTH, "level": "ok", "host": "rye-bookings"})
    if sync_local:
        _sync_local(pair)
    asked = sum(1 for s in state["bookings"].values() if s.get("pending"))
    return {"ok": True, "bookings": len(feed["events"]), "changed": changed,
            "cancelled": len(feed["cancelled"]), "skipped": skipped, "errors": errors,
            "waiting": asked}


# --- David's answer ------------------------------------------------------------

class SiteError(RuntimeError):
    pass


_MOVE = r"""
$in = json_decode(base64_decode('%s'), TRUE);
$db = \Drupal::database(); $lock = \Drupal::lock();
$out = function ($ok, $error = NULL) { print json_encode(['ok' => $ok, 'error' => $error]) . "\n"; };
if (!$lock->acquire('rye_booking', 30)) { $out(FALSE, 'busy'); return; }
try {
  $b = $db->select('rye_booking', 'b')->fields('b')->condition('id', $in['id'])->execute()->fetchAssoc();
  if (!$b || !in_array($b['status'], ['confirmed', 'pending'], TRUE) || (int) $b['start'] !== $in['was']) { $out(FALSE, 'changed'); return; }
  $availability = \Drupal::service('rye_booking.availability');
  if ($in['op'] === 'cancel') {
    $db->update('rye_booking')->fields(['status' => 'cancelled'])->condition('id', $b['id'])->condition('start', $b['start'])->execute();
    // As BookingController::setStatus does: refund a credit, tell the client.
    if ($b['credit']) { \Drupal::service('rye_booking.credits')->refund((int) $b['credit']); }
    rye_booking_notify(['status' => 'cancelled'] + $b, 'cancelled');
    $out(TRUE); return;
  }
  $minutes = intdiv($in['end'] - $in['start'], 60);
  $fields = ['start' => $in['start'], 'sequence' => (int) $b['sequence'] + 1];
  if ($minutes !== (int) $b['minutes']) {
    $price = $availability->price($b['service'], $minutes, (bool) $b['mates']);
    if ($price === NULL) { $out(FALSE, 'duration'); return; }
    $fields += ['minutes' => $minutes, 'price' => $price];
  }
  if ($availability->overlaps(['minutes' => $minutes] + $b, $in['start']) !== NULL) { $out(FALSE, 'overlap'); return; }
  $n = $db->update('rye_booking')->fields($fields)->condition('id', $b['id'])->condition('start', $b['start'])->execute();
  $out((bool) $n, $n ? NULL : 'changed');
} finally { $lock->release('rye_booking'); }
"""

_SITE_ERRORS = {
    "busy": "The booking system was busy; nothing was changed. Try again.",
    "changed": "The booking changed on the website meanwhile; nothing was changed there.",
    "duration": "The website has no price for that length of visit; nothing was changed.",
    "overlap": "That time overlaps another visit on the website; nothing was changed.",
}


class DrushSite:
    """Changes one booking on the Rye Remedial site by running PHP in its
    Drupal (`drush php:eval 'eval(stream_get_contents(STDIN));'`), the way
    bookings have been added by hand. `command` is `calendar.rye.site_command`,
    an argv that runs that drush with the script on stdin. Only ever conditional
    on the booking still starting when the mirror last saw it; no client
    detail is sent or printed."""

    def __init__(self, command: list[str]):
        self.command = [str(c) for c in command]

    def _run(self, params: dict) -> None:
        payload = base64.b64encode(json.dumps(params).encode()).decode()
        try:
            done = subprocess.run(self.command, input=(_MOVE % payload).encode(),
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90)
            lines = [x for x in done.stdout.decode(errors="replace").splitlines() if x.strip()]
            answer = json.loads(lines[-1]) if lines else {}
        except Exception:
            raise SiteError("The website could not be reached; nothing was changed") from None
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            code = answer.get("error") if isinstance(answer, dict) else None
            raise SiteError(_SITE_ERRORS.get(code, "The website refused the change; nothing was changed"))

    def move(self, booking: int, *, was: int, start: int, end: int) -> None:
        self._run({"op": "move", "id": booking, "was": was, "start": start, "end": end})

    def cancel(self, booking: int, *, was: int) -> None:
        self._run({"op": "cancel", "id": booking, "was": was})


def _site():
    from agent_media_core import config
    command = config.load().get("calendar", {}).get("rye", {}).get("site_command")
    if not command or not isinstance(command, list):
        raise SiteError("Changing the website is not set up (calendar.rye.site_command); "
                        "change the booking at ryeremedial.com.au/admin/content/bookings")
    return DrushSite(command)


def _epoch(value: str) -> int:
    t = dt.datetime.fromisoformat(value)
    if t.tzinfo is None:
        raise SiteError("Google's time is all day; set a time in Google, or keep the website's")
    return int(t.timestamp())


def _uid_of(booking: str) -> str:
    text = str(booking).strip()
    m = re.fullmatch(r"(?:rye-booking-)?(\d+)(?:@ryeremedial\.com\.au)?", text)
    if not m:
        raise ValueError("Booking: its number, or rye-booking-<n>[@ryeremedial.com.au]")
    return f"rye-booking-{m.group(1)}@ryeremedial.com.au"


def decide(booking: str, decision: str) -> dict:
    """David's answer to a waiting booking: `apply` (a Google move, onto the
    website), `cancel` (a Google delete, onto the website) or `keep` (put the
    website's booking back in Google)."""
    pair, _ = _config()
    uid = _uid_of(booking)
    state = _load_state()
    s = state["bookings"].get(uid)
    pending = (s or {}).get("pending")
    if not pending:
        raise ValueError("Nothing is waiting on that booking")
    n = int(_number(uid))
    if decision == "keep":
        s.pop("pending", None)
        s["wrote"] = None  # the next pass writes the website's version
        _save_state(state)
        _settle(uid)
        try:
            result = sync()
        except Exception:
            return {"ok": True, "booking": n, "decision": "keep",
                    "note": "Recorded; the next mirror run puts it back in Google."}
        return {"ok": True, "booking": n, "decision": "keep", "mirror": result}
    if decision == "apply" and pending["kind"] == "moved":
        google = pending["google"]
        _site().move(n, was=_epoch(s["feed"][0]), start=_epoch(google[0]), end=_epoch(google[1]))
        s["feed"] = s["wrote"] = google
        s.pop("pending", None)
    elif decision == "cancel" and pending["kind"] == "deleted":
        _site().cancel(n, was=_epoch(s["feed"][0]))
        state["bookings"].pop(uid, None)
    else:
        raise ValueError(f"'{decision}' does not answer this booking (it was {pending['kind']} in Google)")
    _save_state(state)
    _settle(uid)
    return {"ok": True, "booking": n, "decision": decision}


def alert_action(aid: str, action: str) -> tuple[bool, dict]:
    """alerts.ACTION_HANDLERS' entry for `calendar.rye.booking.<n>`."""
    if not aid.startswith(ASK):
        return False, {"error": "not a booking alert"}
    try:
        return True, decide(aid[len(ASK):], action)
    except (ValueError, SiteError) as e:
        return False, {"error": str(e)}
