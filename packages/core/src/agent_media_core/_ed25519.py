"""Ed25519 sign/verify, in pure Python, with no dependencies.

## Why not `cryptography`

Because core's dependency list is the thing that decides whether agent-media
installs at all on the phone, and `cryptography` is precisely the package that
does not: a pip-built wheel dlopen-fails under Termux, and the working version
has to come from `pkg install python-cryptography` and be symlinked into the
venv by hand. Adding it to core to check a licence would mean a licence check
could stop a fresh install from speaking — the one outcome the entitlement
design is not allowed to have.

The verifier also has to be readable by anyone auditing what a paid tier does
to an Apache-2.0 codebase. Seventy lines of RFC 8032 is readable; a C
extension is not.

## What this is

The reference implementation from RFC 8032 §6, transcribed, with the field
arithmetic reduced mod p as it goes so the intermediate integers stay small.
It is *not* constant-time, and it is not written to be: it verifies a licence
that the holder is free to read, and signs only in the developer-side mint.
Nothing here guards a secret from the person running it.

Verification costs two scalar multiplications — order tens of milliseconds.
`entitlements` therefore caches the verdict per token rather than calling this
on every feature check.
"""

from __future__ import annotations

import hashlib


P = 2**255 - 19
Q = 2**252 + 27742317777372353535851937790883648493


def _sha512(s: bytes) -> bytes:
    return hashlib.sha512(s).digest()


def _sha512_modq(s: bytes) -> int:
    return int.from_bytes(_sha512(s), "little") % Q


# Curve constants. Points are extended coordinates (X, Y, Z, T) with
# x = X/Z, y = Y/Z, x*y = T/Z.
_D = -121665 * pow(121666, P - 2, P) % P
_SQRT_M1 = pow(2, (P - 1) // 4, P)


def _point_add(p1, p2):
    a = (p1[1] - p1[0]) * (p2[1] - p2[0]) % P
    b = (p1[1] + p1[0]) * (p2[1] + p2[0]) % P
    c = 2 * p1[3] * p2[3] * _D % P
    dd = 2 * p1[2] * p2[2] % P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _point_mul(s: int, p1):
    q = (0, 1, 1, 0)  # neutral element
    while s > 0:
        if s & 1:
            q = _point_add(q, p1)
        p1 = _point_add(p1, p1)
        s >>= 1
    return q


def _point_equal(p1, p2) -> bool:
    if (p1[0] * p2[2] - p2[0] * p1[2]) % P != 0:
        return False
    return (p1[1] * p2[2] - p2[1] * p1[2]) % P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * _SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_G_Y = 4 * pow(5, P - 2, P) % P
_G_X = _recover_x(_G_Y, 0)
_G = (_G_X, _G_Y, 1, _G_X * _G_Y % P)


def _point_compress(p1) -> bytes:
    zinv = pow(p1[2], P - 2, P)
    x = p1[0] * zinv % P
    y = p1[1] * zinv % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _point_decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _secret_expand(secret: bytes) -> tuple[int, bytes]:
    if len(secret) != 32:
        raise ValueError("ed25519 seed must be 32 bytes")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    """The 32-byte public key for a 32-byte seed."""
    a, _ = _secret_expand(seed)
    return _point_compress(_point_mul(a, _G))


def sign(seed: bytes, msg: bytes) -> bytes:
    """A 64-byte signature over `msg`. Mint-side only."""
    a, prefix = _secret_expand(seed)
    pub = _point_compress(_point_mul(a, _G))
    r = _sha512_modq(prefix + msg)
    rs = _point_compress(_point_mul(r, _G))
    h = _sha512_modq(rs + pub + msg)
    s = (r + h * a) % Q
    return rs + int.to_bytes(s, 32, "little")


def verify(pub: bytes, msg: bytes, signature: bytes) -> bool:
    """True if `signature` is a valid Ed25519 signature of `msg` under `pub`.

    Returns False for every malformed input rather than raising: a corrupt
    licence file must read as "no licence", never as a traceback.
    """
    if len(pub) != 32 or len(signature) != 64:
        return False
    a_pt = _point_decompress(pub)
    if a_pt is None:
        return False
    # Reject small-order public keys. The curve has an order-8 subgroup, and
    # for a key inside it (the all-zero encoding among them) the verification
    # equation degenerates and accepts an all-zero signature — so a token
    # "signed" by a key of zeros would verify. Three doublings is the whole
    # cost of not having that be true.
    if _point_equal(_point_mul(8, a_pt), (0, 1, 1, 0)):
        return False
    rs = signature[:32]
    r_pt = _point_decompress(rs)
    if r_pt is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= Q:
        return False
    h = _sha512_modq(rs + pub + msg)
    return _point_equal(_point_mul(s, _G), _point_add(r_pt, _point_mul(h, a_pt)))
