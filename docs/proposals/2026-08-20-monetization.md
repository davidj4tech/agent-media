# Monetizing agent-media — what can actually be sold, and in what order

Status: proposal, nothing built. 2026-08-20.

## Recommendation in one line

Build **one entitlement primitive** — a signed, offline-verifiable token with a
set of feature flags — and let three storefronts mint it: the Play Store, a
hosted account, and a direct purchase. Then gate features against that one
primitive, never against the storefront. This is what makes "a bit of all
three" a single build instead of three.

Alongside it, run the **referral lane** — Venice first, then the harness
vendors — which needs no entitlement at all and is the only lane that can earn
before there is a product to sell.

## The constraint everything else has to respect

agent-media is Apache-2.0 (`LICENSE`, `NOTICE` — South Pen Labs). Anyone may
fork it and delete a licence check in an afternoon, and the licence explicitly
permits it. So:

> **A key check on self-hosted open source is honour-system revenue.** It is
> worth building — plenty of people pay when asked — but it is not a moat, and
> nothing that depends on it should be load-bearing.

The things that *cannot* be forked away are:

| lane | why it holds | what it costs us |
|---|---|---|
| **Hosted service** | the compute and the keys are ours; a fork gets the client, not the service | we run infra, we carry the bill, we need accounts + metering |
| **Play Store app** | distribution and updates, not code, are the product; most buyers will not build an APK | store fees, review, release discipline |
| **Venice commission** | paid by a third party for traffic, not by our users | almost nothing — a referral code and a CTA |
| **Direct licence key** | goodwill | almost nothing to build |

Order of durability is the order of effort. That is the tension; the staging
below spends effort where the return arrives soonest.

## The entitlement primitive

One module in core — `agent_media_core/entitlements.py` — with a small surface:

```python
feature_enabled("visual.hosted")   -> bool
tier()                             -> "free" | "plus" | "studio"
entitlement()                      -> Entitlement | None   # subject, tier, features, exp
```

A token is a compact signed blob (Ed25519, our public key vendored in core)
carrying subject, tier, feature list, expiry. Verification is **offline** —
no network on the hot path, no phone-home, works on a plane and on p8a's
flaky link. Storefronts differ only in how the token is obtained:

- **Play Store** — the app verifies the purchase with Play Billing, then asks
  our mint endpoint for a token and drops it where core reads it.
- **Hosted account** — sign in, get a token, refreshed on renewal.
- **Direct** — buy, receive a token by email, paste it into
  `~/.config/agent-media/config.toml` or `media licence add`.

The token lands in one place (`~/.config/agent-media/licence`), and core reads
it the same way regardless of which storefront wrote it.

**Licence boundary.** The verifier is Apache-2.0 and lives in core — it is
inert without a token and useful to no fork. The *mint* is proprietary and
server-side. Paid engines and paid intakes stay separate packages (the existing
entry-point extension contract already supports this cleanly, see
`docs/reference/extensions.md`) and may be licensed however we choose, because
core never imports them.

### What gating must never do

Speech is the spine. A failed check, an expired token, or an offline mint must
**never** take the voice away — degrade to free tier, log it, keep talking.
Every gate is written as "this extra thing is off", never "this stops".

## The three lanes, concretely

### 1. Companion app, paid tier (nearest to shippable)

The Android companion is the only artifact that is already distributable, and
it is where the value is most visibly ours: the canvas, the popup, the channel
cards, call-guard, arrival. Its `builtin` playback mode is also the reserved
slot that ends the libmpv/GPL question (`NOTICE`), which a store release would
otherwise walk straight into — so **builtin must land before the store does**.

Plausible free/paid line, to be argued rather than assumed:

- free: controls, channels, call-guard, canvas viewing
- paid: the hosted lanes (below), multi-device fleet, history/search

### 2. Hosted service tier

We run what costs money and cannot be forked: TTS voices beyond `edge`, canvas
image generation, and the relay. Needs accounts, tokens, and metering — the
largest build of the three, and the only one that puts us on the hook for a
monthly bill. Do not start here.

The seam already exists on the client side: `MEDIA_SHARE_TOKEN` is enforced at
both ends for off-loopback media sharing, so the notion of "an authenticated
remote agent-media endpoint" is not new code, only new scale.

### 3. Direct licence key for self-hosters

Cheapest to build once the primitive exists (it is just the "paste a token"
path) and the right home for fleet-shaped features that hosted users get for
free. Price it as support-and-goodwill, expect leakage, do not police it.

## The referral lane — worth a conversation now

Today `packages/visual` reads `VENICE_API_KEY` from the environment or
`~/.config/litellm/litellm.env` (`generate.py:95`) and posts straight to
`api.venice.ai`. Every canvas user is a Venice user; unkeyed users get nothing.
That is a referral funnel we are already standing in and not collecting on.

Two shapes, and they are not equally cheap:

**(a) Attributed signup — recommended first.** An unkeyed canvas shows a CTA
carrying our referral code; the user signs up with Venice directly; Venice pays
commission on the subscription. No proxy, no key pool, no abuse surface, no
cost to us. It needs one thing from Venice — a referral/affiliate arrangement —
and a few lines in the canvas.

**(b) Brokered free trial.** We hold a pool key and meter N free images per new
user before handing off to their own key. This is the better *experience* — the
canvas simply works on first run, which is exactly the moment that sells it —
but it means we run a proxy, we pay for the trial images (or Venice does, by
agreement), and we own the abuse problem. Only worth it if Venice funds the
pool as part of the deal.

Pitch to Venice, in their terms: agent-media puts a Venice-generated image on a
wall for every reply an agent speaks, in a context where no one is shopping for
an image API — it is demand they are not otherwise reaching. Ask for (a) plus a
trial allowance for (b) if they will fund it.

**Do this when the canvas is polished, not before.** A referral deal spends
first impressions; we get one.

### The same argument, pointed at the harness vendors

Venice is the first instance of a general shape, not a one-off. agent-media
sits downstream of somebody's paid product in every direction it grows: the
image API, the model API, and the agent harness itself. Each of those is a
party with a reason to want the traffic.

**Anthropic.** agent-media exists because Claude Code speaks — the Stop hook is
the origin of every word on the speech channel — and a listener hearing an
agent for the first time is about as warm as an audience for Claude Code gets.
A CTA is cheap to place and honest to make. Two cautions, and they matter more
here than with Venice:

- **There is no public affiliate programme to sign up to** (as of 2026-08-20 —
  check before promising anyone commission). So the realistic ask is not a
  percentage: it is a listing, a showcase, credits, or co-marketing. Treat
  revenue from this lane as unlikely and the distribution as the actual prize.
- **We are a client of theirs, and the CTA would be attached to their own
  output.** Anything that reads as monetizing Claude's replies is a
  relationship risk out of all proportion to the money. Keep it to the canvas
  and the app's own chrome; nothing in the spoken channel, ever.

**And the ones after that.** Core already accepts intake from Claude Code, pi,
Codex, Matrix, Home Assistant, Hermes and Open WebUI, and the list grows every
time a harness is added. Each new harness is another vendor whose users arrive
already paying somebody. That argues for building the referral surface
**once**, parameterised by source, rather than hard-coding a Venice CTA we then
copy: attribution is per-intake (we know which harness produced the reply), the
placement rule is one rule, and adding a partner is a config entry.

Three rules for that surface, before it exists and gets them wrong:

1. **Never in the spoken channel.** The voice is the product. A read-aloud
   advertisement would be the single fastest way to make people turn it off.
2. **One placement, in the canvas or the app chrome**, shown when the relevant
   thing is *absent* (no Venice key → the image CTA) rather than shown always.
   A CTA that appears when the feature already works is just noise.
3. **Attributed, or it is not a business.** If we cannot tell a partner how
   many signups came from us, there is nothing to be paid for. That means a
   referral code per partner, and per-harness attribution, from the first one.

## Staging

1. **Now** — this document; decide the free/paid line for the app.
2. ~~**Next** — `entitlements.py` plus `media licence` CLI, with a stub mint and
   a single real gate behind it. Fully testable offline; no storefront yet.~~
   **Built 2026-08-20** — see [entitlements.md](../reference/entitlements.md).
   `VENDORED_KEYS` is empty, so no install can be anything but free tier until
   there is a mint; adding the production public key is the one edit that
   turns it on.
3. **Then** — the referral surface (one placement, parameterised by
   partner and by intake), and the Venice conversation once the canvas is
   presentable. Shape (a) ships as a CTA the day a code exists. Anthropic
   and later harness vendors reuse the same surface.
4. **Then** — companion `builtin` playback (GPL clearance), then Play Billing
   into the same token.
5. **Later, and only if the app sells** — the hosted tier.

## Open questions

- Is South Pen Labs the seller of record, and is there a payment processor
  chosen? Play Billing and a direct-purchase flow have different answers.
- Free/paid line in the app — the list above is a guess and needs your call.
- Does the hosted tier ever make sense given that the target user already runs
  their own fleet, or is agent-media structurally a sell-once product?
- Is any of this compatible with keeping the repo public and Apache-2.0? This
  proposal assumes yes and confines proprietary code to the mint and to
  optional packages.
