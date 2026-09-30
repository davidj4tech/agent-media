"""The phone as the TV's mic: `POST /mic/ask`, `POST /mic/cancel` (server-contract.md §6.20).

Over real HTTP to an in-process canvas (test_contract's rig), with two paired
devices — a TV and a phone — from a throwaway device store. The `mic` frame
on `/sessions/events` is pinned in test_session_events.py.
"""

from __future__ import annotations

import pytest

from agent_media_server import auth_abs, devices, mic

from test_contract import AUTH, SID2, call, server, shelf, signed_in, typed  # noqa: F401


def _device(name: str) -> tuple[dict, str]:
    """A paired device: `(headers, device_id)`."""
    code, _ = devices.mint_code(name)
    got = devices.redeem(code, name)
    return {"Authorization": f"Bearer {got['token']}"}, got["device_id"]


@pytest.fixture()
def tv():
    return _device("Living room TV")


@pytest.fixture()
def phone():
    return _device("Pixel 8a")


def test_an_ask_names_the_device_and_the_thread(server, shelf, signed_in, tv):
    hdrs, dev_id = tv
    res, obj = call(server, "POST", "/mic/ask", {"session": SID2}, hdrs)
    assert res.status == 200, obj
    a = obj["ask"]
    assert obj["ok"] is True
    assert len(a["id"]) == 8 and int(a["id"], 16) >= 0
    assert a["device"] == "Living room TV" and a["device_id"] == dev_id
    assert a["session"] == SID2 and a["title"] == "Sasonica web"
    assert a["expires"] == pytest.approx(a["at"] + 90)
    assert mic.open_asks() == [a]


def test_no_session_is_a_new_chat(server, tv):
    for body in ({}, {"session": None}, {"session": ""}):
        res, obj = call(server, "POST", "/mic/ask", body, tv[0])
        assert res.status == 200, obj
        assert obj["ask"]["session"] is None and obj["ask"]["title"] is None


def test_an_unknown_thread_has_no_title(server, tv):
    sid = "11111111-2222-4333-8444-555555555555"
    res, obj = call(server, "POST", "/mic/ask", {"session": sid}, tv[0])
    assert res.status == 200 and obj["ask"]["title"] is None


def test_a_bad_session_id_is_400(server, tv):
    for bad in ("nope", 42, ["x"]):
        res, obj = call(server, "POST", "/mic/ask", {"session": bad}, tv[0])
        assert res.status == 400 and obj["ok"] is False, bad
    assert mic.open_asks() == []


def test_refused_without_a_credential(server, monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda bearer: (None, 401))
    res, obj = call(server, "POST", "/mic/ask", {})
    assert res.status == 401 and obj["ok"] is False
    res, obj = call(server, "POST", "/mic/cancel", {"id": "abcd1234"})
    assert res.status == 401
    assert mic.open_asks() == []


def test_a_second_ask_from_the_same_device_replaces_the_first(server, tv, phone):
    first = call(server, "POST", "/mic/ask", {}, tv[0])[1]["ask"]
    other = call(server, "POST", "/mic/ask", {}, phone[0])[1]["ask"]
    second = call(server, "POST", "/mic/ask", {"session": SID2}, tv[0])[1]["ask"]
    assert first["id"] != second["id"]
    assert {a["id"] for a in mic.open_asks()} == {other["id"], second["id"]}


def test_cancel_by_either_side(server, tv, phone):
    a = call(server, "POST", "/mic/ask", {}, tv[0])[1]["ask"]
    res, obj = call(server, "POST", "/mic/cancel", {"id": a["id"]}, phone[0])
    assert res.status == 200 and obj == {"ok": True, "id": a["id"]}
    assert mic.open_asks() == []
    res, obj = call(server, "POST", "/mic/cancel", {"id": a["id"]}, tv[0])
    assert res.status == 404 and obj["ok"] is False
    assert call(server, "POST", "/mic/cancel", {}, tv[0])[0].status == 404


def test_an_expired_ask_is_gone(server, tv, monkeypatch):
    a = call(server, "POST", "/mic/ask", {}, tv[0])[1]["ask"]
    v = mic.version()
    later = a["expires"] + 1
    monkeypatch.setattr(mic.time, "time", lambda: later)
    assert mic.open_asks() == []
    assert mic.version() == v + 1, "an expiry is a change the stream sends"
    assert call(server, "POST", "/mic/cancel", {"id": a["id"]}, tv[0])[0].status == 404


def test_open_asks_leaves_out_the_devices_own(server, tv, phone):
    a = call(server, "POST", "/mic/ask", {}, tv[0])[1]["ask"]
    assert mic.open_asks(tv[1]) == []
    assert mic.open_asks(phone[1]) == [a]
    assert mic.open_asks("") == [a], "a login that is no device excludes nothing"


def test_an_abs_login_may_ask_too(server, signed_in):
    res, obj = call(server, "POST", "/mic/ask", {}, AUTH)
    assert res.status == 200, obj
    assert obj["ask"]["device"] == "david" and obj["ask"]["device_id"] is None


def test_cross_origin_like_reply(server):
    res, _ = call(server, "OPTIONS", "/mic/ask")
    assert res.status == 204
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    assert call(server, "OPTIONS", "/mic/cancel")[0].status == 204
