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
import urllib.parse
import urllib.request

#: DER prefix of a SHA-256 DigestInfo (PKCS#1 v1.5, RFC 8017 §9.2).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
CACHE_S = 3600.0
#: Clock skew allowed on exp/iat/nbf.
LEEWAY_S = 60

_LOCK = threading.Lock()
_JWKS: dict[str, tuple[float, list[dict]]] = {}
_DISC: dict[str, tuple[float, dict]] = {}


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


def discovery(iss: str) -> dict:
    """The issuer's discovery document, cached an hour."""
    now = time.time()
    with _LOCK:
        got = _DISC.get(iss)
        if got and now - got[0] < CACHE_S:
            return got[1]
    try:
        disc = _fetch_json(f"{iss}/.well-known/openid-configuration")
    except Exception as e:  # noqa: BLE001 — an unreachable issuer is a refusal
        raise OidcError("bad_id_token", f"could not reach the issuer: {e}") from e
    if str(disc.get("issuer") or "").rstrip("/") != iss:
        raise OidcError("bad_id_token", "the issuer's discovery document names another issuer")
    with _LOCK:
        _DISC[iss] = (now, disc)
    return disc


def _keys(iss: str, *, refresh: bool = False) -> list[dict]:
    """The issuer's signing keys, through its discovery document, cached."""
    now = time.time()
    with _LOCK:
        got = _JWKS.get(iss)
        if got and not refresh and now - got[0] < CACHE_S:
            return got[1]
    try:
        disc = discovery(iss)
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
        # A key with no kid (Simple OAuth publishes one) matches any token.
        return [k for k in keys if k.get("kty") == "RSA" and (not kid or k.get("kid") in (kid, None))]

    keys = matching(_keys(iss))
    if not keys and kid:
        keys = matching(_keys(iss, refresh=True))   # a rotated key
    if not any(_rs256_ok(k, signed, sig) for k in keys):
        raise OidcError("bad_id_token", "bad signature")
    return claims | {"iss": iss}


def public_info() -> list[dict]:
    """For `GET /enrol`: where the app signs in — each trusted issuer with its
    authorization endpoint and this server's client id. [] when off."""
    out = []
    clients = _list("MEDIA_OIDC_CLIENTS")
    for iss in issuers():
        try:
            disc = discovery(iss)
        except OidcError:
            continue
        out.append({"issuer": iss, "authorization_endpoint": disc.get("authorization_endpoint"),
                    "client_id": clients[0] if clients else "",
                    "scopes": "openid email profile"})
    return out


def _token_request(iss: str, params: dict) -> dict:
    """A POST to the issuer's token endpoint as the public client (no secret)."""
    token_url = str(discovery(iss).get("token_endpoint") or "")
    body = urllib.parse.urlencode({**params, "client_id": (_list("MEDIA_OIDC_CLIENTS") or [""])[0]}).encode()
    req = urllib.request.Request(token_url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "User-Agent": "agent-media oidc"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def exchange_code(code: str, verifier: str, redirect_uri: str, iss: str = "") -> tuple[str, str]:
    """Swap a sign-in's authorization code for `(id_token, refresh_token)` at
    the issuer (a public client with PKCE: no secret), so the app need not
    reach the issuer's token endpoint across origins. The ID token is
    unchecked here: `verify` is still what decides. The refresh token (may be
    "") is what keeps the account's name and email current (`profile`)."""
    iss = (iss or (issuers() or [""])[0]).rstrip("/")
    if iss not in issuers():
        raise OidcError("bad_id_token", "not a trusted issuer")
    try:
        got = _token_request(iss, {"grant_type": "authorization_code", "code": code,
                                   "code_verifier": verifier, "redirect_uri": redirect_uri})
    except Exception as e:  # noqa: BLE001 — a refused code is a refusal
        raise OidcError("bad_id_token", f"the issuer refused the code: {e}") from e
    idt = str(got.get("id_token") or "")
    if not idt:
        raise OidcError("bad_id_token", "the issuer gave no ID token (was openid asked for?)")
    return idt, str(got.get("refresh_token") or "")


def profile(iss: str, refresh_token: str) -> tuple[dict, str]:
    """The account as the issuer has it now — `({sub, email, name,
    preferred_username, picture}, new refresh token)` — through the refresh token kept
    at sign-in and the userinfo endpoint. A name or email changed at the
    issuer shows here. Raises OidcError when the issuer will not (the account
    signed out everywhere, or the refresh token expired)."""
    iss = iss.rstrip("/")
    if iss not in issuers() or not refresh_token:
        raise OidcError("bad_id_token", "nothing to refresh with")
    try:
        got = _token_request(iss, {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                   "scope": "openid email profile"})
        access = str(got.get("access_token") or "")
        url = str(discovery(iss).get("userinfo_endpoint") or "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}",
                                                   "Accept": "application/json",
                                                   "User-Agent": "agent-media oidc"})
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read())
    except Exception as e:  # noqa: BLE001 — a refusal, never a 500
        raise OidcError("bad_id_token", f"the issuer would not refresh: {e}") from e
    return ({k: info.get(k) for k in ("sub", "email", "name", "preferred_username", "picture")},
            str(got.get("refresh_token") or refresh_token))


def username_of(claims: dict) -> str:
    """What to call an account: its username, else its name, else its email's local part."""
    return str(claims.get("preferred_username") or claims.get("name")
               or str(claims.get("email") or "").split("@")[0] or "")


def picture_of(claims: dict) -> str:
    """The account's picture: an https URL, else "" (the app shows an initial)."""
    url = str(claims.get("picture") or "").strip()
    return url if url.startswith("https://") and len(url) <= 2048 else ""


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
        _DISC.clear()
