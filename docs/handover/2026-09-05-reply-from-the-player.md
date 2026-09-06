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

1. ~~**The session payload still grows.**~~ Answered, pending a sideload.
   `sasonica` `55a477c2` takes `audioTracks` off the bridge entirely rather
   than making it smaller: the question the previous session left open — does
   it need to cross at all — measures as no. It is read in exactly one place
   in the whole web layer, the browser-only fallback player in
   `plugins/capacitor/AbsAudioPlayer.js`, which builds its own session from the
   server and never receives the event. The scrub bar's duration and position
   arrive on `onMetadata`; the chapter comes from the session's top-level
   `chapters`. The local item's file and track lists went the same way (the
   player reads only its id and cover), and `prepareLibraryItem`, which was
   resolving the untrimmed session to two callers that discard it, now sends
   the trimmed one. The payload no longer scales with the conversation, so one
   file per turn is a structure argument now — 421 chapters is not a table of
   contents — and not a payload one.

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


## What the night of 2026-09-06 added

* **The item comes through the canvas.** `GET /item` serves an Audiobookshelf
  library item carrying only what the app reads — 1267 KB becomes 138 KB, 29 KB
  on the wire. Measured from the phone, that took the item page from **51–86
  seconds to 2.4–5.5**, because the link to Falkenstein is 448 ms and the size
  was the whole delay. A shortcut, never a dependency: the app falls back to
  Audiobookshelf when the canvas does not answer.
* **The canvas serves more than one Audiobookshelf.** Identity is asked of each
  server in `ABS_URLS` until one recognises the bearer, and the server that did
  is remembered with it. Before this, an app signed in to a second server was
  told its login was rejected — and answered that the way it answers any 401,
  by refreshing and eventually logging the user out.
* **The transcript keeps up**: the reply box says when a reply is accepted, and
  the log re-asks three times after that and every fifteen seconds while the
  page is up, stopping when the app is backgrounded.
* **No audio-file table.** Hundreds of rows of `sentence-042.opus`, built even
  when collapsed, because the bar toggles `v-show`.
* **`agent-media.env.example`** at the repo root, with a test that fails if it
  names a key nothing reads.

Still open from the list above: the quote (2), the branch button (3), and the
auto-play mini player (4) — though `MEDIA_PLAY_FROM_SEARCH`, which upstream
already implements, may make (4) far cheaper than it looked.
