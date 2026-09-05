# Reply from the player — where it got to

2026-09-05, out of the session that built it. The design and its arguments are
in `docs/proposals/2026-09-04-reply-from-the-player.md`; this is the state of
the thing and what is left.

## What exists

**agent-media.** `packages/visual/.../reply.py` plus four canvas endpoints:
`GET /conversation` (is this item a conversation I may reply to),
`GET /conversation/log` (the turns as readable lines), `POST /reply`,
`POST /focus`. Auth is the caller's own Audiobookshelf bearer, verified by
handing it back to ABS's `/api/authorize`; root may reply by default, anyone
else by name in `MEDIA_REPLY_USERS`. A dead session is revived in a background
tmux window and typed into. A reply is also rendered to speech in a second
voice and written to speech history, so the listener's own words become a turn
(`book_tracks.record_listener_turn`).

**Sasonica** (`sasonica` branch). `components/item/ReplyBox.vue` — growing
textarea, mic button, floating jump button — and
`components/item/ConversationLog.vue`, the transcript that stands upstream's
ChaptersTable down. Plus `AbsSpeechInput` (system speech recogniser), Android
TV support, and the keyboard and session-payload fixes below.

Deployed: red5's canvas is restarted and serving; the app is installed on the
phone and the living-room TV.

## What is left

1. **The session payload still grows.** Fixed today by removing duplication
   (1.7 MB → ~530 KB) — see below — but what remains is `audioTracks`, about
   1.2 KB per track, growing with every turn. The failure returns eventually.
   Two moves, in order of honesty:
   - **One audio file per turn** instead of per sentence. This conversation
     would be ~50 tracks rather than 421. Also the right structure: 421
     chapters is not a table of contents. Buys ~8x; does not remove the class
     of problem.
   - **Find out whether `audioTracks` needs to cross the bridge at all.** The
     native side is what plays; the web layer wants it for the scrub bar and
     track mapping, and possibly little else. If it can be dropped or reduced,
     the payload stops scaling with the conversation, which is a real fix.
2. **No quote from the app.** The server takes and clips a `quote`; the app
   sends none, because the player keeps its time in a component rather than the
   store and nothing exposes the current chapter. Quoting the *last* turn
   instead would be confidently wrong at chapter three.
3. **`mode: "branch"`** is implemented and tested server-side. No button.
4. **The auto-play expectation.** David expects the mini player to show when a
   reply auto-plays from the Stop hook. It cannot today: that audio goes out
   through our own stack to mpv, and Sasonica only knows about sessions it
   started. Making it true means the app plays the conversation instead of mpv
   — the absorption path in [[abs-fork-direction]] — and moves audio focus,
   ducking and the call guard off mpv. Substantial.

## Four things learned the hard way

- **A plugin event reaches the WebView as one JavaScript string.** Past some
  size it does not arrive, silently: native playback runs, the server records
  the listening, and no player is ever drawn because the web layer never learns
  a session exists. 1.7 MB failed, 530 KB works, the ceiling between them is
  unknown. Do not diagnose this from the app — the *server's* session list
  (`GET /api/sessions`) proves whether playback happened at all, which is what
  split "the player is broken" from "the player was never told".
- **The keyboard is an inset, and MainActivity consumes every inset it is
  given.** `windowSoftInputMode=adjustResize` is inert on an edge-to-edge
  window that manages its own margins; the fix was to include the IME inset in
  the WebView's bottom margin.
- **ABS metadata writes take arrays.** `seriesName` and `authorName` are the
  read side of the same fields; writing them is accepted and ignored. Series is
  `[{name, sequence}]`, authors `[{name}]`. Folders are never renamed — a
  renamed folder is a new item with no progress — so titles, series and author
  are set through the API and re-applied on every publish.
- **`_record_turn` renders on a background thread**, which outlives the fixture
  that redirected the state store: a test that left it live wrote a
  conversation called "You: hi" into the real library. It is stubbed in the
  fixture now, with a test that fails if that is undone.
