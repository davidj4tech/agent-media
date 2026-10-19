# Follow-ups from the permanent-address and Windows-pairing work

Moved from agent-mail #91, #92, #95, #96 (agent-media → agent-config, 16 Oct 2026) on 2026-10-16. They are agent-media's plans and outcomes for threads 80 and 78. David: "I think they already completed anyway." Both are done; this chat exists for what was left open.

## Still open (from #95/#96)
1. **Windows on a real desktop:** not tried yet. hpo is offline, so SmartScreen and the logon tasks are unchecked. Do it when hpo is back.
2. **sasonica.com/install.ps1 still serves Shell's get.ps1.** The Windows server installer is only reachable as the raw GitHub deploy/install.ps1. /start's "On Windows" text still says Windows doesn't pair. Waiting on David to choose a short URL: ask him.
3. **No ARM64 cloudflared for Windows.**
4. **Pre-existing CI pytest failures:** personas, doc undo, org_listen and others. They were the same before this work, so none come from it, but they're still red.
5. Installs and pairings from before 16 Oct need `sasonica update` and one more pairing to get the lookup. Does the app or update path tell people this?
   **Done 19 Oct 2026 (pairing half):** GET /me now names the lookup and the app keeps it (agent-media 2930df0, sasonica-app fc42431), so an old pairing needs no second pairing once its server is updated. The server half stands: an install from before 16 Oct publishes nothing until `sasonica update`, and nothing tells its owner so.

First, check against the repo whether any of these are already done. Then tell David which are left, and ask which to take on.

## The original messages

### #91 — agent-media: permanent server address (off-the-tailnet) (2026-10-16T01:07)

Plan (David, 16 Oct 2026: "you decide", and start Matrix too).

Decision: lift the 8 Oct "on hold pending Matrix" on the lookup and ship it. It needs no account (self-hosters keep working), it's already built and tested (agent-media lookup-hold 04f637f, sasonica-app tunnel-lookup 1c52416), and if Matrix later becomes the app's transport it replaces the lookup without anything being lost. Not the Shell relay: it's a command queue with no HTTP/SSE proxy, and sasonica-shell docs/umbrella.md argues against carrying the app there.

Steps:
1. Rebase lookup-hold onto main (130 behind) and tunnel-lookup onto sasonica-app main (92 behind); rerun tests.
2. Worker deploy/lookup redeployed on sasonica.com (KV sasonica-lookup kept) — after David says go.
3. App build with the lookup; sasonica.com/start loses "A permanent address is coming."
4. In parallel: write the Matrix-as-app-transport proposal (owed since docs/handover/2026-10-09-matrix.md).
No push, deploy or APK install without David.

### #95 — agent-media: permanent server address (off-the-tailnet) (2026-10-16T03:04)

Outcome (David approved going live, 16 Oct 2026):
- Lookup live: Worker sasonica-lookup deployed on sasonica.com/r/* (version 89063a7a); unknown id answers 404 {"error":"no such install"}; the rest of the site unaffected.
- agent-media main 755947a..b5fe625 (lookup, hold lifted, contract §9, install note), pushed; Server binary run 38840068155 green on all four targets.
- sasonica-app main d0c1ab5, a8b54d0, 01cc044 (lookup in the app incl. shared files; What's new 2026-10-16m), pushed; Build APK 38840070204 green. Not installed on p8a (red5 uses its named tunnel, not the lookup).
- Matrix-as-transport proposal: docs/proposals/2026-10-16-matrix-transport.md (3186c33, David's answers 89fdb05). The lookup stays as the no-account path.
- sasonica.com/start wording updated (websites 636ba23), deploying.
Caveat: installs and pairings from before today need `sasonica update` and one more pairing to get the lookup.

### #92 — agent-media: Windows server should pair with the Sasonica app (2026-10-16T01:07)

Plan. The Windows server already exists (single binary, schtasks logon tasks, headless sessiond, local speech merged). The one pairing blocker: install.py tunnel_skip_reason() returns "not on Windows yet" and tunnel.py platform_asset() has no Windows cloudflared, so _pair_host() has no address.

Steps:
1. tunnel.py: Windows cloudflared asset (cloudflared-windows-amd64.exe), run as a Sasonica\tunnel schtasks logon task like canvas/sessiond; URL capture from its log as on Linux.
2. Drop the Windows skip; un-skip the tunnel tests on Windows; add tunnel + pair --device to the windows CI e2e.
3. Pairing then rides the same permanent-address lookup as #80 (built on top of it).
4. sasonica.com/start "On Windows": the server pairs with the app; give it a short install URL (sasonica.com/install.ps1 is Shell's get.ps1 today — keep that, add one for the server).
Unverified until a real desktop (hpo offline): SmartScreen, WASAPI, logon tasks. No push/release without David.

### #96 — agent-media: Windows server should pair with the Sasonica app (2026-10-16T03:04)

Outcome: Windows gets the quick tunnel (agent-media b1444cd, CI dd951e5, pushed). cloudflared-windows-amd64.exe 2026.9.3 pinned by sha256, a Sasonica\quick-tunnel logon task, cloudflared in a kill-on-close job object, install.ps1 waits for URL + /healthz before pairing; with the lookup (#80) the address survives restarts. Server binary run 38840068155: Windows smoke test green, and a real quick tunnel through Cloudflare answered /healthz on GitHub's Windows runner. CI's pytest failures are identical to the run before (personas, doc undo, org_listen, etc.), none from this.
Still open: not tried on a real desktop (hpo offline: SmartScreen, logon tasks); sasonica.com/install.ps1 still serves Shell's get.ps1, so the Windows server installer is only the raw GitHub deploy/install.ps1, and /start's "On Windows" text still says it doesn't pair — left for David to choose a short URL. No ARM64 cloudflared for Windows.

