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
the About page; seven text sizes (7–19 px, Default 13); the digital-
assistant slot in Next; the coding-agents installer in Settings; Sasonica
Shell rename, named URLs and client labels; answered questions stop being
read out; tap to read from here (tap a sentence while it is spoken;
select a word in an older reply → "Read from here"; not yet tried on the
phone with real audio); background notifications in Next (a `specialUse`
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

## Queued, in order

1. **Sasonica Shell OAuth** — the proposal is written, not built.
2. **One setup for a machine, from the Coding agents page** (David, 23 Sep
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
