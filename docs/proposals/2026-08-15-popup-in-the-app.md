# The popup, on the phone's screen

Companion to `2026-08-13-android-companion-app.md`. Nothing built yet — this is
the spec, pending David's go-ahead.

Target device: Pixel 8a (p8a), Android 17. The app is `android/companion`.

## The ask

> "I'd also like the pop-up functionality to be built into the Android app."

Today the phone gets three media cards: play, pause, next, a title, a scrub bar.
That is the transport and nothing else. Everything the control popup can do —
replay the last clip, step a sentence, mute durably, seek by timecode, open a
URL, browse chapters or clips, switch channel, ask pi — needs a terminal, a tmux
server and an ssh session, which on the phone means Termux and a keyboard.

The popup is the control surface for agent-media. The phone is the device that
is always in the room. That gap is the whole of this proposal.

## What "the popup" actually is

Read off `packages/core/tmux/media-popup-help`, which is the honest inventory:

| group | keys |
|---|---|
| all channels | Tab/n channel · Spc play/pause · h/l ±5s · H/L ±30s · -/= volume · </> prev/next · g source · a ask pi · f fleet doctor · q close |
| speech only | r replay last · p clip at cursor · m mute now · M keep muted · v highlight · c clip browser |
| book & music | s typed seek · o open URL · w web UI |
| music only | c chapters |
| speech & book | [ ] speed · ⌫ reset |
| mouse | channel glyph · close · title→source · prev/next · progress bar→play/pause · wheel→volume |
| pane behind | PgUp/PgDn · Home/End · Shift+arrows |

Plus seven nested popups, each its own screen: `media-popup-{help,clips,chapters,docs,fleet,link,open,search}`.

Sorted by what a phone can actually do with them:

1. **mpv property writes** — play/pause, seek, volume, speed. The app already
   does these: three loopback bridges, `SideChannel` drives two of them. No new
   transport needed, and they work with red5 unreachable.
2. **`media` subcommands** — replay, sentence/paragraph skip, bookmark, typed
   seek, open URL, search, chapters, clip browser, web UI, mute-keep. These are
   argv, and the argv is already written down twice (see below).
3. **tmux-world actions** — go to source pane, clip at cursor, highlight
   follow-along, ask pi, fleet doctor. They only mean anything with a terminal
   at the other end, but the phone is a perfectly good button for them.
4. **pane-behind scrolling** — meaningless here. Drops out.

## The thing to avoid: a third dispatch table

The popup's `handle_key` (bash) and the canvas's `ctl_argv` (Python,
`packages/visual/src/agent_media_visual/canvas.py`) are the same table written
twice — the canvas one says so in its own docstring: *"The maps mirror the
popup's handle_key dispatch."* Writing it a third time in Java is the moment
those three start to drift, and the drift will be silent: a key that works in
tmux and does nothing on the phone reads as a broken app, not as a missing row.

**So the first piece of work is not in the app at all.** Promote the action
table into `packages/core` as the one place a channel+action becomes `media`
argv, have the canvas import it, and give it the tests it has never had. Then
the app is a front-end over a contract, and so is every future front-end.

This is the same shape as the control-surface work already agreed for Emacs:
independence, dispatch per-action.

## Two transports, on purpose

The app should not route everything through one pipe:

- **Local (127.0.0.1)** — the mpv bridges it already speaks. Play/pause, seek,
  volume, speed. Instant, no round trip, and correct when red5 is off, asleep or
  out of tailnet range. This is what makes the app a *device* rather than a
  remote control.
- **Remote (the action endpoint)** — everything in groups 2 and 3, which is to
  say everything that needs `media` and the state that lives on red5.

A control that silently needs the network is worse than one that says so, so the
UI has to show which half is live. The status server's existing `/state` already
carries `connected` per channel; the screen should read from the same place.

## Where the action endpoint lives

Two candidates, and this is a real decision, not a detail:

**(a) red5's canvas `/ctl`.** Exists today, token-guarded, backed by `ctl_argv`,
and the canvas page already drives it. The app would be a second client of a
working endpoint. Costs: a tailnet round trip for every button, a token to
provision on the phone, and nothing works when red5 does not.

**(b) a Termux-side action server on loopback.** p8a already has `media` on
PATH and a shell it runs in; a small local HTTP endpoint would put every action
on-device, need no token (loopback, same posture as `mpv-*-bridge-local` and the
app's own `:8770`), and keep working for the local half when red5 is away. Costs:
a new service to run and supervise, and `media` on the phone still resolves the
speech target remotely — so "local endpoint" does not mean "works offline",
only "no token, no tailnet hop".

Recommendation: **(b)**, because the app already talks to loopback for
everything else and the auth story is the one already accepted for the bridges.
But it is David's call, and (a) is a working endpoint today, which is worth a
lot for stage 2.

## Native or a WebView?

**A. WebView onto the canvas.** The canvas page already has a control row, key
handling, sheets for typed seek and open-URL, and a channel switcher. Point a
WebView at `:8781` and the popup is "in the app" this afternoon. But it is a wall
display in a phone-shaped hole: it wants a screen that is always on, it is
useless without red5, and every touch target was drawn for a room, not a thumb.
Good stopgap, poor destination.

**B. Native.** Views over the two transports above. More work, but it is the
only version that survives red5 being off, that can use the phone's own
affordances (long-press, swipe, hardware volume keys), and that can be reached
from the lock screen.

**C. Shade only** — more notification actions on the three cards. Cheap, and it
is where a couple of these buttons genuinely belong (replay on the speech card),
but the shade cannot hold thirty controls and this app is already careful about
notification churn.

Recommendation: **B for the controls, A for the browsers.** Rebuilding the clip
browser, chapters, docs and search as native list UIs is most of the work for
least of the value; each already has a web UI or a popup that renders fine in a
WebView. Native where the thumb lives, WebView where the list lives.

## Stages

Each stands alone and each is worth having on its own.

- **0 — one table.** Extract `ctl_argv` into core, canvas imports it, tests for
  every row. No behaviour change anywhere. *(Prerequisite for everything else.)*
- **1 — the endpoint.** Decide (a) or (b), stand it up, exercise it with curl.
- **2 — the first screen.** Channel switcher, transport, volume, speed, and the
  popup's status line. Transport goes local; the rest through the endpoint. This
  is the popup's two visible rows, and it is the release that makes the app
  useful without Termux.
- **3 — the verbs.** Replay, sentence/paragraph steps, mute and keep-muted,
  bookmark, highlight, go-to-source; the two sheets (typed seek, open URL).
- **4 — the browsers.** Clips, chapters, docs, search, fleet — WebView, one
  entry point each.
- **5 — parity pass.** Hardware volume keys, lock-screen reach, what the app
  does when red5 is unreachable, and a written map of popup key → app control so
  the next drift is visible.

## What this does not do

It does not retire the tmux popup. That surface is where David works, it is
faster than a phone for anyone already in a terminal, and it is the reference
implementation the app is measured against. This is a second front-end, and the
whole point of stage 0 is that a second front-end should be cheap.

## Risks

- **Every iteration is a sideload.** No adb on p8a, no logcat for our uid. The
  existing discipline holds: keep decisions in android-free classes with host
  tests (`test/run.sh`), and put anything worth reading into `/state` and `/log`.
- **Notification churn and the addressed-player slot.** The app's most delicate
  property. A control screen should not be posting notifications at all, but any
  new session or card must respect the rule: only music opens an AudioTrack.
- **Play Protect.** No NotificationListenerService, ever. Nothing here needs one.
- **Scope.** This is the largest single piece of the app so far — larger than the
  focus policy. Stage 2 is the one that has to land; 3–5 can be paced.

## Open questions for David

1. Native controls with WebView browsers, or WebView throughout for now?
2. Endpoint on the phone (new local service) or red5's existing `/ctl`?
3. Must the app be useful with red5 unreachable? (It decides how much lives
   local, and it is the difference between a device and a remote control.)
4. Is the phone's screen the target, or the lock screen and the car display too?
5. Which of the popup's actions do you actually reach for from the phone? The
   inventory above is complete, not prioritised — stage 2 should carry the five
   or six you would use standing in the kitchen, and the rest can wait.
