"""This server on its accounts' lists at sasonica.com (servers_report.py,
roadmap item 20): a stable id, the addresses it gives, and one report per
signed-in account through that account's own token. No network: the issuer
and the token refresh are stood in for."""

from __future__ import annotations

import pytest

from agent_media_server import app, devices, servers_report


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for k in ("MEDIA_PUBLIC_URL", "MEDIA_WEB_APP_URL", "MEDIA_SERVER_NAME", "MEDIA_SERVERS_REPORT"):
        monkeypatch.delenv(k, raising=False)
    from agent_media_server import tunnel
    monkeypatch.setattr(tunnel, "current_url", lambda: "")


def test_the_id_is_made_once_and_kept():
    sid = servers_report.server_id()
    assert len(sid) >= 8 and servers_report.server_id() == sid


def test_addresses_prefer_the_configured_then_the_tunnel_then_the_last_sign_in(monkeypatch):
    assert servers_report.addresses() == {"api": "", "web": ""}
    servers_report.remember_base("http://100.1.2.3:8781/")
    assert servers_report.addresses()["api"] == "http://100.1.2.3:8781"
    from agent_media_server import tunnel
    monkeypatch.setattr(tunnel, "current_url", lambda: "https://x.trycloudflare.com")
    assert servers_report.addresses()["api"] == "https://x.trycloudflare.com"
    monkeypatch.setenv("MEDIA_PUBLIC_URL", "https://red5.example")
    monkeypatch.setenv("MEDIA_WEB_APP_URL", "http://100.1.2.3:8795/")
    assert servers_report.addresses() == {"api": "https://red5.example", "web": "http://100.1.2.3:8795"}


def test_one_report_per_account_through_its_latest_device(monkeypatch):
    rows = [
        {"id": "d_old", "iss": "https://cms.test", "sub": "1", "last_seen": 10},
        {"id": "d_new", "iss": "https://cms.test", "sub": "1", "last_seen": 20},
        {"id": "d_two", "iss": "https://cms.test", "sub": "2", "last_seen": 5},
        {"id": "d_paired", "last_seen": 30},                         # a code pairing: no account
        {"id": "d_nort", "iss": "https://cms.test", "sub": "3", "last_seen": 1},  # no refresh token
    ]
    rts = {"d_old": "r", "d_new": "r", "d_two": "r", "d_paired": "", "d_nort": ""}
    monkeypatch.setattr(devices, "list_devices", lambda: rows)
    monkeypatch.setattr(devices, "refresh_token_of",
                        lambda did: ("https://cms.test", rts[did]) if rts.get(did) else ("", ""))
    monkeypatch.setattr(app, "_account_access", lambda did, iss, fresh=False: f"tok-{did}")
    monkeypatch.setenv("MEDIA_SERVER_NAME", "red5")
    sent = []
    monkeypatch.setattr(servers_report, "_post", lambda iss, access, body: sent.append((iss, access, body)) or 200)
    assert servers_report.report_all() == 2
    assert sorted(a for _, a, _ in sent) == ["tok-d_new", "tok-d_two"]
    body = sent[0][2]
    assert body["name"] == "red5" and body["id"] == servers_report.server_id() and set(body) == {"id", "name", "api", "web"}


def test_a_refused_token_is_refreshed_once(monkeypatch):
    monkeypatch.setattr(devices, "refresh_token_of", lambda did: ("https://cms.test", "r"))
    fresh = []
    monkeypatch.setattr(app, "_account_access", lambda did, iss, fresh_=False, **kw: fresh.append(kw.get("fresh", False)) or "t")
    answers = iter([401, 200])
    monkeypatch.setattr(servers_report, "_post", lambda iss, access, body: next(answers))
    assert servers_report.report("d_1") is True
    assert fresh == [False, True]


def test_off_means_no_thread(monkeypatch):
    monkeypatch.setenv("MEDIA_SERVERS_REPORT", "0")
    called = []
    monkeypatch.setattr(servers_report, "report", lambda did: called.append(did))
    servers_report.report_soon("d_1", "http://h:8781")
    assert called == [] and servers_report.addresses()["api"] == "http://h:8781"
