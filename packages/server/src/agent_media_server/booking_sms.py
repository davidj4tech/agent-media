"""Texts to Rye Remedial clients, from David's own number (David, 22 Oct 2026).

The ten-minute booking mirror (booking_calendar.sync) tells, from the feed and
what it saw last, when a booking is newly confirmed, moved or cancelled, and
this sends the client a short text through the Google Messages bridge
(mautrix-gmessages, the same one the Messages tab uses), as David:

- **confirmed**: a booking that appears confirmed, or a pending one confirmed.
- **moved**: the website moved a confirmed booking, or David's Google move was
  applied to the website (`bookings-decide … apply`), past the email's rule
  (`booking_calendar.emails_client`: start or end by more than 15 minutes, or
  another Melbourne day). "Change, no email" (`apply-quiet`) sends no text.
- **cancelled**: a booking gone from a complete feed (or cancelled through
  `bookings-decide … cancel`) that the website says is cancelled — not one
  declined or deleted.
- **reminder**: once, the day before a confirmed booking, inside
  `reminder_hours` (default 10:00–18:00 Melbourne); not when a confirmed or
  moved text went out for it that day.

Nothing is sent in `quiet_hours` (default 21:00–08:00); texts found then wait
in the state file and go at the first run after. A text whose visit has
started is dropped. Before each send the site is asked (read-only, through
`calendar.rye.site_command`) for the booking's status, start and phone; a text
the booking no longer fits (status changed, moved again) is dropped. The phone
number lives only in memory for that one send: never in the state file, a
log, Google or an alert. Failures raise `calendar.rye.sms`, are retried on
later runs (`MAX_TRIES`) and never stop the mirror.

Config, `calendar.rye.sms` (off unless `enabled` is true):
`enabled`, `quiet_hours` "21:00-08:00", `reminder_hours` "10:00-18:00",
`test_to` (every text goes to this number instead of the client's, for a
trial), `bridge` ("gmessages", the bridge's appservice id), `login_id` (the
bridge login to send from; default its first), `matrix_env` (the file with
MATRIX_HOMESERVER / MATRIX_OWNER_TOKEN / MATRIX_OWNER_ID when the process
environment lacks them; default ~/.config/agent-media/matrix.env).
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Australia/Melbourne")
ALERT = "calendar.rye.sms"
KINDS = ("confirmed", "moved", "reminder", "cancelled")
#: A text that fails this many runs in a row is given up (and said so).
MAX_TRIES = 3
DEFAULTS = {"enabled": False, "quiet_hours": "21:00-08:00", "reminder_hours": "10:00-18:00",
            "test_to": "", "bridge": "gmessages", "login_id": "",
            "matrix_env": "~/.config/agent-media/matrix.env"}


class SmsError(RuntimeError):
    """A text not sent. Its message never carries a number or a name."""


# --- the words (all of them, here) ---------------------------------------------

#: The service, as a client would say it ("a reminder of your treatment"; David, 22 Oct 2026: "instead of massage treatment").
WHAT = {"Massage therapy": "treatment", "Yoga": "yoga session"}
SIGN = " – David"


def _local(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso).astimezone(ZONE)


def _day(t: dt.datetime) -> str:
    return f"{t:%A} {t.day} {t:%B}"  # Friday 23 October


def _time(t: dt.datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"  # 7:00pm


def _hours(a: dt.datetime, b: dt.datetime) -> str:
    x, y = _time(a), _time(b)
    return (x[:-2] if x[-2:] == y[-2:] else x) + "–" + y  # 7:00–8:00pm, 11:30am–12:30pm


def text(kind: str, first: str, span: list[str], what: str = "treatment") -> str:
    """The text for one booking; the site's voice (rye_booking.module)."""
    a, b = _local(span[0]), _local(span[1])
    when = f"{_day(a)}, {_hours(a, b)}"
    hi = f"Hi {first}, " if first else "Hi, "
    if kind == "confirmed":
        return f"{hi}your Rye Remedial booking is confirmed for {when}. Reply here if anything changes.{SIGN}"
    if kind == "moved":
        return f"{hi}your Rye Remedial booking has moved to {when}. Reply here if anything changes.{SIGN}"
    if kind == "reminder":
        return (f"{hi}a reminder of your {what or 'visit'} tomorrow, {_day(a)} at {_time(a)}. "
                f"Reply here if anything changes.{SIGN}")
    if kind == "cancelled":
        return f"{hi}your Rye Remedial booking on {when} has been cancelled. Reply here to book another time.{SIGN}"
    raise ValueError(f"kind: one of {', '.join(KINDS)}")


def _first(title: str) -> str:
    """The client's first name from the event title ("Massage therapy: Nicole Smith")."""
    name = title.rpartition(": ")[2].strip().split()
    first = name[0] if name else ""
    return first[:1].upper() + first[1:] if first.islower() else first


def _what(title: str) -> str:
    return WHAT.get(title.partition(": ")[0].strip(), "visit")


# --- config and clock ------------------------------------------------------------

def settings() -> dict:
    from agent_media_core import config
    got = config.load().get("calendar", {}).get("rye", {}).get("sms") or {}
    return {**DEFAULTS, **(got if isinstance(got, dict) else {})}


def _window(spec: str) -> tuple[dt.time, dt.time]:
    a, _, b = str(spec).partition("-")
    return dt.time.fromisoformat(a.strip()), dt.time.fromisoformat(b.strip())


def _within(t: dt.datetime, spec: str) -> bool:
    """`t` (Melbourne) inside "HH:MM-HH:MM"; a window may cross midnight."""
    if not spec:
        return False
    a, b = _window(spec)
    now = t.time()
    return a <= now < b if a <= b else (now >= a or now < b)


def _epoch(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).timestamp())


def _number(uid: str) -> int:
    return int(re.fullmatch(r"rye-booking-(\d+)@ryeremedial\.com\.au", uid).group(1))


def e164(raw: str) -> str:
    """An Australian mobile (or an international number) as +61…; SmsError
    (without the number) when it is not one that can be texted."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if str(raw or "").strip().startswith("+"):
        out = "+" + digits
    elif digits.startswith("00"):
        out = "+" + digits[2:]
    elif digits.startswith("61"):
        out = "+" + digits
    elif digits.startswith("04") and len(digits) == 10:
        out = "+61" + digits[1:]
    elif digits.startswith("4") and len(digits) == 9:
        out = "+61" + digits
    else:
        raise SmsError("The booking's phone number is not a mobile that can be texted")
    if not 9 <= len(out) - 1 <= 15:
        raise SmsError("The booking's phone number is not a mobile that can be texted")
    return out


# --- what changed (the mirror's ten-minute pass) -----------------------------------

def _sms(state: dict) -> tuple[dict, bool]:
    """state["sms"] and whether this is its first pass (nothing is texted for
    what was already there when the feature first ran)."""
    sms = state.get("sms")
    if not isinstance(sms, dict):
        sms = state["sms"] = {}
        baseline = True
    else:
        baseline = False
    sms.setdefault("bookings", {})
    sms.setdefault("queue", {})
    return sms, baseline


def _queue(sms: dict, uid: str, kind: str, rec: dict, now: float) -> None:
    """One text waiting per booking: a cancellation replaces anything; a move
    on a confirmation not yet sent stays a confirmation, at the new time."""
    old = sms["queue"].get(uid)
    if old and old["kind"] == "confirmed" and kind == "moved":
        kind = "confirmed"
    sms["queue"][uid] = {"kind": kind, "span": list(rec["span"]), "first": rec.get("first", ""),
                         "what": rec.get("what", ""), "since": now, "tries": 0}


def detect(state: dict, feed: dict, now: float) -> None:
    """Compare the feed with what was seen last; queue texts when enabled.
    Runs (and records) when disabled too, so turning texts on later sends
    only what changes after."""
    from .booking_calendar import _span, emails_client
    on = bool(settings().get("enabled"))
    sms, baseline = _sms(state)
    books, seen = sms["bookings"], set()
    for ev in feed["events"]:
        uid, status = ev["uid"], ev["status"]
        seen.add(uid)
        span = _span(ev["start"], ev["end"])
        rec = books.get(uid)
        new = {**(rec or {}), "status": status, "span": span,
               "first": _first(ev["title"]), "what": _what(ev["title"])}
        if on and not baseline and status == "CONFIRMED":
            if rec is None or rec.get("status") != "CONFIRMED":
                _queue(sms, uid, "confirmed", new, now)
            elif rec.get("span") != span and emails_client(rec["span"], span):
                _queue(sms, uid, "moved", new, now)
        books[uid] = new
    gone = set(feed.get("cancelled") or ())
    if not feed.get("unknown"):
        gone |= set(books) - seen - set(feed.get("held") or ())
    for uid in gone:
        rec = books.pop(uid, None)
        if rec and on and not baseline and _future(rec, now):
            _queue(sms, uid, "cancelled", rec, now)


def _future(rec: dict, now: float) -> bool:
    try:
        return _epoch(rec["span"][0]) > now
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def applied(state: dict, uid: str, span: list[str], *, text: bool, now: float) -> None:
    """David's Google move went onto the website: the client's text when the
    email went too (`text`); the new time recorded either way, so the feed's
    copy of it is no move."""
    sms, _ = _sms(state)
    rec = sms["bookings"].get(uid)
    if rec is None:
        return
    rec["span"] = list(span)
    if text and settings().get("enabled") and rec.get("status") == "CONFIRMED":
        _queue(sms, uid, "moved", rec, now)


def cancelled(state: dict, uid: str, now: float) -> None:
    """David cancelled a booking on the website from its Google delete."""
    sms, _ = _sms(state)
    rec = sms["bookings"].pop(uid, None)
    if rec and settings().get("enabled") and _future(rec, now):
        _queue(sms, uid, "cancelled", rec, now)


# --- sending ---------------------------------------------------------------------

#: What each text needs the website to say before it goes.
_WANTS = {"confirmed": "confirmed", "moved": "confirmed", "reminder": "confirmed",
          "cancelled": "cancelled"}


def _recipient(n: int, kind: str, span: list[str], cfg: dict, site) -> str | None:
    """The number to send to, or None when the booking no longer fits the
    text. Asks the site (read-only); `test_to` replaces the client's number."""
    from .booking_calendar import SiteError
    try:
        got = site().lookup(n)
    except SiteError:
        if cfg.get("test_to") and not _site_configured():
            return e164(cfg["test_to"])  # a trial without the site: unchecked
        raise
    if got.get("status") != _WANTS[kind]:
        return None
    if kind != "cancelled" and int(got.get("start") or 0) != _epoch(span[0]):
        return None
    return e164(cfg["test_to"]) if cfg.get("test_to") else e164(got.get("phone") or "")


def _site_configured() -> bool:
    from .booking_calendar import _site_ready
    return _site_ready()


def flush(state: dict, now: float, *, site, sender=None) -> dict:
    """Send what is waiting and the day-before reminders, outside quiet hours.
    Never raises; failures go to the `calendar.rye.sms` alert."""
    cfg = settings()
    sms, _ = _sms(state)
    queue, books = sms["queue"], sms["bookings"]
    if not cfg.get("enabled"):
        queue.clear()
        return {"sent": 0, "failed": 0, "waiting": 0}
    for uid in [u for u, item in queue.items() if not _future(item, now)]:
        queue.pop(uid)  # the visit has started: too late
    local = dt.datetime.fromtimestamp(now, ZONE)
    sent, failed, gave_up = 0, [], []
    try:
        quiet = _within(local, cfg.get("quiet_hours") or "")
        remind = _within(local, cfg.get("reminder_hours") or "")
    except ValueError:
        _alert([("config", "", "calendar.rye.sms quiet_hours/reminder_hours: HH:MM-HH:MM")], [])
        return {"sent": 0, "failed": 1, "waiting": len(queue)}
    if quiet:
        return {"sent": 0, "failed": 0, "waiting": len(queue), "quiet": True}
    get_sender = sender or _sender

    def deliver(uid: str, kind: str, item: dict, txn: str) -> str:
        """'sent', 'dropped' or 'failed'."""
        n = _number(uid)
        try:
            to = _recipient(n, kind, item["span"], cfg, site)
            if to is None:
                return "dropped"
            get_sender(cfg).send(to, text(kind, item.get("first", ""), item["span"], item.get("what", "")), txn)
            return "sent"
        except Exception as e:
            failed.append((kind, n, str(e) if isinstance(e, SmsError) or type(e).__name__ == "SiteError"
                           else "The text could not be sent"))
            return "failed"

    for uid, item in sorted(queue.items()):
        txn = f"rye-sms-{_number(uid)}-{item['kind']}-{_epoch(item['span'][0])}-{int(item['since'])}"
        got = deliver(uid, item["kind"], item, txn)
        if got == "failed":
            item["tries"] = item.get("tries", 0) + 1
            if item["tries"] < MAX_TRIES:
                continue
            gave_up.append((item["kind"], _number(uid)))
        if got == "sent":
            sent += 1
            if uid in books and item["kind"] in ("confirmed", "moved"):
                books[uid]["texted"] = now
        queue.pop(uid)
    if remind:
        tomorrow = local.date() + dt.timedelta(days=1)
        for uid, rec in sorted(books.items()):
            if rec.get("status") != "CONFIRMED" or uid in queue or not _future(rec, now):
                continue
            start = _local(rec["span"][0])
            if start.date() != tomorrow or rec.get("reminded") == tomorrow.isoformat():
                continue
            texted = rec.get("texted")
            if texted and dt.datetime.fromtimestamp(texted, ZONE).date() == local.date():
                rec["reminded"] = tomorrow.isoformat()  # told today already
                continue
            got = deliver(uid, "reminder", rec, f"rye-sms-{_number(uid)}-reminder-{_epoch(rec['span'][0])}")
            if got == "failed":
                rec["remind_tries"] = rec.get("remind_tries", 0) + 1
                if rec["remind_tries"] < MAX_TRIES:
                    continue
                gave_up.append(("reminder", _number(uid)))
            sent += got == "sent"
            rec["reminded"] = tomorrow.isoformat()
            rec.pop("remind_tries", None)
    if failed:
        _alert(failed, gave_up)
    elif sent:
        _notify({"id": ALERT, "level": "ok", "host": "rye-bookings"})
    return {"sent": sent, "failed": len(failed), "waiting": len(queue)}


def _notify(body: dict) -> None:
    from . import booking_calendar
    booking_calendar._notify(body)


def _alert(failed: list, gave_up: list) -> None:
    what = ", ".join(f"{k} for booking {n}" if n else k for k, n, _ in failed)
    detail = failed[0][2] + "."
    if gave_up:
        detail += " Given up after %d tries: %s; text the client yourself." % (
            MAX_TRIES, ", ".join(f"{k} for booking {n}" for k, n in gave_up))
    else:
        detail += " It is tried again on the next run (every ten minutes)."
    _notify({"id": ALERT, "level": "warn", "host": "rye-bookings",
             "title": f"Rye booking text not sent: {what}"[:200], "detail": detail,
             "fix": "media calendar bookings-sync"})


# --- the bridge --------------------------------------------------------------------

def _env_file(path: str) -> dict:
    out = {}
    try:
        lines = Path(path).expanduser().read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        k, sep, v = line.partition("=")
        if sep and re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
            out[k] = v.strip().strip("'\"")
    return out


class GmessagesSender:
    """One SMS from David's number through mautrix-gmessages (bridgev2):

    1. `POST /_matrix/provision/v3/create_dm/<+61…>?user_id=<owner>` on the
       bridge's provisioning API (bearer = its `provisioning.shared_secret`,
       found as bridges.py finds it) starts (or finds) the chat with that
       number and answers its portal room, `dm_room_mxid`.
    2. The owner joins it (a no-op when already in) and sends the text there
       with his own token (`MATRIX_OWNER_TOKEN`), as the Messages tab does
       (matrix.Room.send); the bridge sends it as an SMS from his phone.
       The transaction id is the text's own, so a retry after an unclear
       failure is the same message, not a second one.

    Unverified against the live bridge (22 Oct 2026): the identifier format
    gmessages' ResolveIdentifier takes (E.164 is sent) and whether a new
    portal invites the owner (the join covers it when it does)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _env(self) -> dict:
        env = _env_file(self.cfg.get("matrix_env") or DEFAULTS["matrix_env"])
        env.update({k: v for k, v in os.environ.items() if k.startswith("MATRIX_") and v})
        return env

    @staticmethod
    def _http(method: str, url: str, token: str, body: dict | None, what: str, timeout=60) -> dict:
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                got = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise SmsError(f"{what} refused it (HTTP {e.code})") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise SmsError(f"{what} could not be reached") from None
        return got if isinstance(got, dict) else {}

    def send(self, number: str, body: str, txn: str) -> str:
        from agent_media_core import matrix
        from . import bridges
        env = self._env()
        token, owner = env.get("MATRIX_OWNER_TOKEN") or "", matrix.owner_of(env)
        homeserver = (env.get("MATRIX_HOMESERVER") or "").rstrip("/")
        if not token or not owner or not homeserver:
            raise SmsError("No Matrix owner login for texts (MATRIX_OWNER_TOKEN, MATRIX_OWNER_ID, "
                           "MATRIX_HOMESERVER; run matrix-owner-login)")
        bridge = bridges.find(self.cfg.get("bridge") or "gmessages")
        if bridge is None:
            raise SmsError("The Google Messages bridge was not found (its provisioning secret)")
        q = {"user_id": owner}
        if self.cfg.get("login_id"):
            q["login_id"] = self.cfg["login_id"]
        url = (f"{bridge.url}/_matrix/provision/v3/create_dm/"
               f"{urllib.parse.quote(number, safe='')}?{urllib.parse.urlencode(q)}")
        room = self._http("POST", url, bridge.secret, None, "The Google Messages bridge").get("dm_room_mxid")
        if not room:
            raise SmsError("The Google Messages bridge gave no chat for that number")
        r = urllib.parse.quote(room, safe="")
        api = f"{homeserver}/_matrix/client/v3"
        self._http("POST", f"{api}/rooms/{r}/join", token, {}, "The homeserver")
        got = self._http("PUT", f"{api}/rooms/{r}/send/m.room.message/{urllib.parse.quote(txn, safe='')}",
                         token, {"msgtype": "m.text", "body": body}, "The homeserver")
        return str(got.get("event_id") or "")


def _sender(cfg: dict):
    return GmessagesSender(cfg)


# --- a trial ----------------------------------------------------------------------

def trial(to: str, kind: str, *, now: float | None = None, sender=None) -> dict:
    """One text of `kind` about a made-up booking (Nicole, tomorrow 7–8pm) to
    `to` only — David's own phone — whatever `enabled` and the hours say."""
    if kind not in KINDS:
        raise ValueError(f"--kind: one of {', '.join(KINDS)}")
    number = e164(to)
    base = dt.datetime.now(ZONE) if now is None else dt.datetime.fromtimestamp(now, ZONE)
    day = base.date() + dt.timedelta(days=1)
    a = dt.datetime.combine(day, dt.time(19), ZONE)
    span = [a.isoformat(), (a + dt.timedelta(hours=1)).isoformat()]
    body = text(kind, "Nicole", span, "treatment")
    (sender or _sender)(settings()).send(number, body, f"rye-sms-trial-{uuid.uuid4().hex}")
    return {"ok": True, "kind": kind, "text": body}
