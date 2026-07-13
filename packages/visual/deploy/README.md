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

## Deploy reality (as of this stage)

OWUI runs on **`red5:3000`** (podman quadlet `open-webui`, user unit
`open-webui.service`) with **no Caddy in front of it**. `bg.js` *requires* a
reverse proxy (for both the injection and the same-origin canvas routes), so
standing that proxy up in front of the live OWUI — and any OWUI restart/config
change it entails — is deferred to a deploy step with sign-off. This stage
ships the asset + the Caddyfile + this doc.

To bring it up:

1. Build Caddy with `replace-response` (above).
2. Drop `owui-canvas.Caddyfile` in place, set the tanet hostname + ports
   (`:3000` OWUI, `:8781` canvas), reload Caddy.
3. `./smoke-test.sh https://owui.<your-tailnet>.ts.net`
4. Walk the browser checklist the smoke test prints.

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
