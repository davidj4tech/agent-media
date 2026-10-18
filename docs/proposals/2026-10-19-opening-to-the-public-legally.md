# Opening Sasonica to the public without the bridge risk (19 Oct 2026)

David, 19 Oct 2026: "What are the legal ramifications of these breaches in
sasonica? how can we open it to the public in a legal way?" — after the
Teams bridge went in on red5 and bridge sign-in from the app was built
(contract §6.26). He is thinking of pitching Sasonica to the hot springs.
Status: **proposal, nothing decided.** Not legal advice: the first step
is an opinion from an Australian technology lawyer.

## What the risk is

The bridges on ryer.org (mautrix gmessages, meta, instagram, teams) are
unofficial clients. Each service's terms forbid that.

- **Contract, not crime, for personal use.** The likely consequence is
  the service locking or banning the account. Meta does this, and has
  done it to bridge users.
- **Bigger for a company that ships it.** A product that helps many
  users do it is a clearer target. *Facebook v. Power Ventures* (US, 9th
  Cir. 2016): an aggregator that kept going after a cease-and-desist was
  liable under the CFAA, even though its users had consented.
- **Australia.** The Criminal Code's computer offences (Part 10.7) turn
  on access being *unauthorised*. Users signing in with their own
  credentials are unlikely to fall under them, but carrying on after a
  service says stop is murkier.
- **Circumvention is worse than access.** The app's cookie sign-in strips
  Android's `; wv` marker from the user agent for Google sign-in pages,
  which gets around Google's embedded-WebView block. That is acceptable
  on David's own phone. It **must not ship in a public build**.
- **Other people's data.** The server keeps other people's messages and
  contact details (the rooms, `contacts.db`, the CRM timeline). That is
  personal information under the Privacy Act 1988 whoever the operator
  is.
- **AGPL.** The mautrix bridges are AGPL-3.0. Hosting them for other
  people means offering those people the source, including any changes.

## The proposal: two kinds of channel

### 1. Official channels: what Sasonica sells

Use only the services' own APIs, under their developer terms:

| Need | Official route |
|---|---|
| Teams (staff chat) | Microsoft Graph with an app the tenant admin consents to (what Element's paid bridge does) |
| WhatsApp (customers) | WhatsApp Business Platform (Cloud API), through Meta's business verification |
| Facebook / Instagram (customers) | Messenger Platform and Instagram Messaging API for the business's Page and professional account |
| SMS | A carrier API (e.g. Twilio, MessageMedia) on the business's number |
| Email | IMAP/SMTP or Gmail/Microsoft Graph with OAuth consent |

For the hot springs this is the right product anyway: their customer
conversations are business accounts. Every one of these routes exists for
this purpose. Each is a Matrix bridge or a server connector of our own,
licensed Apache-2.0 per `docs/decisions/2026-09-22-licensing.md`.

### 2. Personal bridges: bring your own, never hosted by us

Sasonica stays a Matrix client. A user who runs their own homeserver and
bridges (as David does) can sign them in from the app through the generic
provisioning API (§6.26). Sasonica ships no unofficial bridge, hosts none
for customers and does not advertise any. This is roughly where Element
and other Matrix clients sit.

- The public app build has no WebView fingerprint changes (drop the
  `; wv` strip behind a build flag that is off in release).
- The §6.26 screen works with whatever the user's bridges offer; the
  marketing and onboarding never name an unofficial bridge.

## Before any public launch

1. **A lawyer's opinion**: an Australian technology lawyer on the two-kind
   split, the terms of each official API, and the Part 10.7 question.
   Bring this document.
2. **Privacy**: a privacy policy; follow the Australian Privacy Principles
   even if the small-business exemption applies today (it is under
   reform). Write down what is kept, where, for how long and who can see
   it. Have a response plan for the Notifiable Data Breaches scheme.
   Cover deletion: `forget.py` exists for threads, and contacts and the
   timeline need the same.
3. **Terms of service** for Sasonica itself, including the user's
   responsibility for any bridge they connect.
4. **Meta / WhatsApp business verification** for South Pen Labs, which
   takes weeks.
5. **Microsoft**: a multi-tenant Entra app registration and, for some
   Graph permissions, publisher verification.
6. **Release-build checks**: a CI check that the release APK has the
   WebView user-agent strip turned off.

## The hot springs pitch (David, 19 Oct 2026)

"It will be about integrating AI into the current Hot Springs workflows.
I'm not sure to what degree. I'm not sure whether we'd host them here or
on their own servers yet." So the pitch is not "Sasonica the messaging
app" but AI fitted into how they already work, with Sasonica as the
delivery vehicle where it fits. Both open questions stay open: how far,
and who hosts.

That uncertainty is itself the reason to **sell a paid discovery first**,
not a build.

## Quoting advice

Rough Australian market figures for an independent AI or software
consultant in 2026. Check them against local peers before quoting. Prices
exclude GST: add 10% once South Pen Labs is registered (required at $75k
turnover).

### Quote in three phases, each its own decision

1. **Discovery: a fixed fee, about 1–2 weeks.** Sit with front desk,
   bookings, guest messaging, marketing and management. Map each workflow,
   the hours it takes and the systems involved (booking platform, POS,
   Microsoft 365 or Google, CRM). The deliverable is a short report: three
   to five AI opportunities ranked by hours saved against risk, a hosting
   recommendation, and a fixed quote for the pilot. Typical price is
   **$3k–$8k**. Credit it against the pilot if they go ahead, which makes
   the yes easy.
2. **Pilot: one workflow, fixed price, about 4–8 weeks.** Pick the
   workflow with the clearest payoff, for example drafting replies to
   guest enquiries for a person to approve, or a staff assistant that
   answers questions from their own policies and procedures. Agree the
   success measure up front (hours per week, response time). Typical price
   is **$15k–$40k**, depending on the integrations. Fix the scope in
   writing; anything beyond it is a change request with its own price.
3. **Ongoing: a monthly retainer.** It covers support, monitoring, prompt
   and model updates, and small changes. Typical price is
   **$1k–$4k/month**, depending on hosting (below) and response-time
   commitments.

Internally, estimate in day rates (about **$1,200–$2,000/day**, or
$150–$250/hour). Quote fixed prices, not hours: the client is buying an
outcome, and fixed prices reward you for the reusable Sasonica pieces.
Sanity-check against value. If the pilot saves 20 staff hours a week at
about $45/hour loaded, that is roughly $45k a year, so a $25k pilot pays
back within a year.

### Hosting changes the quote, not the phases

| | Hosted by South Pen Labs | On their servers |
|---|---|---|
| Setup | lower (our stack, already built) | higher: their hardware or cloud, their IT, access and security review; add about 20–40% |
| Monthly | higher retainer: covers our infrastructure, backups and on-call | lower retainer (support only), but remote-access work is billable |
| Privacy Act | we hold their guests' data: we need a data processing agreement, breach plan and insurance | they hold it; we are a contractor with access |
| Fit | small team, wants it to just work | has IT staff, or policy says data stays in-house |

Put both options in the discovery report and let them choose. Don't
guess before discovery.

### AI usage costs

Model API costs vary with use. Either **pass them through at cost plus
about 15–25%** (itemised monthly), or **bundle a usage allowance** into
the retainer, with overage above it. Never absorb them uncapped. Give a
monthly estimate from the pilot's real usage before the retainer is
signed.

### In the contract

- **IP**: South Pen Labs keeps Sasonica and anything reusable, and the
  client gets a perpetual licence to use what was built for them. Their
  data and content stay theirs.
- **Payment**: 50% on signing each phase and 50% on delivery. Retainer
  billed monthly in advance.
- **Data**: what AI providers see their data, a no-training setting on the
  API, where it is stored. Hospitality data includes guests' health
  notes (spa and bathing), which is sensitive information under the
  Privacy Act: keep it out of AI prompts unless they decide otherwise in
  writing.
- **Human in the loop**: in the pilot, AI drafts and staff send. This
  protects them (and us) from an AI reply going straight to a guest.
- **Insurance**: professional indemnity and cyber cover before the
  pilot, more so if we host.

## Other ways to work with them

David, 19 Oct 2026: "I'm not sure whether I want to go down that
consulting route but I am curious." The quoting advice above assumes
consulting. These are the alternatives. Discovery (a short, paid look at
what they need) fits in front of any of them.

| Route | What they buy | For South Pen Labs | Needs first |
|---|---|---|---|
| **Product subscription** | Sasonica at a set monthly price per venue or per seat, a small setup fee, no custom work | scales best; one codebase; revenue recurs | a polished, documented product, self-serve setup, support process, the official connectors |
| **Fixed-price package** | one well-defined thing (e.g. AI-drafted replies to guest enquiries, staff approve) set up for a known price, plus a small monthly fee | repeatable for other venues; bounded effort; a stepping stone to the product | one connector done well, a setup checklist |
| **Through their IT provider** | the provider resells it and owns the relationship and first-line support | less client work; reach to the provider's other clients | a reseller price (provider margin about 20–30%), partner docs, second-line support terms |
| **Trial, paid if it works** | free or cheap pilot for 4–6 weeks, then a subscription if it meets the agreed measure | easiest yes for them; we carry the risk | a cap on our time, the success measure in writing up front |
| **Consulting** (above) | our time: discovery, a pilot, a retainer | most income per client early; most custom work; does not scale | nothing new: start now |

A blend is common: consult on the first venue to learn the workflows, but
build every piece as part of the product, keep the IP (see "In the
contract"), and turn what worked into the fixed-price package for the
next venue.

## Open questions for David

- Which route, or which blend?

- Staff side (Teams, internal procedures), guest side (enquiries, bookings,
  WhatsApp, Messenger, Instagram, SMS) or both? Discovery answers this;
  it decides which official connector comes first.
- Hosted or on their servers: decided in discovery (table above).
- Which booking and POS systems they run: the biggest unknown in pilot
  pricing.
