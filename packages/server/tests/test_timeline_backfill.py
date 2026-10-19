"""The timeline's past (timeline_backfill.py, §6.25a): the phone's SMS and
MMS, and every bridged room's history, counted once into the ledger. No
phone (a fake adb script), no homeserver (a room whose `_get` answers from
a list of pages), no CRM; HOME, the config dir and the state dir are
throwaway, and every MEDIA_*/MATRIX_* variable is gone."""

from __future__ import annotations

import datetime as dt
import json
import os
import time

import pytest

from agent_media_core import matrix as core_matrix
from agent_media_server import contacts, matrix, timeline, timeline_backfill
from test_contract import call, server, signed_in, typed  # noqa: F401

ME, SAM = "@david:ryer.org", "@sam:ryer.org"
GM_BOT, WA_BOT = "@gmessagesbot:ryer.org", "@whatsappbot:ryer.org"
NONCE = "n0nce"
HOST = "host-token"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_", "AMUX_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MATRIX_SAM_ID", SAM)
    monkeypatch.setenv("MATRIX_OWNER_BRIDGES", f"{GM_BOT},{WA_BOT}")
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setenv("MEDIA_TIMELINE_BACKFILL_PAUSE_S", "0")
    monkeypatch.setenv("AMUX_AUTH_TOKEN", HOST)
    monkeypatch.setattr(matrix, "_ROOMS", {})
    monkeypatch.setattr(timeline_backfill, "_nonce", lambda: NONCE)
    with timeline_backfill._JOB_LOCK:
        timeline_backfill._JOB.clear()
        timeline_backfill._JOB["running"] = False
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    contacts._reset_for_tests()


def _google(n, name, phones=()):
    return ("google", f"people/c{n}",
            {"name": name, "names": [{"display": name, "given": name.split()[0],
                                      "family": name.split()[-1]}],
             "phones": [{"value": contacts.e164(p, "AU"), "raw": p, "type": "Mobile"}
                        for p in phones],
             "emails": [], "addresses": [], "organizations": [], "notes": ""},
            {"etag": f"e{n}"})


LIONEL = contacts.contact_id("google", "people/c1")
MEL = contacts.contact_id("google", "people/c2")


@pytest.fixture
def book():
    conn = contacts._connect()
    try:
        for kind, sid, card, meta in (_google(1, "Lionel Smith", ["0412 345 678"]),
                                      _google(2, "Mel Jones", ["+61 400 111 222"])):
            conn.execute("INSERT OR REPLACE INTO records (kind, sid, card, meta) "
                         "VALUES (?, ?, ?, ?)", (kind, sid, json.dumps(card), json.dumps(meta)))
        contacts._rebuild(conn)
    finally:
        conn.close()
    contacts._INDEX["checked"] = 0.0


def _ms(day_offset, hour=12):
    d = dt.date.today() - dt.timedelta(days=day_offset)
    return int(dt.datetime.combine(d, dt.time(hour)).timestamp() * 1000)


def _day(day_offset):
    return (dt.date.today() - dt.timedelta(days=day_offset)).isoformat()


def _ledger(cid=LIONEL):
    conn = timeline._connect()
    try:
        return {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
            "SELECT channel, day, count_in, count_out FROM entries WHERE contact=?", (cid,))}
    finally:
        conn.close()


# --- the phone ------------------------------------------------------------------

def _rows(rows):
    return "\n".join(f"Row: {i} {r}" for i, r in enumerate(rows)) or "No result found."


def _phone(tmp_path, sms, mms, addrs, fail_after=None, bad_ids=()):
    """A fake adb that answers each of the backfill's scripts as the phone
    would: the SMS, the MMS list, or a batch of MMS addresses. Each script
    is logged (one per line in `scripts`); with `fail_after`, the address
    batches after that many fail as a phone gone off Wi-Fi does; an id in
    `bad_ids` takes the shell down mid-batch as the live phone did on 20 Oct
    2026 (its marker and a row printed, then exit 255, no end marker)."""
    data = tmp_path / "phone.json"
    data.write_text(json.dumps({"sms": sms, "mms": mms, "addrs": addrs,
                                "fail_after": fail_after, "bad_ids": list(bad_ids)}))
    log = tmp_path / "scripts"
    log.write_text("")
    script = tmp_path / "fake-adb"
    script.write_text(f"""#!/usr/bin/env python3
import json, re, sys
d = json.load(open({str(data)!r}))
s = sys.argv[-1]
with open({str(log)!r}, "a") as f:
    f.write(s.replace("\\n", " ") + "\\n")
m = re.search(r"@@AM-[^@]+@@", s).group(0)
def rows(rs):
    return "\\n".join(f"Row: {{i}} {{r}}" for i, r in enumerate(rs)) or "No result found."
out = []
if "content://sms" in s:
    out += [m + " sms", rows(d["sms"])]
elif "_id:date:msg_box" in s:
    out += [m + " mms", rows(d["mms"])]
else:
    ids = re.search(r"for i in ([0-9 ]+); do", s).group(1).split()
    n = sum(1 for line in open({str(log)!r}) if "/addr" in line)
    if d["fail_after"] is not None and n > d["fail_after"]:
        print("error: device offline", file=sys.stderr)
        sys.exit(1)
    for i in ids:
        if i in d["bad_ids"]:
            out += [f"{{m}} addr-{{i}}", "Row: 0 address=+61415584499, type=151"]
            print("\\n".join(out))
            sys.exit(255)
        out += [f"{{m}} addr-{{i}}", rows(d["addrs"].get(i, []))]
out.append(m + " end")
print("\\n".join(out))
""")
    script.chmod(0o755)
    return str(script) + " shell"


SMS = [
    f"date={_ms(0, 9)}, type=1, address=0412 345 678",
    f"date={_ms(0, 10)}, type=1, address=+61412345678",
    f"date={_ms(0, 11)}, type=2, address=0412345678",
    f"date={_ms(0, 12)}, type=3, address=0412345678",          # a draft: left out
    f"date={_ms(1, 12)}, type=2, address=+61400111222",        # Mel, yesterday
    f"date={_ms(0, 13)}, type=1, address=0499 999 999",        # nobody's
]
MMS = [
    f"_id=7, date={_ms(0, 14) // 1000}, msg_box=1",             # Lionel to the phone
    f"_id=8, date={_ms(0, 15) // 1000}, msg_box=2",             # a group, sent
    f"_id=9, date={_ms(0, 16) // 1000}, msg_box=2",             # to Lionel alone
]
ADDRS = {
    "7": ["address=+61412345678, type=137", "address=+61499555000, type=151"],
    "8": ["address=insert-address-token, type=137", "address=0412345678, type=151",
          "address=0400111222, type=151"],
    "9": ["address=insert-address-token, type=137", "address=0412 345 678, type=151"],
}


def test_the_scripts_never_ask_for_a_body():
    s = timeline_backfill.phone_script(NONCE)
    assert "content://sms" in s and "content://mms" in s and "/addr" in s
    assert "body" not in s and "text" not in s.replace("content", "")
    assert timeline_backfill.mms_addr_script(NONCE, ["7", "x; rm", "9"]).startswith(
        "for i in 7 9; do")                                       # ids only, never words


def test_the_phone_is_counted_per_contact_and_day(book, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", _phone(tmp_path, SMS, MMS, ADDRS))
    got = timeline_backfill.phone()
    assert got["sums"][(LIONEL, "sms", _day(0))] == [3, 2]        # 2 SMS + 1 MMS in; 1 + 1 out
    assert got["sums"][(MEL, "sms", _day(1))] == [0, 1]
    assert (got["sms"], got["mms"], got["groups"], got["nobody"]) == (5, 3, 1, 1)
    assert got["counted"] == 6


def test_the_phone_merges_with_the_ledger_by_the_larger_count(book, tmp_path, monkeypatch):
    timeline.import_days({(LIONEL, "sms", _day(0)): [1, 7]}, push_=False)   # the live rollup's
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", _phone(tmp_path, SMS, MMS, ADDRS))
    got = timeline_backfill.run(rooms_on=False)
    assert got["phone"]["days"] == 2 and "error" not in got["phone"]
    assert _ledger()[("sms", _day(0))] == (3, 7)
    again = timeline_backfill.run(rooms_on=False)
    assert again["phone"]["days"] == 0                                  # the same run: nothing
    raw = timeline.db_path().read_bytes()
    assert b"0412" not in raw and b"+61412345678" not in raw            # no number kept


def test_a_phone_without_the_tool_or_that_does_not_answer_is_said(book, tmp_path, monkeypatch):
    got = timeline_backfill.run(rooms_on=False)
    assert got["phone"] == {"error": "no phone tool on this host"}
    bad = tmp_path / "bad-adb"
    bad.write_text("#!/bin/sh\necho 'error: device offline' >&2\nexit 1\n")
    bad.chmod(0o755)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", f"{bad} shell")
    got = timeline_backfill.run(rooms_on=False)
    assert "exited 1" in got["phone"]["error"]
    assert "device offline" not in got["phone"]["error"]              # never the tool's words


def test_the_phone_is_read_in_short_scripts_mms_in_batches(book, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_backfill, "MMS_BATCH", 2)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", _phone(tmp_path, SMS, MMS, ADDRS))
    got = timeline_backfill.phone()
    scripts = (tmp_path / "scripts").read_text().splitlines()
    assert len(scripts) == 4                     # the SMS, the MMS list, 2 batches of 2 and 1
    assert "content://sms" in scripts[0] and "_id:date:msg_box" in scripts[1]
    assert "for i in 7 8; do" in scripts[2] and "for i in 9; do" in scripts[3]
    assert got["sums"][(LIONEL, "sms", _day(0))] == [3, 2]
    st = timeline_backfill._state(timeline_backfill.MMS_ROW)
    assert st["token"] == "9" and st["done"]


def test_an_mms_pass_cut_short_counts_what_it_read_and_goes_on(book, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_backfill, "MMS_BATCH", 1)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", _phone(tmp_path, SMS, MMS, ADDRS, fail_after=1))
    first = timeline_backfill.run(rooms_on=False)
    assert "exited 1" in first["phone"]["mms_error"]
    assert first["phone"]["mms"] == 1                            # MMS 7 only
    assert _ledger()[("sms", _day(0))] == (3, 1)                 # SMS + the MMS read
    assert timeline_backfill._state(timeline_backfill.MMS_ROW)["token"] == "7"
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", _phone(tmp_path, SMS, MMS, ADDRS))
    second = timeline_backfill.run(rooms_on=False)
    assert "mms_error" not in second["phone"]
    asked = (tmp_path / "scripts").read_text()
    assert "for i in 7;" not in asked                            # not read again
    assert second["phone"]["mms"] == 3                           # all, counted once
    assert _ledger()[("sms", _day(0))] == (3, 2)
    third = timeline_backfill.run(rooms_on=False)
    assert third["phone"]["days"] == 0 and third["phone"]["mms"] == 3
    raw = timeline.db_path().read_bytes()
    assert b"0412" not in raw and b"+61412345678" not in raw     # no number kept


def test_a_phone_that_cannot_list_its_sms_is_unreadable(book, tmp_path, monkeypatch):
    bad = tmp_path / "bad-adb"
    bad.write_text("#!/bin/sh\necho 'error: device offline' >&2\nexit 1\n")
    bad.chmod(0o755)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", f"{bad} shell")
    got = timeline_backfill.run(rooms_on=False)
    assert "exited 1" in got["phone"]["error"] and "sms" not in got["phone"]


def test_an_mms_that_takes_the_shell_down_does_not_stop_the_pass(book, tmp_path, monkeypatch):
    # The live phone, 20 Oct 2026: a batch printed rows, then exited 255.
    monkeypatch.setattr(timeline_backfill, "MMS_BATCH", 3)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", _phone(tmp_path, SMS, MMS, ADDRS, bad_ids=["8"]))
    got = timeline_backfill.run(rooms_on=False)
    p = got["phone"]
    assert "mms_error" not in p and "error" not in p
    assert p["mms"] == 3 and p["unreadable"] == 1                 # 8 left out, 7 and 9 read
    assert _ledger()[("sms", _day(0))] == (3, 2)                  # 7 in, 9 out, as before
    asked = (tmp_path / "scripts").read_text().splitlines()
    assert any("for i in 7 8 9; do" in a for a in asked)          # the batch
    assert any("for i in 8; do" in a for a in asked)              # then the rest alone
    assert any("for i in 9; do" in a for a in asked)
    assert timeline_backfill._state(timeline_backfill.MMS_ROW)["token"] == "9"
    assert all("|| true" in a for a in asked if "/addr" in a)     # an id's failure is its own
    assert "MMS the phone would not read" in " ".join(timeline_backfill.say(got))


def test_an_error_never_says_a_number_or_an_address(book, tmp_path, monkeypatch):
    import re as _re

    def says_a_number(text):
        return bool(_re.search(r"(?:\d[\s().-]?){6,}", text or ""))   # 6+ digits

    leaky = tmp_path / "leaky-adb"
    leaky.write_text("#!/bin/sh\necho 'Row: 2 address=+61415584499, type=151'\n"
                     "echo 'body=meet at 0412 345 678' >&2\nexit 255\n")
    leaky.chmod(0o755)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", f"{leaky} shell")
    got = timeline_backfill.run(rooms_on=False)
    err = got["phone"]["error"]
    assert "exited 255" in err and not says_a_number(err) and "address" not in err
    assert "meet at" not in err
    for line in timeline_backfill.say(got):
        assert not says_a_number(line)
    from agent_media_server import contacts_android
    for text in ("Row: 2 address=+61415584499, type=151",
                 "Error while accessing provider: content://mms/8/addr 0412 345 678"):
        assert not says_a_number(contacts_android.scrub(text))
    try:
        contacts_android.rows("Error: number=+61 412 345 678 refused")
    except contacts_android.Unreachable as e:
        assert not says_a_number(str(e))


# --- the bridged rooms ----------------------------------------------------------

LIONEL_SMS = "@gmessages_1.42:ryer.org"
LIONEL_WA = "@whatsapp_61412345678:ryer.org"


def _ev(i, day_offset, sender, **content):
    return {"type": "m.room.message", "event_id": f"$e{i}", "sender": sender,
            "origin_server_ts": _ms(day_offset, 10 + i % 10),
            "content": content or {"msgtype": "m.text", "body": "secret words"}}


class Pages:
    """A homeserver's /messages for one room, newest page first."""

    def __init__(self, pages):
        self.pages = pages
        self.asked: list = []

    def __call__(self, config, path, params=None, timeout=10.0):
        assert path.endswith("/messages") and params["dir"] == "b"
        self.asked.append(params.get("from"))
        i = 0 if not params.get("from") else int(params["from"][1:])
        chunk = self.pages[i] if i < len(self.pages) else []
        end = f"t{i + 1}" if i + 1 < len(self.pages) else None
        return {"chunk": chunk, "start": f"t{i}", **({"end": end} if end else {})}


def _room(room_id, ghost, name, network, pages):
    cfg = core_matrix.Config(homeserver="https://hs.test", token="t", rooms={room_id})
    get = Pages(pages)
    r = matrix.Room(room_id, cfg, ME, get=get)
    r.members = {ME: "David", GM_BOT: "bridge bot", ghost: name}
    r.network = {"id": network, "name": network}
    r.loaded = True
    matrix._ROOMS[r.thread] = r
    return r, get


def _sms_pages():
    return [
        [_ev(1, 0, LIONEL_SMS), _ev(2, 0, ME), _ev(3, 1, LIONEL_SMS),
         _ev(4, 1, ME, msgtype="m.notice", body="bridge says"),              # not a message
         _ev(5, 0, ME, msgtype="m.text", body="* fixed",
             **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$e2"}}),  # an edit
         {"type": "m.reaction", "sender": LIONEL_SMS, "origin_server_ts": _ms(0),
          "content": {}}],
        [_ev(6, 1, LIONEL_SMS), _ev(7, 1, ME), _ev(8, 3, LIONEL_SMS)],
        [_ev(9, 3, ME)],
    ]


def test_a_room_is_counted_back_to_its_start_without_bodies(book):
    r, get = _room("!a:ryer.org", LIONEL_SMS, "Lionel Smith", "gmessages", _sms_pages())
    got = timeline_backfill.rooms()
    assert got == {"rooms": 1, "done": 1, "messages": 7, "days": 3, "errors": 0}
    assert _ledger() == {("sms", _day(0)): (1, 1), ("sms", _day(1)): (2, 1),
                         ("sms", _day(3)): (1, 1)}
    assert b"secret words" not in timeline.db_path().read_bytes()
    asked = len(get.asked)
    assert timeline_backfill.rooms()["done"] == 1 and len(get.asked) == asked   # done: not again


def test_a_run_cut_short_goes_on_and_a_split_day_is_counted_once(book):
    r, get = _room("!a:ryer.org", LIONEL_SMS, "Lionel Smith", "gmessages", _sms_pages())
    mine, phones = contacts.own_ghosts(), contacts._self_ids()[0]
    first = timeline_backfill.room(r, LIONEL, "sms", mine=mine, self_phones=phones, pages=1)
    assert not first["done"]
    # Yesterday is the oldest day reached, so it is held back, not written half.
    assert _ledger() == {("sms", _day(0)): (1, 1)}
    rest = timeline_backfill.room(r, LIONEL, "sms", mine=mine, self_phones=phones)
    assert rest["done"] and get.asked == [None, "t1", "t2"]
    assert _ledger()[("sms", _day(1))] == (2, 1)                       # both pages' share, once
    assert _ledger()[("sms", _day(3))] == (1, 1)


def test_a_homeserver_down_leaves_the_room_to_go_on_later(book):
    r, get = _room("!a:ryer.org", LIONEL_SMS, "Lionel Smith", "gmessages", _sms_pages())
    real = r._get
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("homeserver down")
        return real(*a, **k)
    r._get = flaky
    got = timeline_backfill.rooms()
    assert got["errors"] == 1 and got["done"] == 0
    got = timeline_backfill.rooms()
    assert got["errors"] == 0 and got["done"] == 1
    assert _ledger()[("sms", _day(1))] == (2, 1)


def test_whatsapp_rooms_count_as_whatsapp(book):
    _room("!w:ryer.org", LIONEL_WA, "Lionel", "whatsapp",
          [[_ev(1, 2, LIONEL_WA), _ev(2, 2, ME), _ev(3, 2, LIONEL_WA)]])
    got = timeline_backfill.rooms()
    assert got["rooms"] == 1 and got["done"] == 1
    assert _ledger()[("whatsapp", _day(2))] == (2, 1)


def test_a_room_of_no_contact_or_an_unknown_network_is_left(book):
    _room("!x:ryer.org", "@gmessages_1.99:ryer.org", "Nobody Known", "gmessages",
          [[_ev(1, 0, "@gmessages_1.99:ryer.org")]])
    _room("!y:ryer.org", "@telegram_61412345678:ryer.org", "Lionel Smith", "telegram",
          [[_ev(1, 0, "@telegram_61412345678:ryer.org")]])
    assert timeline_backfill.rooms()["rooms"] == 0


# --- the job and the route ------------------------------------------------------

def _wait_done():
    deadline = time.monotonic() + 10
    while timeline_backfill.status().get("running"):
        assert time.monotonic() < deadline, "the job did not finish"
        time.sleep(0.05)
    return timeline_backfill.status()


def test_the_route_starts_the_job_once_and_says_how_it_goes(server, book):
    _room("!a:ryer.org", LIONEL_SMS, "Lionel Smith", "gmessages", _sms_pages())
    res, obj = call(server, "POST", "/contacts/timeline/backfill", {})
    assert res.status == 401 and not obj["ok"]
    res, obj = call(server, "POST", "/contacts/timeline/backfill", {},
                    headers={"X-Auth-Token": HOST})
    assert res.status == 200 and obj["ok"] and obj["started"]
    st = _wait_done()
    assert st["rooms"]["messages"] == 7 and st["phone"] == {"error": "no phone tool on this host"}
    res, obj = call(server, "GET", "/contacts/timeline/backfill", headers={"X-Auth-Token": HOST})
    assert res.status == 200 and obj["running"] is False and "finished_at" in obj
    assert "Lionel" not in json.dumps(obj)


def test_a_running_job_is_not_started_twice():
    with timeline_backfill._JOB_LOCK:
        timeline_backfill._JOB.update({"running": True})
    got = timeline_backfill.start()
    assert got["started"] is False and got["running"] is True


def test_the_job_is_said_in_counts():
    lines = timeline_backfill.say({
        "phone": {"sms": 5, "mms": 3, "counted": 6, "groups": 1, "days": 2},
        "rooms": {"rooms": 4, "done": 3, "messages": 120, "days": 40, "errors": 1}})
    assert lines == ["phone: 5 SMS and 3 MMS read, 6 a contact's, 1 group MMS left out; "
                     "2 days changed",
                     "rooms: 3 of 4 read to their start, 120 messages counted; 40 days changed; "
                     "1 cut short (run again to go on)"]
