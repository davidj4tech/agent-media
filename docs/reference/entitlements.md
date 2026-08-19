# Entitlements — what an install has paid for

Built 2026-08-20. Implements stage 2 of
[the monetization proposal](../proposals/2026-08-20-monetization.md).

There is **no seller of record and no mint** yet. What exists is the primitive
every storefront will eventually write into, plus the developer-side commands
to exercise the whole path offline.

## The shape

A licence is a signed, self-contained token:

```
AM1.<base64url payload>.<base64url Ed25519 signature>
```

The payload is JSON — `sub`, `tier`, `feat`, `iat`, `exp`, `kid`. The
signature covers the ASCII of `AM1.<payload>`, so the encoding is part of what
is signed.

Verification is **offline**. No network, no account, no phone-home, on any
path. It works on a plane and on the phone's flaky link, and it works in CI.

```
~/.config/agent-media/licence     the token, one line
MEDIA_LICENCE                     the token itself, or a path to one
```

Environment beats file, as everywhere else in agent-media.

## Asking

```python
from agent_media_core import entitlements

if entitlements.feature_enabled("visual.hosted"):
    ...
```

`feature_enabled` is the only question worth asking. `tier()` exists for
display — **never gate on it**: a tier is a price-list label and will be
renamed, split and grandfathered, while a feature name is a contract with the
code.

A grant may be exact (`visual.hosted`), a prefix (`visual.*`), or everything
(`*`). Prefix grants are what let a token sold last year keep working when a
new feature ships under a heading it already paid for.

## The two rules

**Nothing here can stop a host making sound.** A corrupt token, a missing
file, a clock skew, an unknown signing key — all of them read as free tier,
logged, never raised. Write every gate as "this extra thing is off", never as
"this stops". Free tier is the ordinary state of an install, not an error.

**Gate at the boundary, not in the code everyone shares.** Today core contains
exactly one gate, in `extensions.py`: a render engine may declare
`agent_media_requires = "engine.studio"` and is then hidden from an install
whose licence does not grant it. Paid capability ships as a separate package,
so the question is asked once, where core meets code it does not own. A
skipped engine is not an error — rendering falls through to `edge`.

## The CLI

```sh
media licence show [--json]        # tier, features, expiry, trusted keys
media licence add <token|->        # install one (stdin with -)
media licence remove
media licence check <feature> -q   # exit 0 if granted — for hooks and shell
```

`add` keeps a token it cannot verify, and says so on stderr. An install
lagging the key that signed its licence is a real situation, and throwing the
user's token away is worse than holding an inert one.

### Developer-side, until there is a mint

```sh
media licence keygen --kid dev     # prints a key pair; stores nothing
media licence mint --seed <hex> --kid dev --tier plus \
                   --feature 'visual.*' --days 30
```

`keygen` prints the private seed rather than writing it: a private key a CLI
drops into `$HOME` is a private key that ends up in a backup.

## Trusted keys

In order, each adding to and overriding the last:

1. `entitlements.VENDORED_KEYS` — empty. Adding the production public key here
   is the single edit that turns this on.
2. `[licence.keys]` in `~/.config/agent-media/config.toml`.
3. `MEDIA_LICENCE_KEYS="kid:hex,kid:hex"`.

## The honest part

agent-media is Apache-2.0. Anyone may fork it and delete this module, and the
licence they were given expressly permits it — which is why local config can
supply a trusted key: pretending otherwise would make the code dishonest, not
the enforcement stronger. This is honour-system revenue by construction.

What actually holds is a hosted service, store distribution, and referral
commission. None of it is load-bearing on this file. See the proposal.

## Why the curve arithmetic is hand-rolled

`cryptography` is the one package that reliably fails to install on the phone
— a pip-built wheel `dlopen`-fails under Termux and the working one has to
come from `pkg` and be symlinked into the venv by hand. Adding it to core so
that a licence could be checked would mean a licence check could stop a fresh
install from speaking, which is the single outcome this design is not allowed
to have. `_ed25519.py` is the RFC 8032 reference implementation, checked
against the RFC's own vectors, and it is not constant-time — it guards nothing
from the person running it.
