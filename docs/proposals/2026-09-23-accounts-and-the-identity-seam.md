# Proposal: accounts — one verifier, a list of issuers, and enrolment is the only new job (23 Sep 2026)

Status: **proposal, nothing built.** Extends `server-contract.md` §9 (device
tokens, built 22 Sep 2026). Touches `agent_media_server/auth.py`,
`devices.py`, `app.py`.

## Why

Pairing today is one-directional. The app can redeem a code; nothing in it
can mint one. A second device is enrolled at the desk with
`media-visual-canvas pair --device NAME`, and the device list lives behind
`media-visual-canvas devices`. That is a deliberate property — the person
with a shell decides what devices exist and what they are called, so a
stolen phone cannot enrol a friend — and it is also the whole of the
authority model. There is exactly one scope.

Two things want more than that:

- **A second device, from the couch.** The shell is the only enrolment
  surface, and it is the one surface you do not have when you want it.
- **The hosted tier.** More than one person means identity across *people*,
  not devices: who is paying, whose threads are whose, who may enrol.

The question is not what shape a credential is. It is who has authority to
create one. An account system answers that; OAuth by itself does not.

## The load-bearing claim: accounts enrol, they do not authenticate

A sign-in returns **a device token** — the same 43-character
`secrets.token_urlsafe(32)` `POST /pair` returns today, stored in the same
`devices.json`, checked by the same local sha256 compare.

Everything downstream is unchanged, and the property bought by leaving
Audiobookshelf survives: `auth.lookup` makes no network call, so a request
carrying a device token cannot be answered 503 by an identity provider that
is down. An outage at the issuer stops new *enrolments*. It cannot log
anybody out, and it cannot take the box off the air.

This is the difference between an account system and OAuth-as-auth, and it
is the only design decision here that is hard to reverse.

## Shape

`auth.py` is already the one choke point, and already a fallthrough chain:
device, then ABS. It grows a third link in the middle.

```
gate(bearer) →  devices.lookup()          local, cheap, never 503      (unchanged)
             →  oidc.verify(bearer)       a JWT from a trusted issuer  (new)
             →  auth_abs._gate()          the ABS adapter              (unchanged, deleted at the ABS exit)
```

`oidc.verify` is a bearer that parses as a JWT whose `iss` is in the
configured list, signature checked against that issuer's JWKS (cached, with
the discovery document, for an hour; a cold cache is the only network call,
and failing it is a 401, never a 500). It exists so a browser or a
short-lived session can present an account token directly. The app does not
use it — the app trades its sign-in for a device token once and never speaks
to the issuer again.

New route, open like `POST /pair`:

```
POST /enrol  {"id_token": "<jwt>", "device": "Pixel Tablet"}
  → {"ok": true, "token": "<43 chars>", "device_id": "d_…", "name": …, "server": {…}}
  → 403 {"ok": false, "code": "bad_id_token"}       unknown issuer, bad signature, expired
  → 403 {"ok": false, "code": "not_enrolled"}       a valid token for a subject with no claim here
  → 429 {"ok": false, "code": "rate_limited"}       as POST /pair
```

It is `POST /pair` with a different proof. The device row gains `sub` and
`iss` (who enrolled it), so `devices --revoke` can take an account as well
as an id, and an account that goes away can have its devices dropped with
it.

## Issuers are configuration, not code

```
MEDIA_OIDC_ISSUERS = https://accounts.example.org, https://matrix.example.org
MEDIA_OIDC_ALLOW   = <iss>|<sub or group claim>, …
```

A verifier that trusts a list is the same code as one that trusts a single
issuer. Two worked examples, both of which already speak OIDC:

- **Drupal**, via `simple_oauth` / `openid_connect`. Earns its place if the
  hosted tier wants accounts, supporter purchases and a marketing front door
  on one site — that is a CMS job, and rolling it is the worse trade.
- **Matrix**, via MAS (Matrix Authentication Service, an OIDC provider).
  Interesting because if a room is a thread — `2026-09-23-matrix-in-the-app.md`
  — then identity arrives with the transport that proposal already wants,
  and the management room's `login` flow becomes the enrolment surface.

Both at once is not a compromise, it is two entries in a list. The server
does not care which signed the token; it checks against the matching JWKS
and mints.

## The enrol bit closes the original hole

A device row gains `enrol: true|false`, default false. A claim in the id
token (`groups` containing an admin group, say) sets it at enrolment; the
CLI sets it at mint time.

```
POST /devices/code  {"device": "Pixel Tablet"}   — gated, requires enrol
  → {"ok": true, "code": "7f3a09c1", "expires": <ts>, "links": {...}}
GET  /devices                                    — gated, requires enrol
DELETE /devices/{id}                             — gated, requires enrol
```

`devices.mint_code`, `list_devices` and `revoke` exist already; these are
three thin routes over them plus one predicate. Your phone enrols the
tablet. A plain device still cannot enrol anything, and still cannot see
what else is paired.

**This part does not need accounts, OIDC, or Drupal, or Matrix.** It is the
thirty-line version of the whole problem, and it should land first whatever
happens to the rest.

## Order

1. `enrol` on the device row (default false; `pair --device --enrol` sets
   it), the three gated routes, the app's Settings → Devices screen. Ships
   the couch use case on its own.
2. `oidc.py`: discovery, JWKS cache, `verify`. Tests against a local
   key pair, no network in CI.
3. `POST /enrol`, `sub`/`iss` on the row, `devices --revoke --account`.
4. An issuer, when there is a reason for one — MAS if Matrix lands first,
   Drupal if the hosted tier does.

Steps 2–4 are dead weight for one person on a tailnet. Step 1 is not.

## What this does not solve

- **Multi-tenancy.** Everything here still hands out tokens that stand for
  *the owner*: one shell, one set of threads, one `~/.claude`. An account
  that is a different person needs its own view of sessions, transcripts and
  panes, and that is a much larger change than authentication. Accounts
  here are enrolment for one household, and the hosted tier's real cost is
  that separation, not its login page.
- **The amux token.** `GET /pair` stays what it is: the canvas's page, no
  CORS, its own store, unreachable from any of this. Nothing above may ever
  be a route to being handed it.
