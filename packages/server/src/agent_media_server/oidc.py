"""Account sign-in: checking an OpenID Connect ID token (accounts proposal, step 2).

An account enrols a device; it does not authenticate requests. The app signs
in with an account once, sends the ID token to `POST /enrol`, and gets the
same device token `POST /pair` gives — after which the issuer is never asked
again, so an outage there stops new enrolments and nothing else
(docs/proposals/2026-09-23-accounts-and-the-identity-seam.md).

Configuration, not code (David, 29 Sep 2026: Drupal on cms.sasonica.com is
the issuer):

    MEDIA_OIDC_ISSUERS    https://cms.sasonica.com[, …]   trusted issuers, exact `iss`
    MEDIA_OIDC_CLIENTS    sasonica-app[, …]                the `aud` an ID token must carry
    MEDIA_OIDC_ALLOW      <iss>|<sub>, <iss>|email=<addr>, …   who may enrol here
    MEDIA_OIDC_ENROL      the same form: who gets the enrol bit (may pair others)

An issuer is trusted to say who someone is; MEDIA_OIDC_ALLOW says whether that
someone may have a device on *this* server. Nothing is allowed by default.

RS256 only, checked with the standard library: a signature is `s^e mod n`,
compared with the PKCS#1 v1.5 encoding of the SHA-256 digest. The discovery
document and JWKS are cached an hour; a cold cache is the only network call,
and failing it is a refusal, never a 500.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request

#: DER prefix of a SHA-256 DigestInfo (PKCS#1 v1.5, RFC 8017 §9.2).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
CACHE_S = 3600.0
#: Clock skew allowed on exp/iat/nbf.
LEEWAY_S = 60

_LOCK = threading.Lock()
_JWKS: dict[str, tuple[float, list[dict]]] = {}


class OidcError(Exception):
    """A refusal. `code` is the answer's code: bad_id_token or not_enrolled."""

    def __init__(self, code: str, why: str):
        super().__init__(why)
        self.code = code


def _list(name: str) -> list[str]:
    return [x.strip() for x in (os.environ.get(name) or "").split(",") if x.strip()]


def issuers() -> list[str]:
    return [i.rstrip("/") for i in _list("MEDIA_OIDC_ISSUERS")]


def configured() -> bool:
    return bool(issuers() and _list("MEDIA_OIDC_CLIENTS"))


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _fetch_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "agent-media oidc"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _keys(iss: str, *, refresh: bool = False) -> list[dict]:
    """The issuer's signing keys, through its discovery document, cached."""
    now = time.time()
    with _LOCK:
        got = _JWKS.get(iss)
        if got and not refresh and now - got[0] < CACHE_S:
            return got[1]
    try:
        disc = _fetch_json(f"{iss}/.well-known/openid-configuration")
        if str(disc.get("issuer") or "").rstrip("/") != iss:
            raise OidcError("bad_id_token", "the issuer's discovery document names another issuer")
        keys = _fetch_json(str(disc["jwks_uri"])).get("keys") or []
    except OidcError:
        raise
    except Exception as e:  # noqa: BLE001 — an unreachable issuer is a refusal
        raise OidcError("bad_id_token", f"could not reach the issuer: {e}") from e
    with _LOCK:
        _JWKS[iss] = (now, keys)
    return keys


def _rs256_ok(jwk: dict, signed: bytes, sig: bytes) -> bool:
    n = int.from_bytes(_b64(str(jwk.get("n") or "")), "big")
    e = int.from_bytes(_b64(str(jwk.get("e") or "")), "big")
    k = (n.bit_length() + 7) // 8
    if not n or not e or len(sig) != k or n.bit_length() < 2048:
        return False
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    t = _SHA256_PREFIX + hashlib.sha256(signed).digest()
    want = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, want)


def verify(token: str, *, now: float | None = None) -> dict:
    """The claims of a valid ID token from a trusted issuer, for a known client.
    Raises OidcError("bad_id_token") otherwise."""
    now = time.time() if now is None else now
    try:
        h, p, s = str(token).split(".")
        header, claims, sig = json.loads(_b64(h)), json.loads(_b64(p)), _b64(s)
    except (ValueError, TypeError) as e:
        raise OidcError("bad_id_token", "not a JWT") from e
    if header.get("alg") != "RS256":
        raise OidcError("bad_id_token", f"unsupported alg {header.get('alg')!r}")
    iss = str(claims.get("iss") or "").rstrip("/")
    if iss not in issuers():
        raise OidcError("bad_id_token", "not a trusted issuer")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if not set(map(str, auds)) & set(_list("MEDIA_OIDC_CLIENTS")):
        raise OidcError("bad_id_token", "not for this client")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp + LEEWAY_S < now:
        raise OidcError("bad_id_token", "expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and nbf - LEEWAY_S > now:
        raise OidcError("bad_id_token", "not valid yet")
    if not str(claims.get("sub") or ""):
        raise OidcError("bad_id_token", "no subject")
    signed = f"{h}.{p}".encode()
    kid = header.get("kid")

    def matching(keys):
        return [k for k in keys if k.get("kty") == "RSA" and (not kid or k.get("kid") == kid)]

    keys = matching(_keys(iss))
    if not keys and kid:
        keys = matching(_keys(iss, refresh=True))   # a rotated key
    if not any(_rs256_ok(k, signed, sig) for k in keys):
        raise OidcError("bad_id_token", "bad signature")
    return claims | {"iss": iss}


def _matches(claims: dict, rules: list[str]) -> bool:
    iss, sub = claims.get("iss"), str(claims.get("sub") or "")
    email = str(claims.get("email") or "").lower()
    verified = claims.get("email_verified") is not False
    for rule in rules:
        r_iss, _, who = rule.partition("|")
        if r_iss.rstrip("/") != iss:
            continue
        if who.startswith("email="):
            if verified and email and email == who[6:].strip().lower():
                return True
        elif who == sub:
            return True
    return False


def allowed(claims: dict) -> bool:
    """May this account have a device on this server (MEDIA_OIDC_ALLOW)?"""
    return _matches(claims, _list("MEDIA_OIDC_ALLOW"))


def may_enrol(claims: dict) -> bool:
    """Does this account's device get the enrol bit (MEDIA_OIDC_ENROL)?"""
    return _matches(claims, _list("MEDIA_OIDC_ENROL"))


def _reset_for_tests() -> None:
    with _LOCK:
        _JWKS.clear()
