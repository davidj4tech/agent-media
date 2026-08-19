"""What this install has paid for — one primitive, several storefronts.

    ~/.config/agent-media/licence      a single-line token
    MEDIA_LICENCE                      the token itself, or a path to one

A licence is a signed, self-contained token. Verification is **offline**: no
network on any path here, no phone-home, no account. It works on a plane, on
p8a's flaky link, and in CI.

    AM1.<base64url payload>.<base64url signature>

The payload is JSON — subject, tier, granted features, issue and expiry times,
and the id of the key that signed it. The signature (Ed25519, `_ed25519`)
covers the ASCII of ``AM1.<payload>``, so the encoding is part of what is
signed and a re-encoding is not a valid token.

## Why storefronts do not appear in this file

Play Billing, a hosted account, and a direct purchase differ only in *how a
token is obtained*. Once obtained, all three write the same bytes to the same
path, and everything downstream asks `feature_enabled(...)`. Nothing in core
knows or cares which shop the token came from, which is what keeps "sell it
three ways" a single build instead of three.

The **mint** — the thing holding the private key — is deliberately not here.
There is no seller of record yet (2026-08-20); `media licence keygen` and
`media licence mint` exist so the whole path can be exercised offline against
a key you generate yourself.

## Two rules this module may never break

**Nothing here can stop a host making sound.** A corrupt token, a missing
file, a clock skew, an unknown signing key — every one of them reads as "free
tier", logged, never raised. Every gate is written as "this extra thing is
off", never as "this stops". The same property `config` has, for the same
reason: consumers have a defined behaviour for unset and none for "the licence
check raised".

**A failed check is not an accusation.** Free tier is the ordinary state of an
install, not an error state, so nothing on this path is louder than a debug
line unless a token is present *and* bad.

## And the honest part

agent-media is Apache-2.0. Anyone may fork it and delete this module, and the
licence they were given expressly permits it. This is honour-system revenue
by construction, and `trusted_keys` even accepts keys from local config
because pretending otherwise would only make the code dishonest, not the
enforcement stronger. See docs/proposals/2026-08-20-monetization.md for what
actually holds — hosted service, store distribution, and referral commission —
and for why none of it is load-bearing on this file.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from ._ed25519 import verify as _ed_verify


log = logging.getLogger(__name__)

LICENCE_ENV = "MEDIA_LICENCE"
KEYS_ENV = "MEDIA_LICENCE_KEYS"

TOKEN_PREFIX = "AM1"

FREE_TIER = "free"
KNOWN_TIERS = ("free", "plus", "studio")

# Public keys the mint signs with, as {key_id: hex}. Empty until there is a
# seller of record — an install with no vendored key simply has no way to be
# anything but free tier, which is the correct behaviour for today. Adding the
# production key here is the single edit that turns this on.
VENDORED_KEYS: dict[str, str] = {}


# --------------------------------------------------------------------------
# the entitlement
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Entitlement:
    """A verified licence. Never constructed from an unverified token."""

    subject: str = ""
    tier: str = FREE_TIER
    features: frozenset = field(default_factory=frozenset)
    issued_at: int = 0
    expires_at: int = 0          # 0 = perpetual
    key_id: str = ""

    def expired(self, now: float | None = None) -> bool:
        if not self.expires_at:
            return False
        return (now if now is not None else time.time()) >= self.expires_at

    def grants(self, feature: str) -> bool:
        """Exact grant, or a prefix grant (``visual.*``), or ``*``.

        Prefix grants exist so a tier can be sold as "everything visual"
        without the token having to enumerate features that do not exist yet —
        an old token must keep working when a new feature ships under a
        heading it already paid for.
        """
        name = (feature or "").strip().lower()
        if not name:
            return False
        for granted in self.features:
            if granted == "*" or granted == name:
                return True
            if granted.endswith(".*") and name.startswith(granted[:-1]):
                return True
        return False


FREE = Entitlement()


# --------------------------------------------------------------------------
# where the token and the keys come from
# --------------------------------------------------------------------------

def licence_path() -> Path:
    """The on-disk token. Beside config.toml, because it is configuration.

    A function rather than a constant for the reason `config` gives: resolved
    at import time it would freeze whatever HOME happened to be, which is
    silently wrong in tests — there it would read the developer's real licence.
    """
    return Path.home() / ".config" / "agent-media" / "licence"


def read_token(path: Path | None = None) -> str:
    """The raw token text, or "". Environment beats file, as everywhere else.

    ``MEDIA_LICENCE`` may hold the token itself or a path to one; a value
    starting with the token prefix is the token, anything else is a path. The
    ambiguity is resolved by shape rather than by a second variable because
    both spellings are things people actually do — a token pasted into a
    service environment, a path pointed at a mounted secret.
    """
    raw = (os.environ.get(LICENCE_ENV) or "").strip()
    if raw.startswith(TOKEN_PREFIX + "."):
        return raw
    if raw:
        path = Path(raw).expanduser()
    try:
        return (path or licence_path()).read_text().strip()
    except OSError:
        return ""


def trusted_keys(path: Path | None = None) -> dict[str, bytes]:
    """``{key_id: public key bytes}`` — vendored, then config, then env.

    Later sources add to and override earlier ones. Accepting keys from local
    config is a self-hoster affordance and an admission (see the module
    docstring): someone who wants to mint their own licence for their own fleet
    can, and someone who wanted to cheat had an easier route already.
    """
    out: dict[str, bytes] = {}

    def _add(kid: str, hexed: str) -> None:
        kid = str(kid).strip()
        try:
            raw = binascii.unhexlify(str(hexed).strip())
        except (binascii.Error, ValueError):
            log.warning("agent-media licence: key %r is not hex — ignored", kid)
            return
        if len(raw) != 32:
            log.warning("agent-media licence: key %r is not 32 bytes — ignored",
                        kid)
            return
        out[kid] = raw

    for kid, hexed in VENDORED_KEYS.items():
        _add(kid, hexed)

    table = config.load(path).get("licence")
    if isinstance(table, dict) and isinstance(table.get("keys"), dict):
        for kid, hexed in table["keys"].items():
            _add(kid, hexed)

    for pair in (os.environ.get(KEYS_ENV) or "").split(","):
        if ":" in pair:
            kid, _, hexed = pair.partition(":")
            _add(kid, hexed)

    return out


# --------------------------------------------------------------------------
# the token itself
# --------------------------------------------------------------------------

def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes | None:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (binascii.Error, ValueError):
        return None


def encode(payload: dict, seed: bytes) -> str:
    """Sign `payload` into a token. Mint-side; needs the private seed.

    Imported lazily so that the signing half of `_ed25519` is never reachable
    from a verifying install by accident — nothing on the read path can end up
    holding a routine that wants a private key.
    """
    from ._ed25519 import sign as _ed_sign

    body = _b64encode(json.dumps(payload, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8"))
    signed = f"{TOKEN_PREFIX}.{body}"
    return f"{signed}.{_b64encode(_ed_sign(seed, signed.encode('ascii')))}"


def _parse(token: str, keys: dict[str, bytes]) -> Entitlement | None:
    """Verify and decode, or None with a warning. Never raises."""
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        log.warning("agent-media licence: not a recognised token — ignored")
        return None

    body, sig = _b64decode(parts[1]), _b64decode(parts[2])
    if body is None or sig is None:
        log.warning("agent-media licence: token is not valid base64 — ignored")
        return None

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        log.warning("agent-media licence: token payload is not JSON — ignored")
        return None
    if not isinstance(payload, dict):
        log.warning("agent-media licence: token payload is not an object")
        return None

    kid = str(payload.get("kid") or "")
    pub = keys.get(kid)
    if pub is None:
        # The common cause is a token minted for a newer core than this one,
        # so name the key: it is the only thing that makes this diagnosable.
        log.warning("agent-media licence: signed by unknown key %r — ignored",
                    kid)
        return None

    if not _ed_verify(pub, f"{parts[0]}.{parts[1]}".encode("ascii"), sig):
        log.warning("agent-media licence: signature does not verify — ignored")
        return None

    feats = payload.get("feat")
    if not isinstance(feats, (list, tuple)):
        feats = []
    tier = str(payload.get("tier") or FREE_TIER).strip().lower()

    def _int(name: str) -> int:
        value = payload.get(name)
        return int(value) if isinstance(value, (int, float)) else 0

    return Entitlement(
        subject=str(payload.get("sub") or ""),
        tier=tier,
        features=frozenset(str(f).strip().lower() for f in feats if str(f).strip()),
        issued_at=_int("iat"),
        expires_at=_int("exp"),
        key_id=kid,
    )


# Verification costs ~5ms of pure-Python curve arithmetic, and `feature_enabled`
# is called from hot paths. Cache the verdict by token text: the same bytes
# always verify the same way. Expiry is deliberately *not* cached — it is
# evaluated against the clock on every call, so a long-running service does not
# hold a licence open past its end.
_verified: dict[str, Entitlement | None] = {}


def _cached(token: str, keys: dict[str, bytes]) -> Entitlement | None:
    # Keyed on the key *material*, not just the ids: a kid is a name a config
    # file can reuse for different bytes, and a cache that ignored that would
    # answer for whichever key it happened to see first.
    key = token + "\x00" + ",".join(f"{k}:{keys[k].hex()}" for k in sorted(keys))
    if key not in _verified:
        _verified[key] = _parse(token, keys)
    return _verified[key]


def refresh() -> None:
    """Drop the verification cache. For tests, and for `media licence add`."""
    _verified.clear()


# --------------------------------------------------------------------------
# what the rest of core calls
# --------------------------------------------------------------------------

def entitlement(path: Path | None = None) -> Entitlement:
    """This install's entitlement — `FREE` when there is no valid licence.

    Returns an Entitlement, never None, so no call site has to decide what a
    missing licence means. They all mean free tier.
    """
    token = read_token()
    if not token:
        return FREE
    ent = _cached(token, trusted_keys(path))
    if ent is None:
        return FREE
    if ent.expired():
        log.warning("agent-media licence: expired %s — free tier",
                    time.strftime("%Y-%m-%d", time.localtime(ent.expires_at)))
        return FREE
    return ent


def feature_enabled(feature: str, path: Path | None = None) -> bool:
    """Is `feature` paid for? The one question the rest of core asks."""
    return entitlement(path).grants(feature)


def tier(path: Path | None = None) -> str:
    """``free`` | ``plus`` | ``studio`` — for display, not for gating.

    Gate on features, never on the tier name: a tier is a price-list label and
    will be renamed, split, and grandfathered, while a feature name is a
    contract with the code.
    """
    return entitlement(path).tier


def status(path: Path | None = None) -> dict:
    """A plain dict for `media licence show --json` and for doctor output."""
    token = read_token()
    ent = entitlement(path)
    return {
        "tier": ent.tier,
        "subject": ent.subject,
        "features": sorted(ent.features),
        "issued_at": ent.issued_at,
        "expires_at": ent.expires_at,
        "key_id": ent.key_id,
        "have_token": bool(token),
        "valid": ent is not FREE,
        "path": str(licence_path()),
        "trusted_keys": sorted(trusted_keys(path)),
    }
