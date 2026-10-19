"""Bridges: signing in from the app (contract §6.26).

Over real HTTP to an in-process server, against a fake bridgev2
provisioning API on a random local port — never the host's real bridges.
Config files, HOME and the XDG dirs are throwaway; every MATRIX_* and
MEDIA_* variable is scrubbed first."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_media_server import auth, bridges, devices
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401

OWNER = "@david:test"
SECRET = "s3cret-for-the-fake-bridge"
PNG = b"\x89PNG\r\n\x1a\nfake-icon"


class Fake:
    """One bridge's provisioning API (and a homeserver's media download)."""

    def __init__(self, name="Microsoft Teams", kind="teams"):
        self.name, self.kind = name, kind
        self.calls: list[tuple[str, str, dict, object]] = []
        self.logins = [{"id": "L1", "name": "david@example.com",
                        "state": {"state_event": "BAD_CREDENTIALS", "timestamp": 1792339271,
                                  "error": "teams-token-expired", "message": "Sign in again"}}]
        self.procs: dict[str, dict] = {}
        self.release = threading.Event()   # ends a display_and_wait
        self.qr_changes = 0                # how many new QRs before complete
        n = [0]

        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _route(self, method):
                u = urllib.parse.urlsplit(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                n_ = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n_) or b"null") if n_ else None
                fake.calls.append((method, u.path, q, body))
                if u.path.startswith("/_matrix/client/v1/media/download/"):
                    if self.headers.get("Authorization") != "Bearer owner-tok":
                        return self._answer(401, {"errcode": "M_MISSING_TOKEN"})
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(PNG)))
                    self.end_headers()
                    self.wfile.write(PNG)
                    return
                if self.headers.get("Authorization") != f"Bearer {SECRET}" \
                        or q.get("user_id") != OWNER:
                    return self._answer(401, {"errcode": "M_UNKNOWN_TOKEN",
                                              "error": "Invalid auth token"})
                p = u.path.removeprefix("/_matrix/provision/v3")
                parts = [urllib.parse.unquote(x) for x in p.strip("/").split("/")]
                if method == "GET" and p == "/whoami":
                    return self._answer(200, {
                        "network": {"displayname": fake.name, "network_id": fake.kind,
                                    "beeper_bridge_type": "go.mau.fi/mautrix-teams",
                                    "network_icon": "mxc://maunium.net/teamsicon"},
                        "login_flows": [
                            {"id": "password", "name": "Password", "description": "Type it"},
                            {"id": "qr", "name": "QR", "description": "Scan it"}],
                        "logins": fake.logins})
                if parts[:2] == ["login", "start"]:
                    n[0] += 1
                    pid = f"proc{n[0]}"
                    if parts[2] == "password":
                        step = {"type": "user_input", "step_id": "pw", "txn_id": f"bls_{pid}a",
                                "instructions": "Your password",
                                "user_input": {"fields": [{"type": "password", "id": "password",
                                                           "name": "Password"}]}}
                    else:
                        step = {"type": "display_and_wait", "step_id": "qr",
                                "txn_id": f"bls_{pid}a", "instructions": "Scan",
                                "display_and_wait": {"type": "qr", "data": "QR-1"}}
                    fake.procs[pid] = {"step": step, "cancelled": False, "qrs": 1}
                    return self._answer(200, {"login_id": pid, **step})
                if parts[:2] == ["login", "step"]:
                    pid, step_id, kind = parts[2], parts[3], parts[4]
                    pr = fake.procs.get(pid)
                    if pr is None:
                        return self._answer(404, {"errcode": "M_NOT_FOUND",
                                                  "error": "Login not found"})
                    cur = pr["step"]
                    if cur["step_id"] != step_id or cur["txn_id"] != q.get("txn_id") \
                            or cur["type"] != kind:
                        return self._answer(400, {"errcode": "M_BAD_STATE",
                                                  "error": "Transaction ID does not match"})
                    if kind == "user_input":
                        if (body or {}).get("password") != "right":
                            del fake.procs[pid]
                            return self._answer(403, {"errcode": "M_FORBIDDEN",
                                                      "error": "Wrong password"})
                    elif kind == "display_and_wait":
                        while not fake.release.wait(0.05):
                            if pr["cancelled"]:
                                return self._answer(410, {
                                    "errcode": "FI.MAU.BRIDGE.LOGIN_CANCELLED",
                                    "error": "Login process was cancelled"})
                        if pr["qrs"] <= fake.qr_changes:
                            pr["qrs"] += 1
                            fake.release.clear()
                            pr["step"] = {**cur, "txn_id": f"bls_{pid}{pr['qrs']}",
                                          "display_and_wait": {"type": "qr",
                                                               "data": f"QR-{pr['qrs']}"}}
                            return self._answer(200, {"login_id": pid, **pr["step"]})
                    done = {"type": "complete", "step_id": "done", "txn_id": f"bls_{pid}z",
                            "instructions": "Signed in",
                            "complete": {"user_login_id": "L2"}}
                    del fake.procs[pid]
                    fake.logins.append({"id": "L2", "name": "new@example.com",
                                        "state": {"state_event": "CONNECTED",
                                                  "timestamp": 1792339300}})
                    return self._answer(200, {"login_id": pid, **done})
                if parts[:2] == ["login", "cancel"]:
                    pr = fake.procs.pop(parts[2], None)
                    if pr is None:
                        return self._answer(404, {"errcode": "M_NOT_FOUND",
                                                  "error": "Login not found"})
                    pr["cancelled"] = True
                    return self._answer(200, {})
                if parts[0] == "logout":
                    before = len(fake.logins)
                    fake.logins = [x for x in fake.logins if x["id"] != parts[1]]
                    if len(fake.logins) == before:
                        return self._answer(404, {"errcode": "M_NOT_FOUND",
                                                  "error": "Login not found"})
                    return self._answer(200, {})
                return self._answer(404, {"errcode": "M_UNRECOGNIZED"})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.release.set()
        self.srv.shutdown()
        self.srv.server_close()

    def steps(self, kind):
        return [c for c in self.calls if c[1].endswith(f"/{kind}")]


def _config(appservice_id, port, secret, homeserver="http://127.0.0.1:9", hostname="127.0.0.1"):
    """A mautrix-shaped config: comments, lists, a block scalar, quoting."""
    return f"""\
# Homeserver details.
homeserver:
    # The address that this appservice can use to connect to the homeserver.
    address: {homeserver}
    domain: test
network:
    displayname_template: '{{{{.DisplayName}}}}'
    hosts:
    - teams.microsoft.com
    - id: nested
      shared_secret: not-this-one
appservice:
    address: http://127.0.0.1:{port}
    hostname: {hostname}   # where it listens
    port: {port}
    id: {appservice_id}
    bot:
        username: {appservice_id}bot
        avatar: mxc://maunium.net/x
    as_token: "astok"
encryption:
    pickle_key: |
        id: not-this-either
        port: 1
provisioning:
    # Shared secret for authentication.
    shared_secret: "{secret}"
    allow_matrix_auth: true
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    for d in ("home", "config", "cache"):
        (tmp_path / d).mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MATRIX_OWNER_ID", OWNER)
    # Nothing found unless a test writes configs.
    monkeypatch.setenv("MATRIX_BRIDGES", str(tmp_path / "none" / "*.yaml"))
    bridges._reset_for_tests()
    yield
    auth._REQ.__dict__.clear()
    bridges._reset_for_tests()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = Fake()
    down = _free_port()
    d = tmp_path / "share"
    for name, text in {
        "mautrix-teams": _config("msteams", f.port, SECRET,
                                 homeserver=f"http://127.0.0.1:{f.port}"),
        "mautrix-down": _config("aaadown", down, "another-secret-1234"),
        "mautrix-nosecret": _config("nosecret", f.port, "generate"),
    }.items():
        (d / name).mkdir(parents=True)
        (d / name / "config.yaml").write_text(text)
    monkeypatch.setenv("MATRIX_BRIDGES", str(d / "mautrix-*" / "config.yaml"))
    monkeypatch.setenv("MATRIX_OWNER_TOKEN", "owner-tok")
    yield f
    f.close()


def _poll(server, path, until, timeout=5.0):
    end = time.monotonic() + timeout
    while True:
        res, obj = call(server, "GET", path, headers=AUTH)
        if until(obj) or time.monotonic() > end:
            return res, obj
        time.sleep(0.05)


# --- the config reader ------------------------------------------------------------

def test_the_reader_takes_only_the_mappings():
    got = bridges.read_config(_config("msteams", 29340, "a 'quoted' # secret"))
    assert got["appservice.id"] == "msteams"
    assert got["appservice.port"] == "29340"
    assert got["appservice.hostname"] == "127.0.0.1"
    assert got["appservice.bot.username"] == "msteamsbot"
    assert got["provisioning.shared_secret"] == "a 'quoted' # secret"
    assert got["provisioning.allow_matrix_auth"] == "true"
    assert got["homeserver.address"] == "http://127.0.0.1:9"
    assert got["network.displayname_template"] == "{{.DisplayName}}"
    # Nothing from inside a list or a block scalar.
    assert not any("nested" in v or k.endswith(".id") and k != "appservice.id"
                   for k, v in got.items())
    assert "encryption.pickle_key" not in got and "provisioning.port" not in got


def test_the_reader_quotes_and_lists_at_the_keys_indent():
    text = ("a:\n    b: 'it''s'\n    c: \"x\\\"y\" # note\nlist:\n- one\n- two: 2\n"
            "after: plain value # comment\n\"*\": star\n")
    got = bridges.read_config(text)
    assert got == {"a.b": "it's", "a.c": 'x"y', "after": "plain value", "*": "star"}


def test_discovery_skips_a_config_without_a_secret(fake):
    found = {b.id: b for b in bridges.discover()}
    assert set(found) == {"msteams", "aaadown"}
    assert found["msteams"].url == f"http://127.0.0.1:{fake.port}"
    assert found["msteams"].secret == SECRET


def test_a_wildcard_host_is_asked_on_loopback(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text(_config("x", 1234, "secret-secret-1234", hostname="0.0.0.0"))
    monkeypatch.setenv("MATRIX_BRIDGES", f"{tmp_path / 'missing.yaml'}, {p}")
    assert [b.url for b in bridges.discover()] == ["http://127.0.0.1:1234"]


# --- the listing ------------------------------------------------------------------

def test_no_bridges_is_an_empty_list(server, signed_in):
    res, obj = call(server, "GET", "/bridges", headers=AUTH)
    assert res.status == 200 and obj == {"ok": True, "bridges": []}


def test_the_listing_with_an_unreachable_bridge(server, signed_in, fake):
    res, obj = call(server, "GET", "/bridges", headers=AUTH)
    assert res.status == 200, obj
    down, teams = obj["bridges"]          # name order: "aaadown" < "Microsoft Teams"
    assert down["id"] == "aaadown" and down["reachable"] is False and down["error"]
    assert down["flows"] == [] and down["logins"] == [] and down["icon"] is None
    assert teams == {
        "id": "msteams", "name": "Microsoft Teams", "network": "teams",
        "icon": "/bridges/msteams/icon", "reachable": True, "error": None,
        "flows": [{"id": "password", "name": "Password", "description": "Type it"},
                  {"id": "qr", "name": "QR", "description": "Scan it"}],
        "logins": [{"id": "L1", "name": "david@example.com", "state": "BAD_CREDENTIALS",
                    "error": "teams-token-expired", "message": "Sign in again",
                    "since": 1792339271}]}
    assert SECRET not in json.dumps(obj)


def test_the_icon_comes_through_the_homeserver_and_is_kept(server, signed_in, fake):
    res, body = call(server, "GET", "/bridges/msteams/icon", headers=AUTH)
    assert res.status == 200 and body == PNG
    assert res.getheader("Content-Type") == "image/png"
    # An <img> passes the bearer in the query.
    res, body = call(server, "GET", "/bridges/msteams/icon?access_token=tok")
    assert res.status == 200 and body == PNG
    media = [c for c in fake.calls if "/media/" in c[1]]
    assert len(media) == 1 and media[0][1].endswith("/maunium.net/teamsicon")
    assert call(server, "GET", "/bridges/aaadown/icon", headers=AUTH)[0].status == 404


# --- signing in -------------------------------------------------------------------

def test_a_form_step_signs_in(server, signed_in, fake):
    res, obj = call(server, "POST", "/bridges/msteams/login", {"flow": "password"},
                    headers=AUTH)
    assert res.status == 200, obj
    login = obj["login"]
    assert login["status"] == "step" and login["bridge"] == "msteams" and login["error"] is None
    assert login["id"] == "proc1" and "login_id" not in login["step"]
    assert login["step"]["user_input"]["fields"][0]["id"] == "password"
    path = f"/bridges/msteams/login/{login['id']}"
    res, again = call(server, "GET", path, headers=AUTH)
    assert res.status == 200 and again["login"] == login
    res, obj = call(server, "POST", path + "/step",
                    {"txn_id": login["step"]["txn_id"], "values": {"password": "right"}},
                    headers=AUTH)
    assert res.status == 200, obj
    assert obj["login"]["status"] == "complete"
    assert obj["login"]["step"]["complete"] == {"user_login_id": "L2"}
    sent = fake.steps("user_input")[0]
    assert sent[2]["txn_id"] == "bls_proc1a" and sent[3] == {"password": "right"}


def test_a_wrong_password_fails_the_process(server, signed_in, fake):
    _, obj = call(server, "POST", "/bridges/msteams/login", {"flow": "password"}, headers=AUTH)
    login = obj["login"]
    res, obj = call(server, "POST", f"/bridges/msteams/login/{login['id']}/step",
                    {"txn_id": login["step"]["txn_id"], "values": {"password": "wrong"}},
                    headers=AUTH)
    assert res.status == 200
    assert obj["login"]["status"] == "failed" and obj["login"]["error"] == "Wrong password"


def test_a_stale_txn_is_409_with_the_process(server, signed_in, fake):
    _, obj = call(server, "POST", "/bridges/msteams/login", {"flow": "password"}, headers=AUTH)
    login = obj["login"]
    res, obj = call(server, "POST", f"/bridges/msteams/login/{login['id']}/step",
                    {"txn_id": "bls_old", "values": {"password": "right"}}, headers=AUTH)
    assert res.status == 409 and obj["error"] == "step moved on"
    assert obj["login"] == login
    assert fake.steps("user_input") == []


def test_a_qr_is_waited_on_by_the_server(server, signed_in, fake):
    fake.qr_changes = 1
    res, obj = call(server, "POST", "/bridges/msteams/login", {"flow": "qr"}, headers=AUTH)
    assert res.status == 200, obj
    login = obj["login"]
    assert login["status"] == "waiting"
    assert login["step"]["display_and_wait"] == {"type": "qr", "data": "QR-1"}
    path = f"/bridges/msteams/login/{login['id']}"
    # Not submitted by the app.
    res, _ = call(server, "POST", path + "/step",
                  {"txn_id": login["step"]["txn_id"], "values": {}}, headers=AUTH)
    assert res.status == 400
    # The wait is with the bridge; the app polls.
    _poll(server, path, lambda o: fake.steps("display_and_wait"))
    fake.release.set()
    res, obj = _poll(server, path, lambda o: o["login"]["step"]["display_and_wait"]["data"]
                     == "QR-2")
    assert obj["login"]["status"] == "waiting", obj
    assert obj["login"]["step"]["txn_id"] != login["step"]["txn_id"]
    _poll(server, path, lambda o: len(fake.steps("display_and_wait")) == 2)
    fake.release.set()
    res, obj = _poll(server, path, lambda o: o["login"]["status"] != "waiting")
    assert obj["login"]["status"] == "complete", obj
    assert [c[2]["txn_id"] for c in fake.steps("display_and_wait")] == \
        ["bls_proc1a", "bls_proc12"]


def test_cancel_ends_a_wait(server, signed_in, fake):
    _, obj = call(server, "POST", "/bridges/msteams/login", {"flow": "qr"}, headers=AUTH)
    pid = obj["login"]["id"]
    path = f"/bridges/msteams/login/{pid}"
    _poll(server, path, lambda o: fake.steps("display_and_wait"))
    res, obj = call(server, "POST", path + "/cancel", {}, headers=AUTH)
    assert res.status == 200 and obj["login"]["status"] == "cancelled"
    assert any(c[1].endswith(f"/login/cancel/{pid}") for c in fake.calls)
    time.sleep(0.3)   # the bridge's refusal of the wait changes nothing
    res, obj = call(server, "GET", path, headers=AUTH)
    assert obj["login"]["status"] == "cancelled" and obj["login"]["error"] is None


def test_start_refuses_an_unknown_bridge_or_flow(server, signed_in, fake):
    assert call(server, "POST", "/bridges/nope/login", {"flow": "qr"},
                headers=AUTH)[0].status == 404
    res, obj = call(server, "POST", "/bridges/msteams/login", {"flow": "sms"}, headers=AUTH)
    assert res.status == 400 and obj["error"] == "no such flow"
    res, obj = call(server, "POST", "/bridges/aaadown/login", {"flow": "qr"}, headers=AUTH)
    assert res.status == 502 and not obj["ok"]
    assert call(server, "GET", "/bridges/msteams/login/nosuch", headers=AUTH)[0].status == 404


def test_a_process_is_forgotten_after_its_last_activity(server, signed_in, fake):
    _, obj = call(server, "POST", "/bridges/msteams/login", {"flow": "password"}, headers=AUTH)
    path = f"/bridges/msteams/login/{obj['login']['id']}"
    for p in bridges._PROCS.values():
        p.touched -= bridges.PROC_TTL_S + 1
    assert call(server, "GET", path, headers=AUTH)[0].status == 404


def test_logout_answers_the_bridge(server, signed_in, fake):
    res, obj = call(server, "POST", "/bridges/msteams/logout/L1", {}, headers=AUTH)
    assert res.status == 200, obj
    assert obj["bridge"]["id"] == "msteams" and obj["bridge"]["logins"] == []
    assert call(server, "POST", "/bridges/msteams/logout/L1", {},
                headers=AUTH)[0].status == 404


# --- who ----------------------------------------------------------------------------

def test_only_the_owner(server, fake):
    tok = devices.enrol_account({"iss": "https://cms.test", "sub": "kat",
                                 "email": "k@x.test"}, "phone")["token"]
    for method, path in (("GET", "/bridges"), ("GET", "/bridges/msteams/icon"),
                         ("POST", "/bridges/msteams/login"),
                         ("GET", "/bridges/msteams/login/proc1"),
                         ("POST", "/bridges/msteams/login/proc1/step"),
                         ("POST", "/bridges/msteams/login/proc1/cancel"),
                         ("POST", "/bridges/msteams/logout/L1"),
                         ("POST", "/bridges/telegram/setup")):
        res, obj = call(server, method, path, {} if method == "POST" else None,
                        headers={"Authorization": f"Bearer {tok}"})
        assert res.status == 403 and obj["error"] == "bridges are the owner's", path
        res, _ = call(server, method, path, {} if method == "POST" else None)
        assert res.status in (401, 403), path
    assert [c for c in fake.calls if "logout" in c[1] or "login" in c[1]] == []


# --- streamlining: the code step's page, and the watch (19 Oct 2026) -------------

def test_a_code_step_names_the_page_to_open():
    step = {"type": "display_and_wait",
            "instructions": "Open https://login.microsoft.com/device in a browser and enter "
                            "the code below.\n\nThe code expires in 15 minutes.",
            "display_and_wait": {"type": "code", "data": "GGEDSKP64"}}
    assert bridges.open_url(step) == "https://login.microsoft.com/device"
    assert bridges.open_url({**step, "instructions": "Visit https://x.example/a."}) == "https://x.example/a"
    assert bridges.open_url({**step, "instructions": "Enter the code."}) is None
    assert bridges.open_url({**step, "display_and_wait": {"type": "qr", "data": "x"}}) is None
    assert bridges.open_url({"type": "user_input", "instructions": "https://x.example"}) is None
    assert bridges.open_url(None) is None


def test_the_watch_raises_a_sign_in_that_needs_you_and_clears_it():
    def entry(bid, name, states, reachable=True):
        return {"id": bid, "name": name, "reachable": reachable,
                "logins": [{"id": str(i), "name": f"acct{i}", "state": s,
                            "message": "Logged out" if s == "BAD_CREDENTIALS" else None}
                           for i, s in enumerate(states)]}

    got = bridges.watch_reports([
        entry("meta", "Facebook Messenger", ["BAD_CREDENTIALS"]),
        entry("instagram", "Instagram", ["CONNECTED"]),
        entry("msteams", "Microsoft Teams", []),
        entry("gmessages", "Google Messages", ["LOGGED_OUT"], reachable=False)])
    by = {b["id"]: b for b in got}
    assert set(by) == {"bridge.meta", "bridge.instagram", "bridge.msteams"}
    assert by["bridge.meta"]["level"] == "warn"
    assert by["bridge.meta"]["title"] == "Facebook Messenger needs you to sign in again"
    assert "acct0" in by["bridge.meta"]["detail"] and "Logged out" in by["bridge.meta"]["detail"]
    assert by["bridge.instagram"]["level"] == "ok"
    assert by["bridge.msteams"]["level"] == "ok"
    assert all(b["every_s"] == int(bridges.WATCH_INTERVAL_S * 3) for b in got)


# --- setting a bridge up: Telegram's own API keys (20 Oct 2026) -----------------

TG_HASH = "0123456789abcdef0123456789abcdef"


def _telegram(tmp_path, monkeypatch, api_id="12345", api_hash="tjyd5yge35lbodk1xwzw2jstp90k55qz"):
    d = tmp_path / "tg" / "mautrix-telegram"
    d.mkdir(parents=True)
    conf = d / "config.yaml"
    conf.write_text(f"""\
network:
    # Get your own API keys at https://my.telegram.org/apps
    api_id: {api_id}
    api_hash: {api_hash}
    device_info:
        device_model: mautrix-telegram
appservice:
    hostname: 127.0.0.1
    port: {_free_port()}
    id: telegram
provisioning:
    shared_secret: telegram-secret-1234
""")
    conf.chmod(0o600)
    monkeypatch.setenv("MATRIX_BRIDGES", str(conf))
    return conf


def test_a_bridge_without_its_keys_asks_for_them(server, signed_in, tmp_path, monkeypatch):
    _telegram(tmp_path, monkeypatch)
    res, obj = call(server, "GET", "/bridges", headers=AUTH)
    (tg,) = obj["bridges"]
    assert res.status == 200 and tg["id"] == "telegram" and tg["name"] == "Telegram"
    assert tg["reachable"] is False and tg["flows"] == [] and tg["error"] is None
    setup = tg["setup"]
    assert setup["needed"] is True and setup["url"] == "https://my.telegram.org/apps"
    assert [f["id"] for f in setup["fields"]] == ["api_id", "api_hash"]
    assert all(set(f) == {"id", "name", "type", "pattern"} for f in setup["fields"])
    assert "12345" not in json.dumps(tg), "a placeholder or a key is never sent back"


def test_setup_writes_the_keys_and_restarts_the_bridge(server, signed_in, tmp_path, monkeypatch):
    conf = _telegram(tmp_path, monkeypatch)
    restarted = []
    monkeypatch.setattr(bridges, "_restart", restarted.append)
    monkeypatch.setattr(bridges, "SETUP_WAIT_S", 0.0)
    res, obj = call(server, "POST", "/bridges/telegram/setup",
                    {"values": {"api_id": " 1234567 ", "api_hash": TG_HASH}}, AUTH)
    assert res.status == 200 and obj["ok"], obj
    text = conf.read_text()
    assert "    api_id: 1234567\n" in text and f"    api_hash: {TG_HASH}\n" in text
    assert "device_model: mautrix-telegram" in text and "# Get your own API keys" in text
    assert (conf.stat().st_mode & 0o777) == 0o600
    assert restarted == ["mautrix-telegram.service"]
    # Set up now: asked like any bridge (not answering yet here), the keys never shown.
    assert obj["bridge"].get("setup", {}).get("needed") is not True
    assert TG_HASH not in json.dumps(obj)
    assert bridges.find("telegram").unset == ()


def test_setup_refuses_what_is_not_a_key(server, signed_in, tmp_path, monkeypatch):
    conf = _telegram(tmp_path, monkeypatch)
    before = conf.read_text()
    monkeypatch.setattr(bridges, "_restart", lambda unit: pytest.fail("restarted"))
    for values in ({"api_id": "", "api_hash": TG_HASH}, {"api_id": "12x", "api_hash": TG_HASH},
                   {"api_id": "123456", "api_hash": "short"}):
        res, obj = call(server, "POST", "/bridges/telegram/setup", {"values": values}, AUTH)
        assert res.status == 400 and not obj["ok"], values
    assert conf.read_text() == before
    res, obj = call(server, "POST", "/bridges/msteams/setup", {"values": {}}, AUTH)
    assert res.status == 404


def test_config_values_are_set_in_their_section_only():
    text = "network:\n    api_id: 1\n    nested:\n        api_id: 2\nother:\n    api_id: 3\n"
    out = bridges.set_config_values(text, {("network", "api_id"): "99"})
    assert out == "network:\n    api_id: 99\n    nested:\n        api_id: 2\nother:\n    api_id: 3\n"
    with pytest.raises(KeyError):
        bridges.set_config_values(text, {("network", "nope"): "1"})
