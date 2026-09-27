"""Sasonica Shell sign-ins approved on the phone (shell_signin.py)."""

import hashlib
import hmac

import pytest

from agent_media_server import auth, shell_signin

SID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def shell(tmp_path, monkeypatch):
    (tmp_path / "relay.key").write_text("ab" * 32 + "\n")
    (tmp_path / "env").write_text(
        "SASONICA_WORKER_URL=https://w.example.workers.dev\n"
        "SASONICA_RUNNER_TOKEN=runner-token\n"
        f"SASONICA_KEY_FILE={tmp_path / 'relay.key'}\n")
    monkeypatch.setenv("SASONICA_CONF", str(tmp_path))
    monkeypatch.setattr(auth, "may_control_speech", lambda b: (True, {}))
    monkeypatch.setattr(auth, "may_enrol", lambda b: ({"device": "d"}, {}))
    shell_signin._reset_for_tests()
    calls = []

    def fake(sh, body, timeout=8.0):
        calls.append((sh, body))
        if body["op"] == "signins":
            return {"signins": [{"id": SID, "code": "ABC234", "client_name": "Claude",
                                 "client_host": "claude.ai", "created_at": "2026-09-27 04:00:00"},
                                {"id": "not-an-id"}]}
        return {"id": body["id"], "status": "approved" if body["approve"] else "denied"}
    monkeypatch.setattr(shell_signin, "_runner", fake)
    return calls


def test_waiting_lists_them_and_drops_malformed_rows(shell):
    rows = shell_signin.waiting()
    assert rows == [{"id": SID, "code": "ABC234", "client": "Claude", "host": "claude.ai",
                     "at": "2026-09-27 04:00:00"}]
    shell_signin.waiting()
    assert len(shell) == 1, "reused for a few seconds"
    assert shell[0][0]["url"] == "https://w.example.workers.dev"


def test_a_decision_is_signed_with_the_relay_key(shell):
    ok, detail = shell_signin.decide(SID, True, "bearer")
    assert ok and detail["status"] == "approved"
    body = [b for _s, b in shell if b["op"] == "signin-decide"][0]
    want = hmac.new(("ab" * 32).encode(), f"signin\n{SID}\napprove".encode(), hashlib.sha256).hexdigest()
    assert body["sig"] == want and body["approve"] is True
    ok, detail = shell_signin.decide(SID, False, "bearer")
    deny = [b for _s, b in shell if b["op"] == "signin-decide"][-1]
    assert deny["approve"] is False and deny["sig"] != want


def test_refusals(shell, monkeypatch, tmp_path):
    assert shell_signin.decide("nope", True, "b")[1]["status"] == 400
    monkeypatch.setattr(auth, "may_enrol", lambda b: (None, {"error": "not_enrolled", "status": 403}))
    assert shell_signin.decide(SID, True, "b")[1]["status"] == 403, "approving takes a device that may enrol"
    monkeypatch.setattr(auth, "may_enrol", lambda b: ({"device": "d"}, {}))
    (tmp_path / "env").write_text("")
    shell_signin._reset_for_tests()
    assert shell_signin.waiting() == [], "no shell here: nothing waiting"
    assert shell_signin.decide(SID, True, "b")[1]["status"] == 409


def test_connect_info_is_the_plain_url_only_with_signin_on(shell, tmp_path):
    ok, d = shell_signin.connect_info("b")
    assert ok and d == {"shell": True, "url": None, "signin": None, "secret_url": None}, "OAuth off, no secret"
    with open(tmp_path / "env", "a") as fh:
        fh.write("SASONICA_SIGNIN=app\nSASONICA_URL_SECRET=five-word-secret\n")
    ok, d = shell_signin.connect_info("b")
    assert d == {"shell": True, "url": "https://w.example.workers.dev/mcp", "signin": "app",
                 "secret_url": "https://w.example.workers.dev/five-word-secret/mcp"}


def test_the_secret_url_goes_only_to_a_device_that_may_enrol(shell, tmp_path, monkeypatch):
    with open(tmp_path / "env", "a") as fh:
        fh.write("SASONICA_URL_SECRET=five-word-secret\n")
    monkeypatch.setattr(auth, "may_enrol", lambda b: (None, {"error": "not_enrolled", "status": 403}))
    ok, d = shell_signin.connect_info("b")
    assert ok and d["secret_url"] is None
