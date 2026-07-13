# Canvas behind Open WebUI

Show the agent-media canvas as a live, full-bleed background *behind* Open
WebUI's chat, in one browser tab. The wall canvas and the OWUI backdrop are the
**same page** — `bg.js` embeds `canvas.py`'s real page as an iframe, so the two
never drift.

```
 ┌──────────── one browser tab (OWUI origin) ─────────────┐
 │  OWUI chat  (html/body transparent, message list scrim) │  ← foreground
 │      ▓▓▓ bubbles ▓▓▓                                     │
 │  ┌───────────────────────────────────────────────┐  ▣  │  ← FAB toggles
 │  │  <iframe src="/canvas/">  = canvas.py's page    │     │    front/back
 │  │  Ken Burns · SSE images · audio controller · … │     │
 │  └───────────────────────────────────────────────┘     │  ← background,
 └─────────────────────────────────────────────────────────┘    click-through
```

## Pieces

| file | what |
|------|------|
| `../static/bg.js` | the loader — injected into OWUI's HTML. Builds the iframe + FAB, makes OWUI transparent, adds a scrim behind the chat. A static asset; no daemon. |
| `owui-canvas.Caddyfile` | the reverse proxy in front of OWUI: serves `bg.js`, proxies the canvas (see routing note), injects the `<script>` tag. |
| `smoke-test.sh` | curl-level checks + a browser eyeball checklist. |

## How OWUI loads `bg.js`

OWUI **v0.10.2 has no in-app custom-JS hook** — its config store holds no
`custom_js` / head-injection key (checked: the `config` table's keys are all
providers/RAG/auth/ui-flags, nothing scriptable). So the *only* way to load
`bg.js` is at the reverse-proxy layer: Caddy's `replace_response` inserts

```html
<script defer src="/canvas/bg.js"></script>
```

before `</head>` on every OWUI HTML response. That needs Caddy built with the
`replace-response` plugin (`xcaddy build --with github.com/caddyserver/replace-response`).

> The db-write precedent from earlier stages (writing `webui.db` to install the
> `intake-owui` **Function** + the model connection) does *not* apply here —
> that's OWUI's Functions/tools surface, a different thing. There is no DB row
> that injects front-end JS.

## The routing subtlety (why the Caddyfile has a `@canvas` block)

`canvas.js` requests its endpoints with **origin-absolute** paths —
`new EventSource('/events')`, `<img src="/img/…">`, `fetch('/ctl' | '/status'
| …)` — not relative to `/canvas`. So proxying only `/canvas/*` loads the
iframe page but its stream and images resolve to `<owui-origin>/events` etc.,
which fall through to OWUI → a black canvas.

The Caddyfile fixes this by **also** routing the canvas's own root endpoints to
`:8781` (the `@canvas` matcher — a closed list of `canvas.py`'s top-level
routes, verified not to shadow any OWUI route). Keep that list in lockstep with
`canvas.py`'s `do_GET`/`do_POST` dispatch. `smoke-test.sh` step 4 guards it.

## It's live on red5

Deployed and verified: **<https://red5.eagle-dubhe.ts.net:8443>** shows OWUI with
the canvas behind it. Key constraint: red5 **already runs a system Caddy**
(`/etc/caddy/Caddyfile`, `:80`/`:443`) fronting the public `mel.ryer.org` Matrix
homeserver — that one is **not touched**. The OWUI front door is a **separate,
isolated Caddy instance** so the injection can never affect the Matrix box.

How it was stood up (all under `~/owui-caddy/`, nothing in `/etc`):

1. **Plugin Caddy, no build:** downloaded a prebuilt binary with
   `replace-response` from the official build service (`caddy` here is v2.11.4)
   — the stock system Caddy (2.6.2) lacks the `http.handlers.replace_response`
   module. Verified with `caddy list-modules | grep replace`.
2. **TLS:** `sudo tailscale cert --cert-file red5.crt --key-file red5.key
   red5.eagle-dubhe.ts.net` (a real Let's-Encrypt-via-Tailscale cert; valid ~90
   days — **renewal is the one open follow-up**, see below).
3. **`~/owui-caddy/Caddyfile`** — the template here, adapted for red5:
   - site `red5.eagle-dubhe.ts.net:8443` with explicit `tls red5.crt red5.key`;
   - upstreams at the **tailnet IP** `100.103.43.93` (`:3000` OWUI, `:8781`
     canvas) — both **refuse on `127.0.0.1`**;
   - globals `order replace after encode`, plus `admin off` +
     `auto_https disable_redirects` so it stays off the system Caddy's `:2019`
     admin and `:80` redirect.
4. **Persistence:** systemd **user** unit `owui-canvas-caddy.service`
   (`~/.config/systemd/user/`, enabled; linger is on), so it survives reboot —
   same pattern as the other `agent-media-*` user units.
5. **Verified:** `smoke-test.sh`-style curls (bg.js served, `/canvas/` loads,
   root `/events` + `/status` proxy, and the `<script …bg.js>` tag injected into
   OWUI's HTML) **plus** a headless Playwright load confirming the iframe renders
   the real canvas (`#dot` inside) behind the OWUI login form.

> **Open follow-up — cert renewal.** Caddy's `tls <file> <file>` does **not**
> auto-renew a `tailscale cert`. Before ~Oct 2026, re-run step 2 and
> `systemctl --user reload owui-canvas-caddy.service` (a weekly root timer would
> automate it; not set up yet since `tailscale cert` needs root).

### Redeploy / iterate

`bg.js` is served `no-cache`, so edits to `../static/bg.js` show on a browser
refresh — no Caddy reload. Config changes: edit `~/owui-caddy/Caddyfile` then
`systemctl --user reload owui-canvas-caddy.service`.

### Known polish (not blocking)

The OWUI **transparency selectors are version-brittle** (v0.10.2). Handled so
far: `html/body/#app` + OWUI's full-viewport `bg-white dark:bg-black` backdrop
div (without which the canvas is fully hidden). The **chat-page scrim**
(`main`, `[class*="messages"]`) is best-effort and unverified against the
logged-in chat DOM — re-tune if bubbles are hard to read over the artwork.

## Framing

Current `canvas.py` sets **no** `X-Frame-Options` / CSP `frame-ancestors`, so
the same-origin iframe is already allowed — nothing to relax. If a future OWUI
ships a restrictive `frame-src`, drop it with `header_down -Content-Security-Policy`
in the OWUI `handle` block (commented in the Caddyfile).

## e-ink (PineNote)

Open OWUI with `?eink=1`. `bg.js` reads it, sets a white iframe background, and
passes it through to the canvas page (`/canvas/?eink=1`) so the canvas runs its
DU4 e-ink mode — no motion, no video, line-art + text. The scrim also flips to a
near-opaque white so bubbles stay legible on the 4-grey panel.

## Alternative: canvas on its own host (cross-origin)

If you'd rather not share OWUI's root namespace with the `@canvas` routes, serve
the canvas on its own hostname and point `bg.js` at it:

```
canvas.example.ts.net { reverse_proxy 127.0.0.1:8781 }
```

and inject a one-liner before `bg.js`:

```html
<script>window.AMC_BASE = 'https://canvas.example.ts.net'</script>
```

Now every canvas request is first-party to *its* origin, so no `@canvas` block
is needed. The only cost: the parent→iframe autoplay poke in `bg.js` no-ops
across origins (harmless — the user's own tap on the controller unlocks audio).
`bg.js` also honors `?canvasbase=<v>` on the OWUI URL for the same override
(that's how the headless harness points it at a throwaway server).

## Test

- **Headless client test:** `../tests/browser/bg-harness.js` (Playwright) drives
  `bg.js` over a throwaway canvas — iframe loads, FAB flips
  `pointer-events`/z-index front↔back, `?eink=1` white/no-motion path, and the
  OWUI transparency + scrim. See `../tests/browser/README.md`.
- **Wiring smoke test:** `./smoke-test.sh` (above).
