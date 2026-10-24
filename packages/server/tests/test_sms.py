"""media sms send: one text through the Google Messages bridge. No real bridge."""
import os

import pytest

from agent_media_server import sms


def test_the_bridge_sender_starts_the_chat_joins_and_sends(monkeypatch, tmp_path):
    from agent_media_server import bridges
    env = tmp_path / "matrix.env"
    env.write_text("MATRIX_HOMESERVER=https://hs.invalid\nMATRIX_OWNER_TOKEN=tok\nMATRIX_OWNER_ID=@d:hs.invalid\n")
    for k in list(os.environ):
        if k.startswith("MATRIX_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bridges, "find", lambda bid: bridges.Bridge(
        id=bid, url="http://bridge.invalid", secret="s3", homeserver="", config=""))
    calls = []

    def http(method, url, token, body, what, timeout=60):
        calls.append((method, url, token, body))
        return {"dm_room_mxid": "!r:hs.invalid"} if "create_dm" in url else {"event_id": "$e"}
    monkeypatch.setattr(sms.GmessagesSender, "_http", staticmethod(http))
    got = sms.GmessagesSender({**sms.DEFAULTS, "matrix_env": str(env)}).send("+61400999888", "Hi", "txn-1")
    assert got == "$e"
    assert calls[0][:3] == ("POST", "http://bridge.invalid/_matrix/provision/v3/create_dm/%2B61400999888"
                                    "?user_id=%40d%3Ahs.invalid", "s3")
    assert calls[1][:3] == ("POST", "https://hs.invalid/_matrix/client/v3/rooms/%21r%3Ahs.invalid/join", "tok")
    assert calls[2] == ("PUT", "https://hs.invalid/_matrix/client/v3/rooms/%21r%3Ahs.invalid/send/"
                               "m.room.message/txn-1", "tok", {"msgtype": "m.text", "body": "Hi"})


def test_the_bridge_sender_without_a_login_says_so(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("MATRIX_"):
            monkeypatch.delenv(k, raising=False)
    with pytest.raises(sms.SmsError, match="matrix-owner-login"):
        sms.GmessagesSender({**sms.DEFAULTS, "matrix_env": str(tmp_path / "none")}).send("+61400999888", "Hi", "t")


def test_send_wants_an_international_number_and_a_body_and_never_echoes_the_number(monkeypatch):
    sent = []
    monkeypatch.setattr(sms, "_sender", lambda cfg: type("S", (), {
        "send": lambda self, to, body, txn: sent.append((to, body, txn)) or "$e"})())
    with pytest.raises(sms.SmsError) as e:
        sms.send("0400999888", "Hi")
    assert "0400999888" not in str(e.value)
    with pytest.raises(sms.SmsError):
        sms.send("+61400999888", "  ")
    got = sms.send("+61400999888", "Hi", "txn-9")
    assert got == {"ok": True, "event_id": "$e", "txn": "txn-9"}
    assert sms.send("+61400999888", "Hi")["txn"].startswith("sms-")
