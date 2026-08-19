# The canvas in the app

*2026-08-18*

The canvas is a web page — `packages/visual`, an SSE-fed image surface on
`red5:8781`, viewed in Chrome on the phone. This proposes moving it inside the
companion app, and is deliberate about what that does and does not buy.

## What the app actually adds

Not rendering. `canvas.js` is 54K of working client, the focus ring is a
tested state machine, and the headless harness already covers it. Redrawing
that in `Canvas`/`ImageView` would be weeks of work to arrive back where we
started, with the harness thrown away.

What the app adds is everything *around* the page:

- **The screen wakes when a figure arrives.** A browser tab cannot do this.
  This is the single strongest reason to move, and it is the reason a
  `[[reveal:]]` marker currently only works if you happen to be looking.
- **The stream stays open.** `CompanionService` is already a foreground
  service that survives doze; a Chrome tab is discarded whenever Android feels
  like it, and the reconnect is a page you have to go and find.
- **No Vimium.** `p` no longer opens the clipboard as a URL (see
  `vimium-canvas-exclusion` — this has cost real debugging time twice).
- **No tab management.** It is an icon, not a URL you re-find.
- **One configuration.** The app already knows which agent-media this phone is
  a client of (`Server`/`Settings`). Today the canvas URL is remembered
  separately, by the browser, and diverges silently.

So: a **WebView shell**, not native views. The app supplies the lifecycle; the
page supplies the pixels.

## Phase 0 — the spike that decides it

One `CanvasActivity`, hardcoded URL, no config, no entry point beyond a
temporary button. Perhaps an hour's work, and it answers the only questions
that can sink this:

1. **Cleartext.** `http://red5:8781` over the tailnet needs
   `usesCleartextTraffic` or a `network-security-config` naming the host.
   Prefer the config — a per-domain exception, not a blanket one.
2. **Does the page work under WebView at all?** It is a modern page: SSE,
   `localStorage`, CSS the desktop Chrome renders happily. Android System
   WebView is Chrome, so this should be uneventful — but "should be" is what a
   spike is for.
3. **Touch.** The canvas grew up with a keyboard (`n` = channel, the mode
   machine). On a phone there is no keyboard. Does the existing touch
   controller carry enough, or does the app need its own controls over the top?
   This is the finding most likely to change the plan.
4. **Auth is not in the way.** The amux token guards `/input` only — keystroke
   injection. An unpaired WebView still *sees* everything. So phase 0 needs no
   credential at all, and the token question defers to phase 2.

If touch turns out to be threadbare, stop and reconsider before building any
of the below.

## Phase 1 — make it the app's canvas

*Built 2026-08-19.* What landed, and the two things it decided differently
from the sketch below:

- **The canvas gets an address, not just a port.** The sketch said "a canvas
  port on the host already configured". That is wrong for the arrangement this
  fleet actually runs: media-share is in Termux on the phone (loopback) and the
  canvas is on the machine producing the speech (red5). Neither derives from
  the other — the app cannot infer a host it has never been told about. So
  `Server` grew `canvasHost` (empty = the server's own host, which is the
  common case) beside `canvas`. Same shape of argument as `mpvHost`, from the
  other end.
- **`canvasProblem()` is separate from `problem()`.** Folding it in would let
  a mistyped canvas port fail `orDefaults()` and take the music, the transport
  and the share sheet down with it. A canvas that cannot be reached should cost
  the canvas. It also replaces phase 0's hardcoded `red5`: an unconfigured
  install is now *told* the canvas is not on this phone, rather than being
  quietly pointed at a hostname it never chose.
- **The keyboard**, which the sketch did not mention and which is the whole
  point of carrying the canvas onto the phone: `/input` types into the pane
  that last spoke. This window hides the system bars and draws to every edge,
  so it gets no automatic help when the IME arrives — the keyboard simply
  covers `#inp`. The IME inset is now applied by hand as bottom padding on the
  WebView (`adjustResize` + `setDecorFitsSystemWindows(false)`), which shrinks
  the layout viewport so the page's `position: fixed` dock rides above it.

**Not yet verified on a device.** p8a has no adb from red5, so the keyboard
behaviour above is reasoned, not observed. It is the first thing to check on
the next sideload.


- `WebSettings`: JavaScript on, **DOM storage on** (the pairing token lives in
  `localStorage`; without this it is re-paired forever).
- `FLAG_KEEP_SCREEN_ON` while the activity is visible. A canvas that sleeps
  mid-figure is worse than a browser tab.
- Immersive full-screen; back button finishes.
- Config: `Server` grows a canvas port (default 8781) beside the existing
  control/music/speech/book ports, on the host already configured. One field
  in `SettingsActivity`, same file, same validation.
- Entry point: an action on the **speech card** — the canvas illustrates
  speech, and that is where you already are when a figure lands. Deliberately
  *not* a fourth tab yet: the tab strip swaps a card, and the canvas is a
  full-bleed surface, not a card. Revisit once it has been lived with.
- Pairing, if `/input` is wanted from the phone: the app loads `/pair?c=<code>`
  once. Minting still needs shell on red5, which is correct — no HTTP path can
  create a code.

## Phase 2 — the part only the app can do

`CompanionService` subscribes to `/events` (or is poked by the existing
channel plumbing) and, on a figure arriving:

- wakes the screen and shows `CanvasActivity`, or
- posts a channel notification if the phone is face-down / in pocket.

This is where the move pays for itself, and it is worth being conservative:
waking the screen for every ambient artwork would be intolerable. Likely only
`[[reveal:]]` — the marker that already means "the words wait for the
picture" — should wake anything. Ambient art can arrive quietly.

## Phase 3 — native views, if ever

Only if living with phase 1–2 produces a concrete complaint that a WebView
cannot answer (input latency, memory, e-ink). Sharing `Style.java` is
aesthetically appealing and is not, on its own, a reason.

## Costs and risks

- **A second rendering context in the app.** WebView is heavy — tens of MB —
  and the app currently runs lean because it draws its own views. Worth
  measuring in phase 0.
- **Two clients to keep working.** The desktop browser stays the primary
  canvas (the wall). Anything phase 1 changes in the page must not break it;
  prefer app-side wrapping to page-side branching on user-agent.
- **The harness stops covering the top layer.** `canvas.js` stays tested;
  the Activity around it does not, and Android has no equivalent harness here.
  Keep the Activity thin enough that this does not matter.

## Build note

Build the APK from a throwaway worktree so it is not stamped `+dirty`
(see `concurrent-sessions-commit-sweep`), and sideload — `adb` cannot reach
p8a from red5.
