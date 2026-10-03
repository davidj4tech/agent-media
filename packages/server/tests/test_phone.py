"""The phone as an agent's eyes and hands: `/phone/ask`, `/phone/answer`, `/phone/cancel` (server-contract.md §6.21).

Over real HTTP to an in-process canvas (test_contract's rig). The agent's
side takes the host's own token; the phone is a paired device from a
throwaway store. "A phone is listening" is set directly (`phone.listening`);
the `phone` frame itself is pinned in test_session_events.py.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent_media_server import phone

from test_contract import AUTH, SID2, call, server, shelf, signed_in, typed  # noqa: F401
from test_mic import _device

HOST = {"X-Auth-Token": "hosttok"}


@pytest.fixture()
def host(monkeypatch):
    monkeypatch.setenv("AMUX_AUTH_TOKEN", "hosttok")
    return HOST


@pytest.fixture()
def pixel():
    return _device("Pixel 8a")[0]


@pytest.fixture()
def listening():
    phone.listening(("photo",), True)
    yield
    phone.listening(("photo",), False)


def _audit(tmp_path) -> list[dict]:
    p = tmp_path / "state" / "agent-media" / "phone-asks.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_asking_takes_the_host_token(server, host, pixel, listening):
    body = {"kind": "photo", "why": "show me the router lights"}
    assert call(server, "POST", "/phone/ask", body)[0].status == 401
    # A paired device is not an agent on this host.
    assert call(server, "POST", "/phone/ask", body, pixel)[0].status == 401
    res, obj = call(server, "POST", "/phone/ask", body, host)
    assert res.status == 200, obj
    a = obj["ask"]
    assert a["status"] == "open" and a["kind"] == "photo"
    assert a["why"] == "show me the router lights"
    assert a["expires"] == pytest.approx(a["at"] + 300)
    assert phone.open_asks(("photo",)) == [a]
    assert phone.open_asks(("dnd",)) == []


def test_no_phone_listening_is_no_phone_at_once(server, host, tmp_path):
    res, obj = call(server, "POST", "/phone/ask", {"kind": "photo", "why": "x"}, host)
    assert res.status == 200 and obj["ask"]["status"] == "no_phone"
    assert phone.open_asks(("photo",)) == []
    assert [r["status"] for r in _audit(tmp_path)] == ["no_phone"]


@pytest.mark.parametrize("body", [{"kind": "camera", "why": "x"}, {"kind": "photo"},
                                  {"kind": "photo", "why": "x", "session": "nope"}])
def test_a_bad_ask_is_400(server, host, body):
    assert call(server, "POST", "/phone/ask", body, host)[0].status == 400


def test_allowed_with_a_photo_is_ok(server, shelf, signed_in, host, pixel, listening, tmp_path):
    _, obj = call(server, "POST", "/phone/ask",
                  {"kind": "photo", "why": "the desk", "session": SID2}, host)
    a = obj["ask"]
    assert a["title"] == "Sasonica web"
    got = {}

    def poll():
        got["r"] = call(server, "GET", f"/phone/ask?id={a['id']}&wait=5", headers=host)

    t = threading.Thread(target=poll)
    t.start()
    time.sleep(0.2)
    res, ans = call(server, "POST", "/phone/answer",
                    {"id": a["id"], "decision": "allow",
                     "result": {"path": "/home/x/shared/2026-10-01/p.jpg", "width": 1600}}, pixel)
    assert res.status == 200, ans
    t.join(5)
    res, obj = got["r"]
    assert res.status == 200 and obj["ask"]["status"] == "ok"
    assert obj["ask"]["result"]["path"].endswith("p.jpg")
    assert phone.open_asks(("photo",)) == []
    line = _audit(tmp_path)[-1]
    assert line["status"] == "ok" and line["path"].endswith("p.jpg")
    assert line["device"] == "Pixel 8a" and line["session"] == SID2
    # A second answer finds it settled.
    res, obj = call(server, "POST", "/phone/answer", {"id": a["id"], "decision": "deny"}, pixel)
    assert res.status == 409 and obj["ask"]["status"] == "ok"


def test_denied_and_failed(server, host, pixel, listening):
    _, obj = call(server, "POST", "/phone/ask", {"kind": "photo", "why": "a"}, host)
    call(server, "POST", "/phone/answer", {"id": obj["ask"]["id"], "decision": "deny"}, pixel)
    assert phone.wait(obj["ask"]["id"])[1]["ask"]["status"] == "denied"
    _, obj = call(server, "POST", "/phone/ask", {"kind": "photo", "why": "b"}, host)
    call(server, "POST", "/phone/answer",
         {"id": obj["ask"]["id"], "decision": "allow", "result": {"error": "camera closed"}}, pixel)
    a = phone.wait(obj["ask"]["id"])[1]["ask"]
    assert a["status"] == "failed" and a["error"] == "camera closed"
    # Allowed but no photo came back is a failure too.
    _, obj = call(server, "POST", "/phone/ask", {"kind": "photo", "why": "c"}, host)
    call(server, "POST", "/phone/answer", {"id": obj["ask"]["id"], "decision": "allow"}, pixel)
    assert phone.wait(obj["ask"]["id"])[1]["ask"]["status"] == "failed"


def test_answering_takes_a_device(server, host, listening):
    _, obj = call(server, "POST", "/phone/ask", {"kind": "photo", "why": "a"}, host)
    res, _ = call(server, "POST", "/phone/answer", {"id": obj["ask"]["id"], "decision": "deny"},
                  {"Authorization": "Bearer nope"})
    assert res.status == 401


def test_an_unanswered_ask_times_out(server, host, listening, monkeypatch):
    monkeypatch.setitem(phone.TTL_S, "photo", 0.3)
    _, obj = call(server, "POST", "/phone/ask", {"kind": "photo", "why": "a"}, host)
    res, got = call(server, "GET", f"/phone/ask?id={obj['ask']['id']}&wait=3", headers=host)
    assert res.status == 200 and got["ask"]["status"] == "timeout"
    assert phone.open_asks(("photo",)) == []


def test_cancel_and_one_per_session(server, shelf, signed_in, host, listening):
    _, first = call(server, "POST", "/phone/ask", {"kind": "photo", "why": "a", "session": SID2}, host)
    _, second = call(server, "POST", "/phone/ask", {"kind": "photo", "why": "b", "session": SID2}, host)
    assert phone.wait(first["ask"]["id"])[1]["ask"]["status"] == "cancelled"
    assert [a["why"] for a in phone.open_asks(("photo",))] == ["b"]
    res, _ = call(server, "POST", "/phone/cancel", {"id": second["ask"]["id"]}, host)
    assert res.status == 200
    assert phone.open_asks(("photo",)) == []
    assert call(server, "GET", "/phone/ask?id=ffffffff", headers=host)[0].status == 404


# --- the agent's tool (agent_media_core.phone_ask), against this server ------------

def test_the_tool_asks_and_waits_for_the_answer(server, host, pixel, listening, monkeypatch):
    from agent_media_core import phone_ask

    monkeypatch.setattr(phone_ask, "_base", lambda: "http://%s:%d" % server)
    monkeypatch.setattr(phone_ask, "session_of_caller", lambda: None)

    def phone_answers():
        for _ in range(50):
            asks = phone.open_asks(("photo",))
            if asks:
                call(server, "POST", "/phone/answer",
                     {"id": asks[0]["id"], "decision": "allow",
                      "result": {"path": "/s/p.jpg", "width": 1600, "height": 1200}}, pixel)
                return
            time.sleep(0.05)

    t = threading.Thread(target=phone_answers)
    t.start()
    got = phone_ask.ask("photo", "show me the desk", timeout_s=10)
    t.join(5)
    assert got == {"status": "ok", "result": {"path": "/s/p.jpg", "width": 1600, "height": 1200}}


def test_the_tool_gives_up_and_cancels(server, host, listening, monkeypatch):
    from agent_media_core import phone_ask

    monkeypatch.setattr(phone_ask, "_base", lambda: "http://%s:%d" % server)
    monkeypatch.setattr(phone_ask, "session_of_caller", lambda: None)
    assert phone_ask.ask("photo", "x", timeout_s=1) == {"status": "timeout"}
    assert phone.open_asks(("photo",)) == []


def test_the_tool_without_a_phone_says_so_at_once(server, host, monkeypatch):
    from agent_media_core import phone_ask

    monkeypatch.setattr(phone_ask, "_base", lambda: "http://%s:%d" % server)
    monkeypatch.setattr(phone_ask, "session_of_caller", lambda: None)
    t0 = time.monotonic()
    assert phone_ask.ask("photo", "x") == {"status": "no_phone"}
    assert time.monotonic() - t0 < 2


# --- Do Not Disturb until a time -----------------------------------------------------

@pytest.fixture()
def quiet_listening():
    phone.listening(("dnd",), True)
    yield
    phone.listening(("dnd",), False)


def test_a_dnd_ask_carries_its_end(server, host, pixel, quiet_listening, tmp_path):
    until = time.time() + 3600
    res, obj = call(server, "POST", "/phone/ask",
                    {"kind": "dnd", "why": "quiet for the meeting", "params": {"until": until}}, host)
    assert res.status == 200, obj
    a = obj["ask"]
    assert a["until"] == pytest.approx(until) and a["expires"] == pytest.approx(a["at"] + 120)
    assert phone.open_asks(("dnd",)) == [a]
    call(server, "POST", "/phone/answer",
         {"id": a["id"], "decision": "allow", "result": {"on": True, "until": until}}, pixel)
    got = phone.wait(a["id"])[1]["ask"]
    assert got["status"] == "ok" and got["result"]["on"] is True
    assert _audit(tmp_path)[-1]["until"] == pytest.approx(until)


def test_allowed_but_not_quiet_is_failed(server, host, pixel, quiet_listening):
    _, obj = call(server, "POST", "/phone/ask",
                  {"kind": "dnd", "why": "q", "params": {"until": time.time() + 600}}, host)
    call(server, "POST", "/phone/answer", {"id": obj["ask"]["id"], "decision": "allow",
                                           "result": {"on": False}}, pixel)
    assert phone.wait(obj["ask"]["id"])[1]["ask"]["status"] == "failed"


@pytest.mark.parametrize("params", [None, {}, {"until": "soon"}, {"until": 0},
                                    {"until": "NOW+30"}, "late"])
def test_a_dnd_without_a_good_end_is_400(server, host, quiet_listening, params):
    body = {"kind": "dnd", "why": "q"}
    if params is not None:
        body["params"] = params
    assert call(server, "POST", "/phone/ask", body, host)[0].status == 400


def test_a_dnd_runs_at_most_twelve_hours(server, host, quiet_listening):
    now = time.time()
    body = {"kind": "dnd", "why": "q", "params": {"until": now + 13 * 3600}}
    assert call(server, "POST", "/phone/ask", body, host)[0].status == 400
    body["params"]["until"] = now + 30
    assert call(server, "POST", "/phone/ask", body, host)[0].status == 400


def test_the_tool_reads_a_time_or_minutes(server, host, pixel, quiet_listening, monkeypatch):
    from agent_media_core import phone_ask

    monkeypatch.setattr(phone_ask, "_base", lambda: "http://%s:%d" % server)
    monkeypatch.setattr(phone_ask, "session_of_caller", lambda: None)
    assert phone_ask.ask("dnd", "q", until="whenever")["status"] == "error"

    def phone_answers():
        for _ in range(50):
            asks = phone.open_asks(("dnd",))
            if asks:
                call(server, "POST", "/phone/answer",
                     {"id": asks[0]["id"], "decision": "allow",
                      "result": {"on": True, "until": asks[0]["until"]}}, pixel)
                return
            time.sleep(0.05)

    t = threading.Thread(target=phone_answers)
    t.start()
    t0 = time.time()
    got = phone_ask.ask("dnd", "quiet for a nap", until="+90", timeout_s=10)
    t.join(5)
    assert got["status"] == "ok"
    assert got["result"]["until"] == pytest.approx(t0 + 90 * 60, abs=5)
