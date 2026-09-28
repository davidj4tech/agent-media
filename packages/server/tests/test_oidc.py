"""Account sign-in: an ID token for a device token (oidc.py, POST /enrol).

Real RS256 tokens from a key made here, checked by the standard-library
verifier; the issuer's discovery document and JWKS are stood in for, so no
network. The accounts proposal, steps 2 and 3.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from agent_media_server import devices, oidc  # noqa: E402

from test_contract import call, server, shelf, typed  # noqa: E402,F401

ISS = "https://cms.sasonica.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _jwk(key, kid="k1"):
    n = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig",
            "n": _b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
            "e": _b64(n.e.to_bytes(3, "big"))}


def token(key=KEY, kid="k1", alg="RS256", **claims):
    now = int(time.time())
    body = {"iss": ISS, "aud": "sasonica-app", "sub": "42", "email": "owner@example.com",
            "email_verified": True, "iat": now, "exp": now + 300} | claims
    h = _b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    p = _b64(json.dumps(body).encode())
    sig = key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{_b64(sig)}"


@pytest.fixture(autouse=True)
def issuer(monkeypatch):
    monkeypatch.setenv("MEDIA_OIDC_ISSUERS", ISS)
    monkeypatch.setenv("MEDIA_OIDC_CLIENTS", "sasonica-app")
    monkeypatch.setenv("MEDIA_OIDC_ALLOW", f"{ISS}|email=owner@example.com, {ISS}|99")
    monkeypatch.setenv("MEDIA_OIDC_ENROL", f"{ISS}|email=owner@example.com")
    fetched = []

    def fake(url, timeout=8.0):
        fetched.append(url)
        if url == f"{ISS}/.well-known/openid-configuration":
            return {"issuer": ISS, "jwks_uri": f"{ISS}/oauth/jwks"}
        if url == f"{ISS}/oauth/jwks":
            return {"keys": [_jwk(KEY)]}
        raise OSError("no such page")
    monkeypatch.setattr(oidc, "_fetch_json", fake)
    oidc._reset_for_tests()
    return fetched


def test_a_good_token_verifies_and_the_keys_are_cached(issuer):
    claims = oidc.verify(token())
    assert claims["sub"] == "42" and claims["iss"] == ISS
    oidc.verify(token())
    assert len(issuer) == 2, "discovery and JWKS fetched once, then cached"


@pytest.mark.parametrize("bad, why", [
    (dict(key=OTHER), "bad signature"),
    (dict(iss="https://evil.example"), "not a trusted issuer"),
    (dict(aud="someone-else"), "not for this client"),
    (dict(exp=int(time.time()) - 3600), "expired"),
    (dict(alg="HS256"), "unsupported alg"),
    (dict(sub=""), "no subject"),
])
def test_refusals(bad, why):
    with pytest.raises(oidc.OidcError) as e:
        oidc.verify(token(**bad))
    assert e.value.code == "bad_id_token" and why in str(e.value)


def test_a_key_with_no_kid_matches(monkeypatch):
    """Simple OAuth's JWKS has one key and no kid."""
    jwk = _jwk(KEY)
    del jwk["kid"]
    monkeypatch.setattr(oidc, "_fetch_json", lambda url, timeout=8.0: (
        {"issuer": ISS, "jwks_uri": f"{ISS}/.well-known/jwks.json"} if "openid-configuration" in url
        else {"keys": [jwk]}))
    oidc._reset_for_tests()
    assert oidc.verify(token(kid="whatever"))["sub"] == "42"


def test_a_tampered_payload_fails_the_signature():
    h, p, s = token().split(".")
    claims = json.loads(base64.urlsafe_b64decode(p + "=="))
    claims["sub"] = "1"
    p2 = _b64(json.dumps(claims).encode())
    with pytest.raises(oidc.OidcError):
        oidc.verify(f"{h}.{p2}.{s}")


def test_allow_and_enrol_rules():
    owner = oidc.verify(token())
    assert oidc.allowed(owner) and oidc.may_enrol(owner)
    by_sub = oidc.verify(token(sub="99", email="x@example.com"))
    assert oidc.allowed(by_sub) and not oidc.may_enrol(by_sub)
    stranger = oidc.verify(token(sub="7", email="stranger@example.com"))
    assert not oidc.allowed(stranger)
    unverified = oidc.verify(token(sub="8", email_verified=False))
    assert not oidc.allowed(unverified), "an unverified email counts for nothing"


# --- POST /enrol ------------------------------------------------------------------

def test_enrol_trades_a_sign_in_for_a_device_token(server):
    res, obj = call(server, "POST", "/enrol", {"id_token": token(), "device": "Pixel 8a"})
    assert res.status == 200, obj
    assert obj["enrol"] is True and obj["account"] == "owner@example.com" and len(obj["token"]) == 43
    row = next(d for d in devices.list_devices() if d["id"] == obj["device_id"])
    assert row["iss"] == ISS and row["sub"] == "42" and row["account"] == "owner@example.com"
    # The token works like a paired one.
    res, _ = call(server, "GET", "/targets", headers={"Authorization": f"Bearer {obj['token']}"})
    assert res.status == 200


def test_enrol_refuses_a_stranger_and_a_bad_token(server):
    res, obj = call(server, "POST", "/enrol", {"id_token": token(sub="7", email="s@example.com")})
    assert res.status == 403 and obj["code"] == "not_enrolled"
    res, obj = call(server, "POST", "/enrol", {"id_token": token(key=OTHER)})
    assert res.status == 403 and obj["code"] == "bad_id_token"


def test_enrol_is_off_without_configuration(server, monkeypatch):
    monkeypatch.delenv("MEDIA_OIDC_ISSUERS")
    res, obj = call(server, "POST", "/enrol", {"id_token": token()})
    assert res.status == 404 and obj["code"] == "no_accounts"


def test_revoking_an_account_drops_its_devices(server):
    call(server, "POST", "/enrol", {"id_token": token(), "device": "phone"})
    call(server, "POST", "/enrol", {"id_token": token(), "device": "tablet"})
    assert devices.revoke_account("owner@example.com") == 2
    assert devices.cli_devices(["--revoke-account", "42"]) == 1, "nothing left"


def test_get_enrol_says_where_to_sign_in(server, monkeypatch):
    real = oidc._fetch_json
    monkeypatch.setattr(oidc, "_fetch_json", lambda url, timeout=8.0: (
        {"issuer": ISS, "jwks_uri": f"{ISS}/oauth/jwks", "authorization_endpoint": f"{ISS}/oauth/authorize",
         "token_endpoint": f"{ISS}/oauth/token"} if "openid-configuration" in url else real(url, timeout)))
    oidc._reset_for_tests()
    res, obj = call(server, "GET", "/enrol")
    assert res.status == 200
    assert obj["accounts"] == [{"issuer": ISS, "authorization_endpoint": f"{ISS}/oauth/authorize",
                                "client_id": "sasonica-app", "scopes": "openid email profile"}]


def test_enrol_with_a_code_swaps_it_at_the_issuer(server, monkeypatch):
    seen = {}

    class Resp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=10):
        seen["url"], seen["body"] = req.full_url, req.data.decode()
        return Resp(json.dumps({"id_token": token(preferred_username="david"), "access_token": "x",
                                "refresh_token": "rt-1"}).encode())
    monkeypatch.setattr(oidc, "_fetch_json", lambda url, timeout=8.0: (
        {"issuer": ISS, "jwks_uri": f"{ISS}/oauth/jwks", "token_endpoint": f"{ISS}/oauth/token"}
        if "openid-configuration" in url else {"keys": [_jwk(KEY)]}))
    monkeypatch.setattr(oidc.urllib.request, "urlopen", fake_urlopen)
    oidc._reset_for_tests()
    res, obj = call(server, "POST", "/enrol", {"code": "abc", "code_verifier": "v" * 43,
                                              "redirect_uri": "sasonica://auth", "device": "phone"})
    assert res.status == 200, obj
    assert seen["url"] == f"{ISS}/oauth/token"
    assert "code_verifier=" + "v" * 43 in seen["body"] and "client_id=sasonica-app" in seen["body"]
    assert "client_secret" not in seen["body"], "a public client"
    assert obj["username"] == "david"
    assert devices.refresh_token_of(obj["device_id"])[1] == "rt-1", "kept, privately"
    assert all("rt" not in d for d in devices.list_devices()), "never listed"


def test_me_says_which_account_a_device_is(server):
    res, obj = call(server, "POST", "/enrol", {"id_token": token(), "device": "phone"})
    res, me = call(server, "GET", "/me", headers={"Authorization": f"Bearer {obj['token']}"})
    assert res.status == 200
    assert me["account"] == "owner@example.com" and me["issuer"] == ISS and me["enrol"] is True
    res, _ = call(server, "GET", "/me", headers={"Authorization": "Bearer nope"})
    assert res.status == 401


def test_me_refreshes_an_old_profile_from_the_issuer(server, monkeypatch):
    """An email or name changed at the issuer shows on the next /me."""
    import time as _t
    from agent_media_server import app as app_mod
    got = devices.enrol_account({"iss": ISS, "sub": "1", "email": "old@example.com"}, "phone",
                                refresh_token="rt-1", username="david")
    devices.update_profile(got["device_id"], "old@example.com", "david", "rt-1")
    # Make it old.
    rows = devices._load()
    for d in rows:
        if d["id"] == got["device_id"]:
            d["profile_at"] = _t.time() - 7200
    devices._save(rows)
    calls = []
    monkeypatch.setattr(oidc, "profile", lambda iss, rt: (calls.append(rt) or
                        ({"sub": "1", "email": "new@example.com", "preferred_username": "david2"}, "rt-2")))
    monkeypatch.setattr(app_mod, "_in_background", lambda fn: fn())
    res, me = call(server, "GET", "/me", headers={"Authorization": f"Bearer {got['token']}"})
    assert res.status == 200 and me["account"] == "old@example.com", "this answer is what was there"
    res, me = call(server, "GET", "/me", headers={"Authorization": f"Bearer {got['token']}"})
    assert me["account"] == "new@example.com" and me["username"] == "david2"
    assert calls == ["rt-1"], "asked once; fresh now"
    assert devices.refresh_token_of(got["device_id"])[1] == "rt-2", "the rotated refresh token kept"
