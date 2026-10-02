"""Can David be spoken to now: `POST /device/state`, `GET /free` (server-contract.md §6.22).

Over real HTTP to an in-process canvas (test_contract's rig). The phone is a
paired device from a throwaway store; the answer itself is core free.py's,
pinned in packages/core/tests/test_free.py.
"""

from __future__ import annotations

import time

import pytest

from test_contract import AUTH, call, server, shelf, signed_in, typed  # noqa: F401
from test_mic import _device
from test_phone import HOST, host  # noqa: F401


@pytest.fixture()
def pixel():
    return _device("Pixel 8a")[0]


def test_only_a_device_reports(server, host, pixel):
    body = {"quiet": True}
    assert call(server, "POST", "/device/state", body)[0].status == 401
    assert call(server, "POST", "/device/state", body, host)[0].status == 401
    res, obj = call(server, "POST", "/device/state", body, pixel)
    assert res.status == 200, obj
    assert obj["free"] is False and obj["why"] == ["quiet"]
    assert obj["since"] == pytest.approx(time.time(), abs=5)
    assert obj["until"] is None


def test_a_bad_report_is_400(server, pixel):
    res, _ = call(server, "POST", "/device/state", ["quiet"], pixel)
    assert res.status == 400


def test_reading_without_a_credential_is_401(server):
    assert call(server, "GET", "/free")[0].status != 200


def test_reading_takes_the_gate_or_the_host(server, shelf, signed_in, host, pixel):
    res, obj = call(server, "GET", "/free", None, host)
    assert res.status == 200
    assert obj["free"] is True and obj["age_s"] is None and obj["held"] == 0
    end = time.time() + 1800
    call(server, "POST", "/device/state",
         {"meeting_until": end, "meeting_title": "Weekly 1:1"}, pixel)
    for who in (pixel, AUTH):
        res, obj = call(server, "GET", "/free", None, who)
        assert res.status == 200, obj
        assert obj["free"] is False and obj["why"] == ["meeting"]
        assert obj["until"] == pytest.approx(end)


def test_free_again(server, host, pixel):
    call(server, "POST", "/device/state", {"call": True}, pixel)
    res, obj = call(server, "POST", "/device/state", {"call": False}, pixel)
    assert obj["free"] is True and obj["why"] == [] and obj["since"] is None


def test_catch_me_up_takes_the_host_or_a_device(server, host, pixel, monkeypatch):
    from agent_media_core import catchup
    asked = []
    monkeypatch.setattr(catchup, "request", lambda title_of=None: asked.append(1) or 3)
    assert call(server, "POST", "/catchup", {})[0].status != 200
    for who in (host, pixel):
        res, obj = call(server, "POST", "/catchup", {}, who)
        assert res.status == 200 and obj == {"ok": True, "items": 3}
    assert len(asked) == 2
