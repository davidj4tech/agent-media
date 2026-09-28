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
