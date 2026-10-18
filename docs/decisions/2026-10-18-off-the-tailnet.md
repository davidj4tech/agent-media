# Decision: nothing new relies on the tailnet

Status: **accepted**
Date: 2026-10-18

## The rule

New links between agent-media's parts, and between the server and the
things it talks to, must not need a tailnet (Tailscale or any other mesh).
Use, in order of preference:

1. The public address over HTTPS with a scoped credential (a device token,
   an OAuth client).
2. A connection the device opens outbound, as the phone's jobs worker does
   (server-contract §6.21a).

A tailnet or ssh path may exist as a fallback. It is never the primary
design, and nothing may stop working when it is absent.

## Why

The software is meant to be installed by people who do not run a mesh, and
to work for a phone that is off it. The tailnet is one installation's setup,
not something an install can assume. A slow or down tailnet link also made
features hang on a phone that could reach the server fine by its public
address.

David, 3 Oct 2026: "I'd like to move away from the dependencies on
tailscale for sasonica", which chose a Cloudflare Tunnel for phone to server
and outbound connections from the phone (`docs/proposals/2026-10-03-off-the-tailnet.md`).
David, 18 Oct 2026, widened it to the rest of the software: "we're working
towards not relying on tailnet for the software". The same day a planned
tailnet-only listener for a CRM integration was replaced by its public HTTPS
address and an OAuth client.

## Consequences

- A tailnet address in code or a default (`100.x.y.z`, a MagicDNS name) is a
  bug; hostnames belong in the server's config.
- Plain http on a tailnet is not a secure context in a browser, which hides
  some behaviour (see packages/visual README, "Gotchas"). A public HTTPS
  route is the case to test.
- Server-contract §19 lists the three ways to reach a server; "tailnet only"
  stays as an option for people who already have a mesh, not as the design.
