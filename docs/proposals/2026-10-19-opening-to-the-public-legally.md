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

## Open questions for David

- Is the hot springs pitch staff (Teams), customers (WhatsApp, Messenger,
  Instagram, SMS) or both? That decides which official connector comes
  first.
- Hosted (South Pen Labs runs the homeserver and connectors) or on the
  customer's own box? Hosted makes us the data holder under the Privacy
  Act.
