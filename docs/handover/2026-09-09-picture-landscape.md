# Landscape for the picture under a message — what shipped, and what is left

2026-09-09, David's call, straight after the canvas simplify
(`2026-09-09-canvas-simplify.md`). "I like the canvas link and image under the
message. one thing — can it be displayed in landscape when clicked."

He chose **page now, app later**: ship the browser answer today so it works on
every surface, then fold the automatic in-app rotation into the next APK.

## Shipped (4f13448, plus 1162282 and f8045ad)

- The canvas page grew a fullscreen button (top-right, invisible at rest,
  revealed by any touch or mouse move) that also locks the screen landscape.
  Withheld on e-ink — DU4 does not move, and rotating the screen is the largest
  movement there is.
- `/img/<name>` now answers two questions at one address. An `<img>` gets the
  bytes; a top-level navigation gets a viewer page — the picture whole, with
  that same fullscreen-plus-landscape button. **`Accept` tells them apart**;
  anything that does not say gets the bytes. `?raw=1` declines the viewer,
  `?view=1` demands it.
- **Nothing in the app changed.** `ConversationLog.openPicture` already calls
  `Browser.open` on exactly this URL, so the viewer arrived with a canvas
  restart on red5 and p8a. Same for sasonica-web.

Tests: `packages/visual/tests/test_view.py` (8), browser harness T17a–e.

## Left — the two things that need an APK

Both are in `~/projects/sasonica` (branch `sasonica`), and both want the CI
`sasonica-apk` artifact + `adb install -r` (memory: [[sasonica-fork]]).

1. **`CanvasPanel.vue`'s iframe** — DONE in the fork (073156d6,
   `allow="autoplay; fullscreen"` + `allowfullscreen`), **waiting on an APK**.
   Measured either side against the live canvas: `allow="autoplay"` gives
   `fullscreenEnabled` false and no button; with `fullscreen` delegated, true
   and the button. This is what David reported as "I don't see the full screen
   features" on 2026-09-11.

2. **An in-app picture viewer, so it is no taps rather than one.** The browser
   rule is the whole reason the shipped answer costs a tap: a page cannot
   fullscreen or rotate itself on load, only inside a gesture. A native viewer
   has no such rule — a full-screen `<img>` modal in the app plus
   `setRequestedOrientation(LANDSCAPE)` on open and `UNSPECIFIED` on close is
   automatic. The manifest has no `screenOrientation` lock, so nothing has to
   be undone first; there is no `@capacitor/screen-orientation` in
   `package.json` yet, so it is either that plugin or a few lines beside
   `SasonicaControl.kt`.

   Keep the viewer page when this lands: it is the only answer sasonica-web and
   a plain browser tab will ever have, and it is what `Browser.open` falls back
   to if the native viewer is ever skipped.

## 2026-09-11: the viewer never fired on a real phone

The route shipped reading `Sec-Fetch-Dest`, and every tap kept getting a bare
image. `Sec-Fetch-*` is attached only to **potentially trustworthy** origins —
https, or localhost. The canvas is plain http on a tailnet host, so Chrome sends
none of it, the header was absent, and the absent case falls to the bytes by
design. Measured: a navigation to `http://red5:8781` carries no `Sec-Fetch-*`
at all; the same navigation to `http://127.0.0.1:8781` carries the lot.

**Every test was the case that works** — the unit tests set the header by hand,
the browser harness drives 127.0.0.1. Fixed in 954fcae: `Accept` leads (a
navigation names `text/html`, an `<img>` never does, and `*/*` is not a request
for a page), `Sec-Fetch-Dest` still confirms where it exists, and both answers
carry `Vary: Accept, Sec-Fetch-Dest` with the viewer `no-store` — one address
with two bodies and a day of `immutable` on one of them is a cache waiting to
serve the wrong one.

The lesson worth keeping: **this house is plain http over a tailnet, and a test
on 127.0.0.1 is a different security context from every screen in it.** Anything
that turns on a browser's own judgement of the origin has to be proved against a
hostname, not loopback. See [[plain-http-hides-sec-fetch-headers]].
