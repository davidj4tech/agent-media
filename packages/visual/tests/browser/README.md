# Canvas browser harness

Headless Playwright verification of the canvas *client* JS — the parts pytest
can't reach: SSE watchdog/self-heal (#137), poll cadences and hidden-tab gating
(#141), the keyboardless token-sheet/toast/offbar flows (#142), and e-ink toast
legibility (#146). 15 checks, ~4 minutes, screenshots for eyeballing.

It spins up a **throwaway** canvas on `127.0.0.1:8791` (env
`MEDIA_VISUAL_TRUST_TAILNET=1`, video poller off) behind a stallable TCP proxy
on `:8792` — pausing the proxy's sockets is the only way to reproduce a
*silently* stalled SSE stream; killing the server only exercises `onerror`.
The live wall service is never touched.

## Setup (once)

```sh
cd packages/visual/tests/browser
npm install                      # playwright (browsers not downloaded)
npx playwright install chromium  # ~115 MB into ~/.cache/ms-playwright
```

## Run

```sh
node harness.js                  # tests this repo's packages/visual/src
MEDIA_HARNESS_SRC=~/some-worktree/packages/visual/src node harness.js   # a branch
```

Exit 0 = all pass, 2 = failures (see `results.json`), 3 = harness error.
Screenshots land in `shots/`. Ports override via `MEDIA_HARNESS_PORT` /
`MEDIA_HARNESS_PROXY_PORT`.

## bg.js — the OWUI background loader

```sh
node bg-harness.js               # 11 checks, ~30s, screenshots 10–12
```

A second, smaller harness for `../../static/bg.js` (the canvas-behind-Open-WebUI
loader — see `../../deploy/README.md`). It serves a **mock OWUI shell**
same-origin with a throwaway canvas, loads `bg.js` into it with `?canvasbase=`
(so the iframe points at the throwaway `/`), and asserts: the iframe loads the
real canvas (`#dot` inside), it's behind + click-through by default, the ▣ FAB
flips it front↔back (`pointer-events`/z-index), Esc backs it out when OWUI holds
focus, the OWUI transparency + chat scrim land, and the `?eink=1` path is white
with the canvas in DU4 e-ink mode. Results in `bg-results.json`; ports override
via `MEDIA_BG_PORT` / `MEDIA_BG_PROXY_PORT`. Same `/input`+`/ctl` route-blocks.

## Gotchas

- **`/input` is always route-intercepted in the browser.** With
  `TRUST_TAILNET=1` a real POST `/input` would inject keystrokes into live
  tmux `claude` panes. If you add tests that navigate to new pages, add the
  route there too, first.
- The throwaway instance reads the live house speech state, so when anything
  is speaking the SSE stream carries real state frames and the 15s idle ping
  never fires — T2 accepts either (both stamp the watchdog).
- The agent tree re-renders under your element handles on every poll; query
  selectors fresh per interaction, never hold handles across a poll window.
- T5 needs at least one tmux `claude` pane visible to the throwaway instance,
  or the agents pill never renders.
