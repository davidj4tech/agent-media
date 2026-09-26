# Sasonica roadmap

What is built, in flight and queued for Sasonica: the chat app and its
Android shell (Sasonica Next), the server here in agent-media, and
Sasonica Shell. One list, so any session (or David) can see where things
stand. Update it when an item lands or a decision is made.

Where things live:

- **Server:** agent-media `packages/server`, contract in
  [server-contract.md](server-contract.md). Python, and staying Python.
- **Chat app:** `~/projects/sasonica-chat/chat`, branch `chat-prototype`
  (React Router + assistant-ui). Preview on red5 `:8795`
  (`sasonica-chat-preview`).
- **Sasonica Next:** the Capacitor shell, repo `davidj4tech/Sasonica`,
  branch `android-next` (checkout `~/projects/sasonica-next`), rebased onto
  `chat-prototype`. CI builds `sasonica-next-apk`; install with
  `agent-phone-adb install`.
- **Sasonica Shell:** `~/projects/sasonica-shell` (formerly Runlet).
- **Licences:** agent-media, the chat app + Next, and Sasonica Shell are
  Apache-2.0, copyright South Pen Labs. The Audiobookshelf forks (the old
  Sasonica app, sasonica-web) stay GPL v3; never copy their code into
  `chat/`.

## Standing decisions

- New app work lands in **Next first**; the browser preview is a fallback.
- Keep publishing conversations to Audiobookshelf until the old app retires.
- Idle sessions close after 12 h (6 h when memory is tight); closed is not
  archived.
- Headless sessions are on, with normal permissions.
- Layout: `default` or `projects-per-tmux-session` (David's), detected by
  the installer.
- Optional extras (agent-memory, agent-mail, …) are opt-in in the Sasonica
  installer and detected at runtime; the app shows a feature only when its
  extra is present.
- **The distributable wiring is a Sasonica profile inside agent-media**
  (David, 23 Sep 2026), installed by `media-setup`: agent-media already owns
  the hooks, the services and the installer, so the profile is one
  definition with two consumers — agent-config includes it, and everyone
  else installs it on its own. agent-config stays David's personal layer.
- **It merges into the machine's own Claude Code config** (`~/.claude/
  settings.json`, backed up first, nothing else touched), because the point
  is that the phone drives the *same* sessions — same skills, same memory,
  same panes. A **Sasonica-managed config dir** (its own
  `CLAUDE_CONFIG_DIR`) is offered for people who do not want Sasonica in
  their settings; it costs them that sharing, so it sits under **Advanced**
  in the app (David, 23 Sep 2026) — the same per-device switch that hides
  the device id, the server address and search's tool steps. Merging is
  what an ordinary install does without being asked.

## Done

Pairing and device tokens; threads by session; transcript messages and
per-thread SSE; speech bar with full controls; follow-along; recaps; exit
and archive; idle closer; audio destination picker and Android output
switcher; multi-line sends; per-session stop; multi-select questions; Home
dashboard; background agents in a thread; thread-list sorting and filter
(Active / Live / Closed / Archived / Everything, one project, and any
number of states — Needs you / Working / Your turn; `/targets`
now lists every archived thread, past the 40-row cap; By project's
headings fold, kept per device; filtered to one project, the + starts the
new chat there, 23 Sep 2026); project
line under titles; menus that close on an outside tap; the brand and icons;
the About page; seven text sizes (9–21 px, Default 15), and a pinch
steps through them and saves the choice (24 Sep 2026); the digital-
assistant slot in Next; the coding-agents installer in Settings; Sasonica
Shell rename, named URLs and client labels; answered questions stop being
read out; tap to read from here (tap a sentence while it is spoken; the
selection chip on older replies was removed 25 Sep 2026 — they play from
their ▶; not yet tried on the phone with real audio); tables, code blocks
and links drawn as themselves in the chat, the described table one bold
step (25 Sep 2026); background notifications in Next (a `specialUse`
foreground service on `GET /sessions/events`, §6.13: "New reply" /
"Needs you", Settings toggle; untested on the phone: Doze, reboot, cold-start
tap, network handover); "From <name>" on another session's messages;
search (`GET /search`, §6.14: every thread's messages, titles, recaps and
projects from an incremental FTS5 index on red5, memory as its own section
when agent-memory answers; the app's ⌕ beside Show / Sort on Threads, with a
Show of its own seeded from the list's filter; a hit opens its
thread at that message, lit; Codex/pi/Hermes hits land by time, not by id);
the Advanced setting (per device, off: search's "Tool steps", the
follow-along lead, device id and server address, the legacy connection). Moving a
thread to another project (`POST /session/move`, §6.15: filed on the server,
its transcript refiled, a live session closed and reopened there; the picker
is in the thread menu and the list's long press). Naming a thread after its
first turn (§6.4: a headless one has no `ai-title`, so sessiond runs the
auto-rename once the opening turn is done — needs a sessiond restart to go
live). The "Follow along" pill resyncs the voice as well as the view (it
drops the skew window and asks `/speech/now` and the log at once, and the
skew estimate now corrects a bold left *behind* the voice — a skip taken at
the desk, a media key — not only one running ahead; `chat/test/resync.mjs`).
Every harness's conversations in the one list (§6.16: `harnesses.stored()`
sweeps Claude's, Codex's, pi's and Hermes's own stores, stat-only; every
`/targets` row carries `harness`, store-only rows carry `source: "store"`;
a 30-day window and 40 rows per harness, `?history=all` lifts it; the app
wears the agent as a chip on each row and filters by it, with "Older than
30 days" in the same menu).
Another conversation by chip instead of by title (David, 24 Sep 2026):
Share into… on a thread's long press and ⋮ puts `@[<title>]` at the end of
another thread's draft (or a new chat's) and opens it; `@` in the reply box
offers threads by title. The send carries `refs` and the server adds a line
per chip with the session and its transcript path (§6.3, `refs.py`); the
sent message shows the chip as a link to that thread (`chat/test/refs.mjs`).
Inside a thread its ⋮ offers Insert a thread… instead, which puts the chip
at the caret of the words being written (25 Sep 2026). Threads outside
~/projects are filed under their directory (`org`, `scratch`), never the
shelf's catch-all `conversation` folder. `media session-delete` removes a
thread everywhere (speech history, shelf, library item, search, flags),
backed up first and kept deleted; `codex exec` and /tmp runs are never
spoken or shelved (25 Sep 2026).

Codex and pi threads read from their own transcripts (§6.2.2: a reader per
harness in `transcript.py` — prompts, replies, thinking and every step with
its command and result, instead of one line per spoken sentence; Hermes
keeps a database, so it still answers from its lines).

Coding agents says what is out of date (23 Sep 2026): `GET
/harnesses/updates`, its own route because it is the only thing on that page
that goes to the network — the rows draw first and the update state fills in.
The three npm ones are compared against `npm view`; Hermes answers its own
`hermes update --check` (a fetch, ~10 s, no version — just behind or not).
Cached an hour, re-asked by ↻ and after an install. An agent that is current
has no Update button at all, one that is behind says `Update to 0.156.0`, and
"could not ask" shows nothing rather than claiming it is current.

Signing out of a harness, and not offering what cannot answer (23 Sep 2026):
Codex's sign-in from the phone dead-ended on `localhost:1455` — the browser
flow's callback is the *phone* — so it runs `codex login --device-auth`, a
code read off the pane. Coding agents now also has Sign out on any row it
says is signed in (`POST /harnesses/logout`, no window, asks first in the
row), New chat drops agents this host has not got and dims signed-out ones
with a link to the page, and `POST /ask` refuses a fresh chat with a missing
or signed-out harness (409) instead of opening a window that sits on the
harness's own sign-in screen and never answers.

The Organiser's own Show and Sort menus (chat `lib/noteSort.ts`, per device):
Show keeps or hides done and cancelled items (the view is asked again,
`/notes/view?done=1`), waiting and someday ones, and plain notes with their
section headings; Sort is File order, Date, Priority, Title or Recently
changed — anything but File order flattens the list, since a section heading
only means something where the file put it, and in the agenda it orders the
items inside a day, not the days. Merged to `chat-prototype` and on the red5
preview; in Next at the next CI build.

Typed tools for Sasonica Shell (`docs/tools-and-approvals.md` §1: a skill
declares actions with typed arguments in `~/.config/sasonica/tools/`, the
runner publishes name, description, schema and sha256 — never the argv —
and a call runs a fixed argv with `execFile`, no shell; first set is speak,
music now/pause/resume and memory search, live on red5).

Speech levels (24 Sep 2026): a thread's ⋮ menu (and the list's long press)
has **Speech…** — Interrupt (plays at once, and another chat's reply steps
aside at its next sentence), Auto speak (never held by the desk toast or
silenced by a pane mute), Normal, Quiet (archived unheard, never played by
itself); a badge unless Normal. `POST /session/priority {level}`, rows carry
`speech`, `media priority <level>` at the desk. agent-media `259b97e`,
android-next `35a2802a`; chat-prototype still has the older on/off toggle
(another session had the same files open).

Default speech priority (25 Sep 2026): the menu item is **Speech
priority…**; Settings has **Default speech priority**, the level of every
thread without its own, with a warning that it is the server's, so every
device's (`GET/POST /speech/default`). The thread's sheet tags the default,
links to Settings, and offers **Use the default** while the thread has its
own (`{level: "default"}`, rows' `speech_own`). Next's player section is
**Speech on this phone**. Per device was judged not worth it; with accounts
it becomes the account's. agent-media `3f722af` `12bd56e`, android-next
`006ccce9` `9e63029e`, chat-prototype `fa4aa8e3` `a2b7142b`.

Normal is called **When open** in the app (David, 26 Sep 2026): it plays
while the thread is open, or when you next open it. The level stays
`normal` on the wire and at the desk. A reply let through because its thread
was open is asked again when its turn to speak comes, and held if you have
left (agent-media `896ebdb`); the server logs each thread stream open and
close. chat-prototype `468e9cd3`, installed on p8a.

Follow along can be turned off (David, 25 Sep 2026): Settings → Follow
along → "Scroll with the voice", on by default, per device. Off, a spoken
reply no longer moves the view — it stays where you put it, no pill — and
the bold still marks the sentence (`lib/followOn.ts`, `test/follow.mjs` F6).
chat-prototype `f85a2acf`, android-next `046fa5f7`, installed on p8a.

A reply stops the reading (David, 25 Sep 2026): replying to a thread ends
its reply being read at the close of the sentence playing, and drops what it
had queued (the `read` cut; `/reply` and Claude Code's UserPromptSubmit hook).
While the thread is read, the reply box carries a "Stops reading" chip; a tap
makes it "Keep reading" for that one send (`keep_reading`, §6.3). Auto speak
threads are never cut. agent-media `5518ac7`, chat-prototype `5c3f8d1a`,
android-next `6861b775`, installed on p8a.

Earcons (David, 25 Sep 2026): made in code, no audio assets
(`earcons.py`) — a soft tick when a reply is cut short on purpose (a Stop, a
reply, an answered question), a rising two-note before speech that barges in
over another thread, a falling two-note when a reply is held behind a Play
(never for Quiet). Only on an idle player, never inside a reply's playlist.
`MEDIA_EARCONS=0`, or `MEDIA_EARCON_CUT` / `_INTERRUPT` / `_HELD`. End of
reply ticks too (David wanted it). Replay replays the thread the bar names,
or the one on screen — unscoped, it read out a reply held in another thread
(agent-media `91d912f`, android-next `8250849d`, chat-prototype `17cb2aa4`).

An interrupted reply can be resumed (David, 25 Sep 2026): a Stop, or a reply
that ends the reading, leaves the reply's own resume point (`extras.stopped_at`,
`spoken.resume`, no time limit). Its ▶ says Resume with "0:07 / 0:13" beside
it, a small restart plays it from the top, and the part not heard is dimmed
(mapped with the follow-along's sentences). Replay resumes it too; playing it
again clears it. agent-media `21721da`, android-next `514b6587`, chat-prototype
`3d36593e`, installed on p8a.

The assistant button talks into the thread on screen (David, 25 Sep 2026):
pressed while a thread is showing, that thread's composer listens (same 3 s
auto-send); from another app, the lock screen or a cold start it is a new
chat as before, and a second press within 20 s opens a new chat instead
(`AssistPlugin.java` `shown`, `NativeHooks.tsx`). Under the composer, for
8 s or while the words wait: "New chat instead" (the words go along) and a
chip per other assistant on the phone — those answering ASSIST that take
shared text, browsers left out, the Google app handing to Gemini; with no
words it opens that assistant. New chat shows those chips all the time.
Unverified: whether each one sends or only
fills its box. A thread's title wraps to three lines, not two. android-next
`5873fac6`, `d29db103`, `858fbffc`, `1429cdea`, installed on p8a.

Share to Sasonica (David, 25 Sep 2026): Next is on the share sheet for text,
links and files (SEND / SEND_MULTIPLE, any type; `ShareInPlugin.java`). The
share screen (`routes/share.tsx`, also `/share?text=&title=&url=` on the
web) shows the words, editable, and the files, and offers New chat, Into a
thread… (by title, like Share into…), Organiser inbox (a TODO), Play it (a
link only, `/share`) and Just keep them (files only). The words land at the
end of that thread's draft and it opens. Files are streamed from native code
to `POST /upload` (§6.18), kept in `~/shared/<day>/`, and a
`Shared file: <path>` line per file goes with the words. Not yet: Android's
direct-share icons for recent threads. The reply box attaches too (David,
26 Sep 2026): a paperclip opens the WebView's file chooser (Files, Photos,
the camera); each file goes to `/upload` from the page and the same line lands
at the caret (`AttachButton.tsx`, android-next `a77132ec`).

New chat is one line, not two chip walls (David, 26 Sep 2026): "agent-media ·
Claude ▾" opens a sheet with the places (newest first) and the agents, so a
message of several lines keeps the screen. The other assistants' chips show
only while the box is empty (the assistant button's offer stays), and fold
into one "Other assistants ▾" chip with a sheet (one alone keeps its chip; New
chat instead stays a chip; opening it stops the send countdown). android-next
`581892e9`, `fa6937dd`, installed on p8a.

## In flight

**Next's own speech player.** Media3 (David, 23 Sep 2026: the reason
MediaPlayer was chosen — the companion had no Gradle build — is gone). Built:
`com.sasonica.next.speech` — `MpvServer`, `Json` and `ClipCache` carried over
from the old app unchanged (companion-origin, Apache-2.0), a new
`Media3Speech` on ExoPlayer behind the same `MpvServer.Player` interface, and
`SpeechService`, a `mediaPlayback` foreground service that binds the phone's
tailnet address on **6614** (the old app keeps 6613, so both run). A Settings
toggle, off until turned on; `MEDIA_SPEECH_SOCKET_NEXT=tcp://p8a:6614` and
`media speech-target next` point red5 at it. The protocol test came across to
JUnit and runs in CI. Next: build it on CI, sideload beside the old app, and
listen — the speed at 1.6x and the gap between sentences are the two things
Media3 has to prove. **First reply through it played on p8a, 23 Sep 2026**:
two clips, the join fired on its own and volunteered `playlist-pos`, and
`idle-active` went true at the end.

**The hold tier in Next** (23 Sep 2026). `Holds.java` — dictation pauses a
reply and it carries on; a voice session or a call holds every reply until it
is over, with the Speak now / Later card; urgent takes the room. `MicWatch`,
`MicSteady`, `MicSource`, `BargeIn`, `DictationHold`, `HoldRate` and
`RingerState` came across unchanged, with their tests as JUnit. No `BookHold`
(Next has no book). Still to do: serve `/mic`, `/ringer` and the hold rate on
a port of Next's own, so `call_guard`, `ringer.py` and `media doctor` can
read them from Next instead of the old app's :8772.

The Organiser on plain Org, paragtd as a package (24 Sep 2026,
`docs/proposals/2026-09-24-notes-core-and-paragtd.md`): the layout is a notes
profile. Plain Org by default (your agenda files and `#+TODO` keywords, copied
once from Emacs by setup's `agenda` row), and paragtd's GTD files from
`packages/notes-paragtd`, found by the tree. paragtd writes `.paragtd.json`
(paragtd `c2af9ea`), and the profile reads its files and keywords from it.
Closing a sequenced step from the phone follows Org's dependency blocking and
runs paragtd's next-step trigger, and the Organiser says which step is next.
The app takes its keywords and refile targets from `GET /notes`. On the red5
server and preview; in Next at the next CI build. Then **More…** beside the
capture box: your capture templates (the manifest's, site ones too) filled
and filed as org-capture would, prompts drawn above the box; templates that
call Emacs functions (morning/midday/evening/visioning) stay in Emacs. And
`paragtd-astro.timer` (monthly, turned on from setup) keeps astro.org a year
ahead.

## Queued, in order

1. **Devices screen in the app** — the server half landed 23 Sep 2026
   (`GET /devices`, `POST /devices/code`, `POST /devices/revoke`,
   contract §9 "Enrolling from the app"). What is left is the screen:
   Settings → Devices, shown only when `POST /pair` said `enrol: true`, a
   row per device with when it was last seen, "Pair a device" (a name, then
   the code and its QR), and revoke behind a confirm. Steps 2–4 of
   `docs/proposals/2026-09-23-accounts-and-the-identity-seam.md` (OIDC, an
   issuer) stay parked until there is a second person.
2. **Sasonica Shell OAuth** — the proposal is written, not built.
3. **One setup for a machine, from the Coding agents page** (David, 23 Sep
   2026). A working machine needs three installers today: `media-setup`
   (agent-media's own hooks and services), Sasonica Shell's `install.mjs`
   (Worker, D1, keys, runner), and **agent-config**, which carries
   everything else — the mail inbox hook, the session autoname, the
   catch-up hook, the skills directory, `agent-media.env`. A fresh machine
   that runs only the first two looks set up and is missing half of it.
   Two parts:
   1. **The Sasonica profile** — **DONE 23 Sep 2026**: `media-setup profile`
      wires a machine in one command (hooks, services, shell, and every
      extra it finds, each skipped with a reason when its tool is absent),
      and `media-setup status --json` is the same rows for the page.
      `--config-dir` writes a Sasonica-managed config and records
      `CLAUDE_CONFIG_DIR`. agent-config now *calls* the profile instead of
      carrying its own copy of the speech hooks (agent-config `c36c128`) —
      which caught the drift that made it worth doing: its timeouts were
      right and agent-media's installer still wrote an older set (Stop at
      30 s instead of 120, no `async`, the wrong third event).
   2. **The page**: the app already installs and signs in harnesses (§6.6),
      so the same page shows *this machine's wiring* — hooks, services,
      skills, mailbox, catch-up — each with what is missing and a button
      that runs the installer that owns it. The server calls the existing
      installers, never reimplements them (`media-setup status --json` is
      already that list).
4. **opencode, and more than one account per harness** (David, 24 Sep
   2026) — `docs/proposals/2026-09-24-opencode-and-harness-profiles.md`.
   opencode becomes a fifth row in `RECIPES`; a *harness profile* (a
   harness plus its config dir: `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
   `PI_CODING_AGENT_DIR`, `XDG_DATA_HOME`, Hermes's own profiles) gives
   every harness multiple logins; pi first needs a login recipe at all.
   Also decided there: Next is **not** rebased on opencode-mobile, but its
   diff viewer, tool-call approval and demo mode are queued as app work, in
   that order. Its self-hosted F-Droid repo is **parked until monetization
   is decided** (David, 26 Sep 2026; `docs/proposals/2026-08-20-monetization.md`),
   since where the app ships decides how it can be charged for.

5. **Alerts and digests in Next** (David, 24 Sep 2026) —
   `docs/proposals/2026-09-24-alerts-and-digests.md`. The dozen red5
   watchers (disk, host, login, mcp, memory health, issue watches) and the
   digests (describe, agenda, landscape) report status through one
   `agent-alert` helper to a server-side store; the store does the edge
   detection, the `alerts` event rides `/sessions/events` to Next's
   notifier, cards get **Fix it** (a session via `POST /ask`) and **Ack**,
   and inbox.org stays the record. **Step 1 DONE 24 Sep 2026**: the store
   (§6.17, `POST/GET /alerts`, `/alerts/ack`), `agent-alert` with an offline
   fallback, and disk-watch + host-watch reporting through it (delivery is
   still the digest pane). Next: the `alerts` event and the Home section.
   **Step 3 begun 25 Sep 2026** (David: "rather click a button to play
   it"): a digest reported with `spoken` is rendered held, never read out;
   Home's Digests row plays it (`replay-id`). All three moved the same day:
   org agenda, describe-digest (its tmux pane retired) and the landscape
   watch (read-out = its "Worth stealing"; it still files its TODO).
   **Digests open to read, 25 Sep 2026** (David: "click on the digests to
   browse and read them"): every digest is kept 90 days (`digest_log`,
   `GET /alerts/digests`, `/alerts/digest?n=`); a Home row's title opens
   it, ← Earlier / Later → step through, "All digests" lists them. The
   landscape watch sends its whole file, drawn as a document. The agenda
   digest names its view (`--view agenda`), so its page is the Organiser's
   live rows for just those items (tickable; done since = DONE, gone =
   struck through).

## Loose ends

- **A reply with a figure had no follow-along at all** (David, 23 Sep 2026)
  — not a beat behind: no bold anywhere, the whole way down. **Fixed 23 Sep
  2026** (`32d6704`). A live line has no key (it has never been written to
  history), so `join_speech` matches it to its message by words — and the
  words it compared were the raw transcript text, markers and all, because
  `strip_markers` runs after the join so the key path can have them. A
  `[[visual:]]` marker is easily 400 characters, which is the whole window
  `_norm(…)[:400]` looks at: the reply scored 0.45 against its own live
  line where the bar is 0.6, joined nothing, and the thread had no live
  line to follow. The word match now compares `display_text`, which is what
  the voice was given. Note for whoever meets this again: it bites only
  while a reply is live — afterwards the history row carries a key and the
  key path matches, so the transcript reads perfectly and the fault looks
  like the follow-along, not the join.

- **The bold sat one sentence behind the voice** (David, 23 Sep 2026).
  **Fixed 23 Sep 2026** (`44f14f9`): `pos` and `elapsed` were on different
  clocks. The live frame's `elapsed` is wall time since `play_started_at`,
  so it carries the gaps between clips; `live_pos_s` — what `/speech/now`'s
  `pos` and the progress bar ride — was summed clip lengths, which carries
  none. Measured on a real reply: one 4.88s gap between clip 0 and clip 1
  (the first sentence is played alone while the rest render), then a
  constant 4.87s at all thirteen later boundaries, spread under 50 ms. The
  app closes the loop — `useElapsedSkew` reads `elapsed - pos` as staleness
  and holds the bold back by it — and most sentences in that reply ran
  3–5s, hence exactly one sentence. `submit.wall_position()` now rides the
  measured starts, and publishes `total_wall_s` so the bar's denominator is
  on the same clock (clamping a wall position against the summed audio
  pegged it at 100% a gap early). No app change: the skew estimate collapses
  to the real staleness it was written to measure.

- **Follow-along is lost for the rest of a reply** when another session's
  question barges in mid-reply. Three holes fixed in the follow loop
  (`23efc2d`), and the live row no longer has to survive for the bold to:
  the newest turn carries `sentences` + `offsets` with no `live` (§6.2,
  `f1739ec`), measured now that `clip_starts_s` outlives the row. See
  [docs/notes/2026-09-23-follow-along-after-barge-in.md](notes/2026-09-23-follow-along-after-barge-in.md).
  **Done 23 Sep 2026**: `/speech/now` names the turn it is on (`turn`,
  §6.5, `315e6e7`), the message carries `spoken.timeline` where `live` would
  be (`b721515`), and the app bolds from the player's own `pos` when there is
  no live clock (sasonica-chat `0f97d007`, `test/lostlive.mjs` — verified
  failing without the wiring).
- **A server-settings section in the app** — *maybe, 23 Sep 2026*. Nothing
  server-wide is settable from the phone: auto-naming (`MEDIA_AUTO_TITLE`)
  and which model names a thread (`MEDIA_TITLE_MODEL`) are env vars on red5.
  Not Advanced, which is per device and reveals detail rather than changing
  the server for everyone paired to it. Left as env vars deliberately: a
  bad name costs one tap of Rename, which wins for good. Build the section
  when two or three settings want it, not for one checkbox.
- **The question form should sit at the bottom while it is being filled**
  (David, 23 Sep 2026). **Done 23 Sep 2026**: the card is docked, not
  collapsed around. Measured first, at 390×780 against a new mock fixture
  whose ask sits under a reply taller than the screen (`Mock: asking after
  a long reply`): nothing above the card was collapsible — the reasoning is
  already shut and the steps are one folded "Worked · 4 steps" line. What
  pushed the form off the screen was the reply itself, and no amount of
  collapsing would hold it there while the reply grew under it.
  So the dialog the session is stopped on now renders **once**, in the
  viewport footer above the speech bar (`Thread`'s `dock`, sasonica-chat
  `…`): it is in view whatever the thread is scrolled to, until it is
  answered. `buildItems({dock: true})` keeps the stream from showing a
  second copy — a pending ask keeps its place in the conversation, marked
  "Waiting on your answer — the form is below". The dock is capped at half
  the screen and scrolls inside itself, so a two-question form never pushes
  the composer away.

- **The project picker should be ordered by recently active** (David, 23
  Sep 2026), and it needs scroll: Move to project… lists every project on
  the server alphabetically, so the one he wants is rarely near the top.
  `knownProjects()` (`hooks/useThreads.ts`) sorts with `localeCompare`;
  ordering by activity means live rows first, then the rest by `at`
  descending — note `SessionRow.at` is only set on shelved rows, so a live
  row has no activity time to sort by and the server would have to carry
  one. **Do it the way the Show menu does** (David, 23 Sep 2026): the
  threads screen's filter already lists every project, as a scrolling
  `Popover` with section heads (`routes/threads.tsx`), and that treatment is
  what a long list wants — not the sheet's `action-list`, which grows past
  the screen. Both read the same `projects`, so the activity ordering lands
  in `knownProjects()` and fixes the Show menu at the same time.
  **Done 23 Sep 2026**: the order is one function, `projectOptions()`
  (`lib/threadSort.ts`) — a project is as recent as its most recent thread,
  measured with the same `recency()` the list's Most recent uses (a running
  thread is now), with "Other" last and the name breaking ties. Both
  `projectsOf()` (the Show menu) and `knownProjects()` (the thread page's
  move) read it, so the Show menu was fixed by the same change. The picker
  keeps the sheet's frame — it is summoned from a long press, which has no
  anchor to drop a `Popover` from — and takes the menu's treatment inside
  it: `menu-head` sections (Running now / Recently used) over a list capped
  at half the screen that scrolls.

- **`/session/move` does not guard a session with no pane** (23 Sep 2026).
  The busy check is `if live and _busy(session, pane)` (`moves.py`), and
  `live` needs a tmux pane, so a headless or non-interactive session — one
  the app cannot see a pane for — skips the gate entirely and has its
  transcript moved mid-turn. That is the very thing the gate is for ("an
  interrupted turn would lose whatever it had not written yet"). Found by
  trying to move a live paneless session onto agent-media; the move was not
  run. **Fixed 23 Sep 2026**: `_busy()` asks `sessions.activity_of()`, the
  one place "is this session busy" is answered — it reads the pane when
  there is one and sessiond's own view of a headless session when there is
  not — and the gate no longer asks whether the session is `live`, since
  `live` means "has a pane". A session with neither a pane nor a headless
  state still moves (nothing is running there).

- **Moving a live session splits its transcript in two** (23 Sep 2026).
  `move_transcript()` (`moves.py`) `shutil.move`s the `.jsonl` into the
  destination project's directory, but a running Claude Code holds the old
  path: the history lands under the new project and the next turn re-creates
  a fresh file at the path derived from the session's cwd. The conversation
  ends up as two files with the same session id and no overlapping uuids,
  the tail's first row correctly parented to the head's last — one thread
  cut in half. **Both moves made that afternoon did it**: the session that
  wrote the Matrix proposal (190 rows under
  `-home-ryer-projects-agent-media`, the rest under `-home-ryer-scratch`)
  and the headless one behind the collapse-reasoning ask (337 + 173),
  moved eight minutes apart, both recorded in `moved.json`.
  The busy gate does not catch it and should not have to: nothing was
  *running*, the sessions were merely open — which also means the fix in
  `83721c3` does not cover this.
  Why it shows: `harnesses.transcript()` globs every project directory and
  takes `max(hits, key=mtime)`, so a reader gets whichever half was written
  to last — the app showed the newer half and none of the thread's own
  history. And `move_transcript` picks its *source* with
  `sorted(root.glob(f"*/{session}.jsonl"))[0]`, so once a session is split a
  later move grabs whichever directory sorts first.
  Both were stitched by hand (head rows ahead of the tail, deduped by uuid,
  `os.replace` onto the file the session is still appending to — Claude Code
  reopens per write, so nothing was lost; backups in `~/scratch/`), and the
  orphaned halves retired. Options for a real fix: leave the file alone for
  a session that is open and let the override alone move the thread (the
  docstring already allows "nothing to move" as a non-error), or move it and
  stitch on next read.

- **Not every duplicate transcript is a split** (23 Sep 2026), worth knowing
  before the next diagnosis. A sweep of `~/.claude/projects` found six
  session ids in two directories each; only the two above were splits. The
  four others — three runlet ↔ sasonica-shell pairs and an August
  agent-media ↔ scratch pair — are **copy-forwards**: the newer file
  contains *every* uuid of the older (identical in three cases, +939 rows
  in one), the leftovers of the runlet → sasonica-shell rename, not damage.
  Uuid overlap is what tells the two apart: a split shares none, a copy
  shares all. The stale copies were retired (backups in
  `~/scratch/transcript-stale-copies/`), because they are what makes
  `move_transcript`'s `sorted(...)[0]` and `transcript()`'s newest-mtime
  pick the wrong file.

- `follow.mjs` at the largest text size fails on and off.
- `test_session_events.py::test_a_state_change_sends_the_list_again` times
  out on and off when the machine is busy (the SSE watcher's poll is 50 ms
  in that test); it passes on its own.
- The About page shows the server host but no server version (no route).
- The Windows install test leaves stray PATH entries.
- A stale saved question for a session may not clear after it's answered
  (the PostToolUse clear may not fire).
- Once the old app retires, move Next into its own repository so the
  licence split is clean.
- Next's speech service cannot start itself after a reboot: Android 15 will
  not start a `mediaPlayback` service from `BOOT_COMPLETED`. Opening the app
  starts it. Worth solving before Next's player becomes the default.
- Next's speech takes audio focus (it is the app's only player), so while
  both apps are installed a reply pauses the old app's book through Android
  rather than in process. That is the intended behaviour, but it is the
  arbitration the old app does more carefully, and it has not been heard yet.
