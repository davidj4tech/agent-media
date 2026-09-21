"""Paired device tokens (server-contract.md §9): pairing, the gate, revocation.

Over real HTTP to an in-process canvas, with the same rig as the contract
test (imported from it, so a change to the seams is made once): `typed`
records everything that would reach a pane, `shelf` fakes the sessions, and
the state dir is conftest's throwaway one — devices.json and the pairing
codes land there, never in ~/.local/state.

The two properties that matter most, and so are asserted most directly:

* a device token is checked locally — ABS is never asked who it is, and the
  token itself is never sent to ABS, not even for an item lookup;
* a device pairing code and the canvas's amux pairing code never unlock each
  other's page.
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from agent_media_server import auth_abs, devices
from agent_media_visual import canvas

# The contract test's rig: fixtures and helpers, by name (never `*`, which
# would collect its tests a second time here).
from test_contract import (AUTH, GATED_GETS, ROOT, SID, call, keys, server, shelf,  # noqa: F401
                           signed_in, typed)


def _pair(addr, code, device="Pixel 8a", headers=None):
    return call(addr, "POST", "/pair", {"code": code, "device": device}, headers)


@pytest.fixture()
def abs_spy(monkeypatch):
    """ABS as a recorder: who was asked who, and with which bearer.

    `abs_identity` answers "root" so a route that wrongly asked it would still
    succeed — the assertion on the call log is what catches it, not a status.
    Item lookups answer the shelved conversation, as `signed_in` does.
    """
    calls = {"identity": [], "get": []}

    def identity(bearer):
        calls["identity"].append(bearer)
        return ROOT, 200

    def get(url, bearer, path, method="GET"):
        calls["get"].append((bearer, path))
        if path.startswith("/api/items/"):
            return {"id": "li_1", "path": "/audiobooks/p-agent-media/Sasonica music"}, 200
        return None, 404

    monkeypatch.setattr(auth_abs, "abs_identity", identity)
    monkeypatch.setattr(auth_abs, "_abs_get", get)
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    # The host's own ABS login, which a device's item lookups go out under.
    from agent_media_core import library
    monkeypatch.setattr(library, "_abs_cfg", lambda target=None: ("http://abs", "hosttok", ""))
    return calls


@pytest.fixture()
def device(server):
    """A paired device: `(token, device_id)`."""
    code, _ = devices.mint_code("Pixel 8a")
    res, obj = _pair(server, code)
    assert res.status == 200, obj
    return obj["token"], obj["device_id"]


# --- pairing ----------------------------------------------------------------------

def test_pairing_hands_out_a_token_and_the_server(server):
    code, _ = devices.mint_code("Pixel 8a")
    res, obj = _pair(server, code)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "token", "device_id", "server"}
    assert obj["ok"] is True
    assert len(obj["token"]) == 43                   # token_urlsafe(32)
    assert obj["device_id"].startswith("d_")
    assert keys(obj["server"]) == {"name", "base"}
    host, port = server
    assert obj["server"]["base"] == f"http://{host}:{port}"
    (row,) = devices.list_devices()
    assert row["id"] == obj["device_id"] and row["name"] == "Pixel 8a"


def test_the_base_is_https_behind_a_tls_proxy(server):
    code, _ = devices.mint_code("Pixel 8a")
    res, obj = _pair(server, code, headers={"X-Forwarded-Proto": "https",
                                            "Host": "sasonica.example"})
    assert res.status == 200
    assert obj["server"]["base"] == "https://sasonica.example"


def test_the_desk_names_the_device_not_the_device(server):
    code, _ = devices.mint_code("Pixel 8a")
    _pair(server, code, device="Totally the owner's laptop")
    assert [d["name"] for d in devices.list_devices()] == ["Pixel 8a"]


def test_a_used_code_is_refused(server):
    code, _ = devices.mint_code("Pixel 8a")
    assert _pair(server, code)[0].status == 200
    res, obj = _pair(server, code)
    assert res.status == 403
    assert obj == {"ok": False, "code": "bad_pairing_code",
                   "error": "invalid or expired pairing code"}
    assert len(devices.list_devices()) == 1


def test_a_wrong_code_does_not_burn_the_right_one(server):
    code, _ = devices.mint_code("Pixel 8a")
    wrong = "0" * 8 if code != "0" * 8 else "1" * 8
    assert _pair(server, wrong)[0].status == 403
    assert _pair(server, "")[0].status == 403
    assert _pair(server, code)[0].status == 200


def test_an_expired_code_is_refused(server):
    code, _ = devices.mint_code("Pixel 8a")
    codes = json.loads(devices.codes_path().read_text())
    codes[code]["expires"] = time.time() - 1
    devices.codes_path().write_text(json.dumps(codes))
    res, obj = _pair(server, code)
    assert res.status == 403 and obj["code"] == "bad_pairing_code"
    assert devices.list_devices() == []


def test_the_window_comes_from_the_env(monkeypatch):
    monkeypatch.setenv("MEDIA_DEVICE_PAIR_TTL", "90")
    before = time.time()
    _code, expires = devices.mint_code("x")
    assert before + 89 <= expires <= time.time() + 91
    monkeypatch.delenv("MEDIA_DEVICE_PAIR_TTL")
    assert devices.pair_ttl() == 1800                # PAIR_TTL_S's default


def test_failures_are_rate_limited_per_address(server):
    code, _ = devices.mint_code("Pixel 8a")
    for _ in range(devices.MAX_FAILURES):
        assert _pair(server, "deadbeef" if code != "deadbeef" else "feedface")[0].status == 403
    # Now even the right code is turned away from this address, before it is
    # looked at — so it is not burned either.
    res, obj = _pair(server, code)
    assert res.status == 429 and obj["ok"] is False and obj["code"] == "rate_limited"
    assert devices.redeem(code, "Pixel 8a", "100.64.0.9") is not None


# --- the two pairing codes never unlock each other -------------------------------

@pytest.fixture()
def amux(monkeypatch, tmp_path):
    """A host amux token and a canvas pairing code, both throwaway."""
    monkeypatch.setattr(canvas, "_amux_token", lambda: "AMUX-SECRET-TOKEN")
    path = tmp_path / "spool-pair-code"
    monkeypatch.setattr(canvas, "_pair_code_path", lambda: path)
    path.write_text("a1b2c3d4")
    return "a1b2c3d4"


def test_a_device_code_does_not_open_the_amux_page(server, amux):
    code, _ = devices.mint_code("Pixel 8a")
    res, body = call(server, "GET", f"/pair?c={code}")
    assert res.status == 403
    assert b"AMUX-SECRET-TOKEN" not in (body if isinstance(body, bytes) else json.dumps(body).encode())
    # …and it was not burned by trying: the app can still redeem it.
    assert _pair(server, code)[0].status == 200


def test_the_amux_code_does_not_redeem_a_device_token(server, amux):
    res, obj = _pair(server, amux)
    assert res.status == 403 and "token" not in obj
    assert devices.list_devices() == []
    # …and the canvas's own page still takes it.
    res, body = call(server, "GET", f"/pair?c={amux}")
    assert res.status == 200 and b"AMUX-SECRET-TOKEN" in body


# --- CORS: the POST and its preflight, never the GET ------------------------------

def test_cors_on_post_pair_and_its_preflight(server):
    code, _ = devices.mint_code("Pixel 8a")
    res, _ = _pair(server, code, headers={"Origin": "http://red5:13379"})
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    res, _ = _pair(server, "nope", headers={"Origin": "http://red5:13379"})
    assert res.status == 403 and res.getheader("Access-Control-Allow-Origin") == "*"
    res, _ = call(server, "OPTIONS", "/pair", headers={"Origin": "http://red5:13379"})
    assert res.status == 204
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    assert res.getheader("Access-Control-Allow-Methods") == "POST, OPTIONS"


def test_no_cors_on_the_amux_page(server, amux):
    # The answer that carries the amux token must stay unreadable cross-origin.
    res, _ = call(server, "GET", f"/pair?c={amux}", headers={"Origin": "http://evil.example"})
    assert res.status == 200
    assert res.getheader("Access-Control-Allow-Origin") is None
    res, _ = call(server, "GET", "/pair?c=nope", headers={"Origin": "http://evil.example"})
    assert res.getheader("Access-Control-Allow-Origin") is None


# --- the gate -----------------------------------------------------------------------

@pytest.fixture()
def quiet_routes(monkeypatch):
    """Fakes for what the gated GETs would otherwise reach past the gate: the
    log's builder (which asks ABS for positions), the slash menu (`claude
    -p`), the speech snapshot."""
    from agent_media_core import activity, book_tracks, slash_menu

    monkeypatch.setattr(book_tracks, "conversation_log",
                        lambda s, f, target=None, positions=True: [])
    monkeypatch.setattr(activity, "attach", lambda s, lines: None)
    monkeypatch.setattr(slash_menu, "menu", lambda cwd: [])
    monkeypatch.setattr(canvas, "speech_state", lambda: {"kind": "state", "speaking": False})


#: Gated GETs that need nothing from ABS once the caller is known. The others
#: in GATED_GETS look an item up (`?item=`) or look for a session's item
#: (`/conversation?session=`), and do that under the HOST's ABS login.
_LOCAL = {"/targets", "/sessions/state", "/conversations", f"/draft?session={SID}",
          "/speech/now", f"/commands?session={SID}"}


@pytest.mark.parametrize("path", GATED_GETS)
def test_a_device_token_passes_every_gate_without_asking_abs(
        server, shelf, abs_spy, quiet_routes, device, path):
    token, _id = device
    res, obj = call(server, "GET", path, headers={"Authorization": f"Bearer {token}"})
    assert res.status == 200, obj
    assert obj["ok"] is True
    assert abs_spy["identity"] == []                     # never "who is this?"
    assert all(b != token for b, _ in abs_spy["get"])    # never shown to ABS
    if path in _LOCAL:
        assert abs_spy["get"] == []


def test_a_device_is_the_owner_even_with_root_switched_off(
        server, shelf, abs_spy, device, monkeypatch):
    monkeypatch.setenv("MEDIA_REPLY_ROOT", "0")
    token, _id = device
    res, obj = call(server, "GET", "/targets", headers={"Authorization": f"Bearer {token}"})
    assert res.status == 200, obj


def test_a_revoked_token_falls_back_to_abs_and_is_401(server, shelf, abs_spy, device,
                                                       monkeypatch):
    token, device_id = device
    assert devices.revoke(device_id) is True
    monkeypatch.setattr(auth_abs, "abs_identity",
                        lambda bearer: (abs_spy["identity"].append(bearer), (None, 401))[1])
    res, obj = call(server, "GET", "/targets", headers={"Authorization": f"Bearer {token}"})
    assert res.status == 401 and obj["ok"] is False
    assert abs_spy["identity"] == [token]                 # the fallback ran
    assert devices.revoke(device_id) is False             # already gone


def test_an_abs_bearer_still_works(server, shelf, signed_in, device):
    # The migration keeps both: a bearer that is not a device goes to ABS.
    res, obj = call(server, "GET", "/targets", headers=AUTH)
    assert res.status == 200, obj


# --- the store ----------------------------------------------------------------------

def test_the_store_has_no_token_and_is_owner_only(server, device):
    token, _id = device
    raw = devices.devices_path().read_text()
    assert token not in raw
    (row,) = json.loads(raw)
    assert set(row) == {"id", "name", "sha256", "created", "last_seen", "last_ip"}
    assert row["last_ip"] == "127.0.0.1"
    for p in (devices.devices_path(), devices.codes_path()):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, p
    # No temp files left beside it.
    assert sorted(x.name for x in devices.devices_path().parent.iterdir()
                  if x.name.startswith(".")) == []


def test_last_seen_is_written_at_most_once_a_minute(tmp_path):
    code, _ = devices.mint_code("Pixel 8a")
    got = devices.redeem(code, "", "10.0.0.1")
    token = got["token"]

    def row():
        return json.loads(devices.devices_path().read_text())[0]

    first = row()["last_seen"]
    mtime = devices.devices_path().stat().st_mtime_ns
    assert devices.lookup(token, "10.0.0.2")["id"] == got["device_id"]
    assert row()["last_seen"] == first and row()["last_ip"] == "10.0.0.1"
    assert devices.devices_path().stat().st_mtime_ns == mtime    # not rewritten
    # A minute on, the next request stamps it.
    rows = json.loads(devices.devices_path().read_text())
    rows[0]["last_seen"] = first - devices.SEEN_EVERY_S - 1
    devices.devices_path().write_text(json.dumps(rows))
    devices.lookup(token, "10.0.0.2")
    assert row()["last_seen"] >= first and row()["last_ip"] == "10.0.0.2"


def test_lookup_never_matches_nothing():
    assert devices.lookup("") is None
    assert devices.lookup("not-a-token") is None


# --- the CLI ------------------------------------------------------------------------

def test_pair_device_cli_prints_both_links(capsys, monkeypatch):
    monkeypatch.setattr(canvas, "_qr", lambda url: f"[QR {url}]")
    # Must not need (or touch) the amux token or the spool's pair code.
    monkeypatch.setattr(canvas, "_amux_token", lambda: "")
    monkeypatch.setattr(canvas, "_pair_code_path",
                        lambda: (_ for _ in ()).throw(AssertionError("spool touched")))
    assert canvas._cmd_pair(["--device", "Pixel 8a", "--host", "red5", "--port", "8781"]) == 0
    out = capsys.readouterr().out
    (code,) = json.loads(devices.codes_path().read_text())
    assert f"sasonica://pair?server=http%3A%2F%2Fred5%3A8781&code={code}" in out
    assert f"http://red5:8781/pair?c={code}&device=1" in out
    assert "[QR sasonica://pair?" in out


def test_pair_without_device_is_unchanged(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(canvas, "_qr", lambda url: "")
    monkeypatch.setattr(canvas, "_amux_token", lambda: "t")
    path = tmp_path / "pair-code"
    monkeypatch.setattr(canvas, "_pair_code_path", lambda: path)
    assert canvas._cmd_pair(["--host", "red5", "--port", "8781"]) == 0
    assert f"http://red5:8781/pair?c={path.read_text()}" in capsys.readouterr().out
    assert not devices.codes_path().exists()


def test_devices_cli_lists_and_revokes(capsys):
    code, _ = devices.mint_code("Pixel 8a")
    got = devices.redeem(code, "", "10.0.0.1")
    assert devices.cli_devices([]) == 0
    out = capsys.readouterr().out
    assert got["device_id"] in out and "Pixel 8a" in out and "10.0.0.1" in out
    assert devices.cli_devices(["--revoke", got["device_id"]]) == 0
    assert devices.list_devices() == []
    assert devices.cli_devices(["--revoke", got["device_id"]]) == 1
    assert devices.cli_devices([]) == 0
    assert "no paired devices" in capsys.readouterr().out
