# Sasonica roadmap

What is built, in flight and queued for Sasonica: the chat app and its
Android shell (Sasonica, once Sasonica Next[^next-rename]), the server here in agent-media, and
Sasonica Shell. One list, so any session (or David) can see where things
stand. Update it when an item lands or a decision is made.

Where things live:

- **Server:** agent-media `packages/server`, contract in
  [server-contract.md](server-contract.md). Python, and staying Python.
- **Sasonica, the app** (chat app + its Android shell, one tree since 26 Sep
  2026): repo **`southpenlabs/sasonica-app`** (private), branch `main`,
  checkout `~/projects/sasonica-app` (the old `chat/` is the root). React
  Router + assistant-ui; applicationId **`com.sasonica.app`** (Java package
  too since 27 Sep 2026; the launcher entry keeps `com.sasonica.next.MainActivity`
  as an alias). CI builds `sasonica-apk` (release key) and
  `sasonica-debug-apk` (`com.sasonica.app.debug`, beside it); install with
  `agent-phone-adb sasonica`. Browser preview on red5 `:8795`
  (`sasonica-chat-preview`, from that checkout's `build/`). Its history came
  across from the fork's `chat-prototype`/`android-next`, which are frozen —
  commit hashes below that name them are from there.
- **Sasonica ABS:** the Audiobookshelf app fork, kept for books —
  `com.sasonica.abs`, branch `sasonica`, `agent-phone-adb sasonica-abs`. Book
  control on :8772 (loopback) and :8773 (tokened).
- **Sasonica Shell:** `~/projects/sasonica-shell` (formerly Runlet).
- **Licences:** agent-media, the chat app + its Android shell, and Sasonica Shell are
  Apache-2.0, copyright South Pen Labs. The Audiobookshelf forks (the old
  Sasonica app, sasonica-web) stay GPL v3; never copy their code into
  `chat/`.

## Standing decisions

- App work lands on `main` of sasonica-app — one branch for the web and the
  Android shell; the browser preview is a fallback.
- **A change the user will notice gets a What's new line** (27 Sep 2026):
  add it to the top entry of sasonica-app `app/lib/whatsNew.ts` (a new entry,
  id = the day, when the top one has shipped), in plain words — what they can
  now do, not how. Internals and fixes nobody saw stay out.
- **Sasonica ABS stays for books; Sasonica took its id** (David, 26 Sep 2026).
  Everything but the book is Sasonica's: speech (:6614), the holds, and the
  readouts `/mic` `/ringer` `/state` on loopback **:8774** (call_guard,
  ringer.py and `media doctor` read them there; the old app's :8772 is book
  control only). Keep publishing conversations to Audiobookshelf while the
  books app plays them.
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

**Sasonica accounts** (David, 29 Sep 2026: the split — Drupal on
cms.sasonica.com owns accounts and is the OIDC issuer; a Sasonica Matrix
homeserver comes later). Accounts enrol devices, they do not authenticate
requests (`docs/proposals/2026-09-23-accounts-and-the-identity-seam.md`).
Built the same day:
- **Sign-in:** "Sign in with a Sasonica account" on the pairing screen
  (PKCE, `sasonica://auth`); `POST /enrol` swaps the code for the ID token
  server-side and mints a device token; RS256 checked in stdlib (`oidc.py`).
  The device is named after the phone. red5 allows David's account by `sub`.
- **Staying current:** the refresh token is kept on the device row (never
  listed); `/me` over an hour old re-reads userinfo (email, username,
  picture).
- **In the app:** the profile button top right on every tab (it replaced
  ⚙): picture and name, a menu (Edit profile, Settings, About, Sign out —
  `POST /me/signout`). Edit profile is a sheet: photo, username, email (a
  confirmation link to the new address, an undo link to the old), password
  (the current one, or an emailed link), Delete account (an emailed link that
  opens a page with a button). `GET/POST /me/account` passes these to
  cms.sasonica.com's `/api/account` with the account's own access token.
- **Social sign-in:** Google live (project `sasonica`), **published 8 Oct
  2026** (out of Testing; branding: home page sasonica.com, privacy policy
  sasonica.com/privacy-policy, no logo so no verification, no terms link yet); GitHub and Facebook modules
  installed, each needs its OAuth app (`bin/social-secret`). A social
  sign-in fills an empty profile picture.
- **Email:** sasonica.com sends through Cloudflare Email Sending (South Pen
  Labs account, Workers Paid; only sasonica.com onboarded — southpenlabs.com
  would get `p=reject` DMARC over its forwarding), HTML in a Sasonica frame
  with a preview line; SPF, DKIM and DMARC pass at Gmail.
Commits: agent-media `206173c`…`16ffd70`; websites `4cb8f03`…; sasonica-app
`3b5fd9f`…`3ff0c8a`.

**The server as one file** (David, 29 Sep 2026: "let's do that"):
`deploy/binary/build.sh` makes `sasonica-linux-<arch>`, a PyApp launcher with
Python and core/server/visual inside (57 MB, 65 ms to start after its first
run). `sasonica serve` / `sessiond` / `install` / `<any console script>`;
CI publishes x86_64 and aarch64 to the rolling `server-latest` release, and
`sasonica.com/install` on Linux now installs it and shows a pairing QR.
Headless first: no multiplexer needed. agent-media `43281af`,
`docs/proposals/2026-09-29-single-binary.md`. **The phone installer too**
(same day): the binary in its Debian, no git, pip, Python or venv, Node only
for Claude Code; `SASONICA_FROM=source` for the old way. **And opencode
headless** (same day): one shared `opencode serve` held by sessiond, the same
approval cards, interrupt, model chip and park/resume — no tmux for any
agent the phone starts. **The Mac too** (same day): `sasonica-macos-aarch64`,
launchd agents from `sasonica install`, procinfo.py for other processes;
checked in CI (docs/proposals/2026-09-29-single-binary.md). **And Windows**
(same day): `sasonica-windows-x86_64.exe`, `deploy/install.ps1`, sessiond
over loopback TCP with a token, logon tasks; checked in CI. **Windows on
ARM64** (David, 16 Oct 2026): the same x64 .exe and x64 cloudflared, run
under Windows 11's x64 emulation, no ARM64 build; install.ps1 accepts ARM64
(refuses 32-bit) and CI smoke-tests the x64 .exe on `windows-11-arm`
(branch `win-arm64`). **Next:** try both on real machines; signing; a
sasonica.com address for install.ps1.
**Sasonica Shell, opt-in, in the same installer** (David, 16 Oct 2026):
after pairing, one question, default No ("Also let an AI assistant such as
Claude run commands on this computer? It needs a free Sasonica account.");
`SASONICA_WITH_SHELL=1/0` and `-Shell`/`-NoShell` for unattended installs,
`SASONICA_ONLY=shell` / `-ShellOnly` (and `sasonica.com/shell`,
deploy/shell.sh) for Shell alone. Beside the server there is one command:
`sasonica shell …`, and `sasonica status`/`skills`/`url`/… forwarded to
Shell (David, 16 Oct 2026; branch one-command in both repos).
docs/proposals/2026-09-29-single-binary.md, "Install".

**A welcome, and Just look around** (David, 27 Sep 2026): a fresh install
opens on Welcome, which says what Sasonica is and offers **Connect to my
server** (Pair this device) or **Just look around**: a demo served inside the
page (`app/demo/server.ts`, four invented threads, a reply answered after
working steps, nothing sent anywhere) under a banner whose Connect my server
leads to pairing from any screen. Pairing ends it and forgets its snapshots.
This is the demo mode queued under item 4. Still open: scanning a pairing QR,
and reaching the server off the tailnet (contract §19). sasonica-app `afff1c2`.
Next proposed: **the server on the phone** — Run it on this device, Termux and
a one-line installer that hands the app its pairing link, and an optional ADB
power-up (`docs/proposals/2026-09-27-server-on-the-phone.md`).

**What's new** (David, 27 Sep 2026): once after an update, a sheet lists
what changed since the last one this install saw — hand-written entries in
`app/lib/whatsNew.ts`, newest first. Got it, a tap outside or back closes it
for good; an install updating from before the sheet sees the newest entry
only, a new install none. About keeps the whole list. sasonica-app `f774f38`.

**Take back your last message** (David, 27 Sep 2026): tap your latest
message for **Cancel it** or **Edit it**. Either stops the turn and anything
queued behind it, and strikes the message through ("taken back"); Edit then
puts its words in the reply box. A correction, not a rewind — the next
message tells the agent to disregard the one taken back
(`POST /session/retract`, server-contract §6.19). Latest message only. The
app's Stop button now calls `/session/stop` too (it was still a stub).

**The reply box's bottom row** (David, 26 Sep 2026): the words take the
box's whole width, and one row under them holds attach, the thread's model
chip (Opus / Sonnet / Haiku / Fable), a Plan chip, `/` (the slash menu from
`/commands`, as a sheet with a filter), then stop, mic, New chat and send.
The new chat's "where · agent" chip joined the row; its model and plan go
with `/ask`. Claude only; `GET`/`POST /session/settings` (server-contract
§6.4). Panes change between turns only (typed `/model`, shift+tab).

Pairing and device tokens; threads by session; transcript messages and
per-thread SSE; speech bar with full controls; follow-along; recaps; exit
and archive; idle closer; audio destination picker and Android output
switcher; multi-line sends; per-session stop; multi-select questions; Home
dashboard; background agents in a thread; thread-list sorting and filter
(Active / Live / Closed / Archived / Everything, one project, and any
number of states — Needs you / Working / Your turn; `/targets`
now lists every archived thread, past the 40-row cap; By project's
headings fold, kept per device; filtered to one project, the + starts the
new chat there, 23 Sep 2026; the project is a strip of chips across the
top, like the Organiser's views, each counting what needs you, 28 Sep
2026, sasonica-app d2de765); project
line under titles; menus that close on an outside tap; the brand and icons;
the About page; seven text sizes (9–21 px, Default 15), and a pinch
steps through them and saves the choice (24 Sep 2026); the digital-
assistant slot in Next[^next-rename]; the coding-agents installer in Settings; Sasonica
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
`/org/view?done=1`), waiting and someday ones, and plain notes with their
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
android-next `6861b775`, installed on p8a. Since 16 Oct 2026 the chip sits
in the voice's bar instead, on a second row with the speed (− 1× +), shown
only in the thread being read (sasonica-app `66cc2ea`).

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

A home-screen widget (David, 3 Oct 2026): **Ask** opens a new chat already
listening (`/new?listen=1`), and one line shows Needs you · title, Working ·
title, All quiet, or Not connected. It opens the one waiting thread, or Home.
`AskWidget.java` has no stream of its own: NotifyService hands it each
`sessions` frame and marks it offline when the stream drops. sasonica-app
`301ddef`, four cells wide since `e708dc9` (p8a's 1.5 font scale cut the line).
Then four sizes, each its own picker entry (David, 3 Oct 2026, `43523eb`):
a 1×1 Ask button tinted by state, the 4×1, a 4×2 Sessions list (three rows,
needs you first), and a 4×3 Last reply. That one fetches the newest six
messages from `/conversation/log` when a thread goes working → waiting.
Not yet seen on p8a's home screen.

Home's **Unread replies** (David, 3 Oct 2026): right under Needs you, every
thread that ends on a reply newer than this device last had it open, with
its last words — replies that landed while the app was shut included.
`/dashboard` gained `replies` (§6.11: `transcript.last_reply` reads each
recent transcript's end, again only when it changes; ~0.1 s cold on red5's
192 threads). Read state stays on the device (`lib/arrivals.ts` "seen"); a
thread never opened there counts as read up to its first look. Opening one,
or Mark all read, clears it.

## In flight

**The app in a browser: its own mic and speaker** (David, 16 Oct 2026: "why
aren't there any mic and speaker control settings in the player on the
browser's version?" — then: both, dictation and replies spoken in the
browser). The shell's Mic/Speaker switches are Android's routing
(AudioRoutePlugin), and a browser had nothing to route: it neither played
nor heard anything. Merged (sasonica-app `53fee18`) and deployed 16 Oct 2026:
`lib/webAudio.ts` (the browser's SpeechRecognition for the mic key and
hands-free, the chosen mic's track handed to `start(track)` where the browser
takes one; `setSinkId` for the speaker; both choices per browser) and
`lib/webSpeaker.ts` (Settings → "Speak replies in this browser", and Speak
here in the speech sheet: holds `/sessions/events?speech=frames`, plays each
`tts:` entry fetched from the new `GET /speech/clip` (contract §6.5), reports
to `/speech/state`). The sheet shows Speak here / Mic / Speaker in a browser
in place of the phone/earbuds pills. Test: `test/webaudio.mjs`. Open: Firefox
has no recogniser (no mic key there); the newest frames stream wins, so a
browser and the phone both on take it in turns as they reconnect; a tab
nobody has touched since it loaded starts a reply paused (autoplay).

**New projects, worktrees and presets** (David, 15 Oct 2026). Server done
(`1142230`, contract §6.24): `/ask` takes `new_project` (+ `repo`,
`agents_md`, `preset`) or `worktree: {from, branch}`; `/presets` CRUD. App:
the new-chat sheet's New project… / New worktree… rows, preset chips and a
Settings → Presets editor, building. `agents_md: "draft"` (server done):
the first chat writes AGENTS.md from the repo, the same words for every
agent — the app's third AGENTS.md choice, "Draft from the repo", for a
clone. Open: in the default layout a
worktree's thread is filed under its branch name (the folder's basename),
not its project.

**Roles: admin, member, guest** (David, 18 Oct 2026) — proposal:
`docs/proposals/2026-10-18-roles.md` (a role on the device row; members'
chats permission-checked, approvals to an admin). Step 0, project roots
(the new-project folder shown and chosen, Settings → Project folders),
built the same day.

**Sasonica's own speech player.** Media3 (David, 23 Sep 2026: the reason
MediaPlayer was chosen — the companion had no Gradle build — is gone). Built:
`com.sasonica.next.speech` — `MpvServer`, `Json` and `ClipCache` carried over
from the old app unchanged (companion-origin, Apache-2.0), a new
`Media3Speech` on ExoPlayer behind the same `MpvServer.Player` interface, and
`SpeechService`, a `mediaPlayback` foreground service that binds the phone's
tailnet address on **6614** (the old app keeps 6613, so both run). A Settings
toggle, off until turned on; `MEDIA_SPEECH_SOCKET_SASONICA=tcp://p8a:6614` and
`media speech-target sasonica` point red5 at it. The protocol test came across to
JUnit and runs in CI. Next: build it on CI, sideload beside the old app, and
listen — the speed at 1.6x and the gap between sentences are the two things
Media3 has to prove. **First reply through it played on p8a, 23 Sep 2026**:
two clips, the join fired on its own and volunteered `playlist-pos`, and
`idle-active` went true at the end.

**The hold tier in Sasonica** (23 Sep 2026). `Holds.java` — dictation pauses a
reply and it carries on; a voice session or a call holds every reply until it
is over, with the Speak now / Later card; urgent takes the room. `MicWatch`,
`MicSteady`, `MicSource`, `BargeIn`, `DictationHold`, `HoldRate` and
`RingerState` came across unchanged, with their tests as JUnit. No `BookHold`
(Sasonica has no book). Served on loopback :8774 since 26 Sep 2026
(`Readouts.java`), and agent-media reads them there.

The Organiser on plain Org, paragtd as a package (24 Sep 2026,
`docs/proposals/2026-09-24-notes-core-and-paragtd.md`): the layout is a notes
profile. Plain Org by default (your agenda files and `#+TODO` keywords, copied
once from Emacs by setup's `agenda` row), and paragtd's GTD files from
`packages/org-paragtd`, found by the tree. paragtd writes `.paragtd.json`
(paragtd `c2af9ea`), and the profile reads its files and keywords from it.
Closing a sequenced step from the phone follows Org's dependency blocking and
runs paragtd's next-step trigger, and the Organiser says which step is next.
The app takes its keywords and refile targets from `GET /org`. On the red5
server and preview; in the app at the next CI build. Then **More…** beside the
capture box: your capture templates (the manifest's, site ones too) filled
and filed as org-capture would, prompts drawn above the box; templates that
call Emacs functions (morning/midday/evening/visioning) stay in Emacs. And
`paragtd-astro.timer` (monthly, turned on from setup) keeps astro.org a year
ahead.

## Queued, in order

1. **Devices screen in the app** — the server half landed 23 Sep 2026
   (`GET /devices`, `POST /devices/code`, `POST /devices/revoke`,
   contract §9 "Enrolling from the app"). **The screen: DONE 27 Sep 2026**
   (sasonica-app `1510b5e`): Settings → Devices, shown only when `POST /pair`
   said `enrol: true` (an older pairing asks `GET /devices` once), a row per
   device with when it was last seen, "Pair a device" (a name, then the code
   as a QR of its `sasonica://pair` link; the new device appears as it
   pairs), and revoke behind a confirm. `devices --enrol ID` (agent-media
   `aed4572`) gives a device already paired the bit at the desk. Not yet on
   p8a: none of its devices has the bit, and it needs the next CI build.
   Steps 2–4 of
   `docs/proposals/2026-09-23-accounts-and-the-identity-seam.md` (OIDC, an
   issuer) stay parked until there is a second person.
2. **Sasonica Shell OAuth** — **DONE 27 Sep 2026.** red5's shell moved to
   the South Pen Labs Cloudflare account (`sasonica-shell-red5.southpenlabs.workers.dev`)
   with OAuth on `/mcp`, and sign-in is **approved in the Sasonica app**
   (`SASONICA_SIGNIN=app`, David: users must not need a Cloudflare login): the
   page shows a code, the runner raises one notification that says where to
   go, a banner over every screen (and Home's card) has Approve/Deny, and the
   decision is signed with red5's relay key (sasonica-shell `fe1e77a` …
   `0358f74`, agent-media §6.17a, sasonica-app `74481e6`, `b21c886`). Claude
   connected and ran commands as `oauth-claude` (15:54). Cloudflare Access
   stays as the other mode. Left: revoke the shared secret URL and delete the
   old Worker in the davidj4tech account; tiers decided (self-hosted free
   first, a hosted relay later — sasonica-shell `docs/hosted-relay.md`).
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
   2. **The page** — **DONE 27 Sep 2026** (agent-media `37f7ca7`,
      sasonica-app `2551b29`): Coding agents → This machine, `GET /setup`
      and `POST /setup/run` (`media-setup profile --only ROW`). Sasonica
      Shell's installer is not a row yet. As first planned: the app already installs and signs in harnesses (§6.6),
      so the same page shows *this machine's wiring* — hooks, services,
      skills, mailbox, catch-up — each with what is missing and a button
      that runs the installer that owns it. The server calls the existing
      installers, never reimplements them (`media-setup status --json` is
      already that list).
4. **opencode, and more than one account per harness** (David, 24 Sep
   2026) — `docs/proposals/2026-09-24-opencode-and-harness-profiles.md`.
   **27 Sep 2026: pi's sign-in and harness profiles built** (agent-media
   `97437a1`, `786e971`; sasonica-app `38aef50`): Add login on Coding agents,
   a row and a sign-in per login, New chat's Login choice, every session
   reader over each login's folder. Not yet: headless (sessiond) chats in a
   login (they open as panes), and the modules that still read `~/.claude`
   directly for Claude's own extras (recaps, search, slash menu).
   As first written:
   opencode becomes a fifth row in `RECIPES`; a *harness profile* (a
   harness plus its config dir: `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
   `PI_CODING_AGENT_DIR`, `XDG_DATA_HOME`, Hermes's own profiles) gives
   every harness multiple logins; pi first needs a login recipe at all.
   Also decided there: Sasonica is **not** rebased on opencode-mobile, but its
   diff viewer, tool-call approval and demo mode (done 27 Sep 2026) are queued as app work, in
   that order. Its self-hosted F-Droid repo is **parked until monetization
   is decided** (David, 26 Sep 2026; `docs/proposals/2026-08-20-monetization.md`),
   since where the app ships decides how it can be charged for.

5. **Alerts and digests in Sasonica** (David, 24 Sep 2026) —
   `docs/proposals/2026-09-24-alerts-and-digests.md`. The dozen red5
   watchers (disk, host, login, mcp, memory health, issue watches) and the
   digests (describe, agenda, landscape) report status through one
   `agent-alert` helper to a server-side store; the store does the edge
   detection, the `alerts` event rides `/sessions/events` to Sasonica's
   notifier, cards get **Fix it** (a session via `POST /ask`) and **Ack**,
   and inbox.org stays the record. **Step 1 DONE 24 Sep 2026**: the store
   (§6.17, `POST/GET /alerts`, `/alerts/ack`), `agent-alert` with an offline
   fallback, and disk-watch + host-watch reporting through it (delivery is
   still the digest pane). **Alerts as notifications 27 Sep 2026** (David:
   the disk warning should come through when the phone is on silent): the
   `alerts` frame on `/sessions/events?alerts=<cursor>` (§6.13), posted by
   the notifier on an "Alerts" channel, one per alert. **Home's Alerts
   section and the per-level toggle DONE 27 Sep 2026** (agent-media
   `1863a8c`, sasonica-app `c6c6882`): open alerts worst first with Fix it
   (a new chat with the alert as its words) and Ack; Settings →
   Notifications, Needs you and Warnings, held by the Android notifier.
   **Step 3 begun 25 Sep 2026** (David: "rather click a button to play
   it"): a digest reported with `spoken` is rendered held, never read out;
   Home's Digests row plays it (`replay-id`). All three moved the same day:
   org agenda, describe-digest (its tmux pane retired) and the landscape
   watch (read-out = its "Worth stealing"; it still files its TODO).
   **Digests open to read, 25 Sep 2026** (David: "click on the digests to
   browse and read them"): every digest is kept a year, the landscape
   watch's for good (27 Sep 2026; was 90 days) (`digest_log`,
   `GET /alerts/digests`, `/alerts/digest?n=`); a Home row's title opens
   it, ← Earlier / Later → step through, "All digests" lists them. The
   landscape watch sends its whole file, drawn as a document. The agenda
   digest names its view (`--view agenda`), so its page is the Organiser's
   live rows for just those items (tickable; done since = DONE, gone =
   struck through).

6. **The phone's own voice as the default** (David, 26 Sep 2026: "that
   sounds awesome"). Live speech on the phone renders on the device with
   Android's `TextToSpeech` instead of fetching server clips: first audio
   without the bridge's round trips (the latency is the bridge, not the
   render), speech offline, and a fresh install talks with no TTS key or
   server engine — which suits the free app + hosted tier. Follow-along gets
   the engine's own word positions (`UtteranceProgressListener.onRangeStart`)
   instead of `clip_starts_s` arithmetic. What stays on the server: the rooms
   and red5's canvas (they cannot hear the phone's voice), the Conversations
   library and replay — **decided (David, 26 Sep 2026): clips on demand** —
   a phone-voiced turn has none until something needs one (a replay, the
   rooms), and the server renders it then; nothing is uploaded.
   The server voice stays as a setting ("Voice: this phone / server").
   Shape: today Sasonica's :6614 speaks the mpv vocabulary and only ever gets
   `loadfile <uri>`, so the server needs a text lane to it — a verb carrying
   the sentences (and the turn/clip ids the follow-along already keys on),
   with the holds, barge-in, priority and speed (`setSpeechRate`) applying
   unchanged. To find out first: whether Google's voices hold up at 1.6x,
   and how a mixed history (some turns rendered, some not) reads in the
   library.
   **Spike, 26 Sep 2026** (sasonica-app branch `phone-voice-spike`,
   `PhoneVoice.java`; loopback `/say?text=&rate=&voice=`, `/say/log`,
   `/voices`, `/say/stop` on :8774; installed on p8a from run 36246281165 —
   the next main install drops it): Google's engine, 625 voices, offline
   en-AU ones (`en-au-x-aua…aud-local`). Warm, **~180 ms from request to
   sound** at 1.6x; the first request after a voice change ~2.1 s (engine
   init 2.2 s at service start). `onRangeStart` fires for every word, and
   sentence N+1 starts within 5 ms of N's done. Not wired to replies or
   the holds. **David: "that sounded great"** (en-au-x-aua-local, 1.6x) —
   go ahead with the text lane.
   **Built and ON, 27 Sep 2026** (agent-media `3fca516`, sasonica-app
   `f0686c3`): `MEDIA_SPEECH_RENDER_SASONICA=device` +
   `MEDIA_SPEECH_DEVICE_VOICE_SASONICA` in red5's env. Each sentence is filed as a
   `.tts` clip (its words) and the player gets `tts:<clip>?text=…&voice=…`;
   `ClipCache` renders it with `synthesizeToFile` at 1.0 (player speed does
   the rest). Durations estimated at 15 chars/s until measured. Replay on a
   player that can't render words (rooms) renders the audio on red5 then and
   keeps it; the ABS export and the feed skip device turns; history, line ids
   and replay to the phone are unchanged. Measured: 330 ms per sentence warm,
   ~2.2 s cold (voice load after the app restarts). **Left:** ~4 s from
   `media say` to the phone's request is the pipeline/bridge, now the
   biggest part of the wait; drop Conversations from Sasonica ABS (agreed, ask
   before deleting); a Voice setting in the app (phone / server); word-level
   follow-along (`onRangeStart` is lost once the sentence is a file);
   replay to the phone not yet heard.
   **Voice, 27 Sep 2026:** David found the offline voice flat; Kokoro was
   measured and ruled out (p8a CPU RTF 2.1–4.6, red5 0.85 — too slow at
   1.6x); Google's online voice A (`en-au-x-aua-network`) chosen.
   **Start latency, 27 Sep 2026** (token → start at the phone, was ~4 s):
   batches ride the reply's open connection (`8fdb530`), music is paused
   alongside instead of first (`ba126e4`, David's call), the claim is one
   message (`56bc86c`, app `am-claim`), claim + load + start travel together
   (`cab0356`, app `am-claim-play`), and `media-ipc-relay`
   (`agent-media-speech-relay.service`, loopback 16614 → p8a:6614, spares
   kept open, `51fd5c1`) takes the connect off the path. Measured: the
   claim-and-play message leaves 3–11 ms after the token, reaches the phone
   ~0.22 s later; then the first sentence's render (~0.2 s offline, ~0.5 s
   online voice).
   **Rest of the list, 27 Sep 2026:** Conversations library removed (David:
   "remove it all") — `MEDIA_CONVERSATIONS_LIBRARY=0` (`6e09e5b`), the ABS
   library deleted, `~/conversations` emptied (9,702 clips had no other copy),
   the container mount dropped (`af2a0ae`, takes effect at its next restart);
   chat transcripts keep every message, old swept turns lose their spoken
   lines. Settings → Voice: this phone / server + the voice (agent-media
   `0dd44aa`, app `18d3794`). Word follow-along: the player answers the
   word under its position (`Speech.word()`), lit with
   `::highlight(speech-word)` (app `c175acb`); Google's voices report a range
   per word but not usable positions (`[359, 0, 4]`), so words are spread by
   characters over each clip's real length. Replay to the phone heard OK.
   **Word highlight withdrawn** the same day (David: "a little bit laggy and
   distracting"; app revert of `c175acb`) — the sentence bold is the
   follow-along. Audiobookshelf's container restarted without /conversations.
   **Natasha from the phone, 27 Sep 2026** (agent-media `8444d96`, app
   `2f02fc0`): Voice → "Natasha · Microsoft" (`edge:en-AU-NatashaNeural`);
   the app speaks Edge's Read Aloud WebSocket itself (`EdgeVoice`, OkHttp,
   the edge-tts protocol — unofficial, so every clip carries
   `&fallback=<Google voice>`). Measured ~1.2 s per sentence cold; a
   sentence with no audio in 3 s or not done in 10 s is rendered by Google
   instead (tested). Chosen on red5 as the phone voice.
   **Language, site-wide (David, 27 Sep 2026):** one Language setting in
   Sasonica, saved on the server (`language.current()`), that (a) filters
   Settings → Voice to its accents and voices (phone/server radio for every
   voice, Microsoft's full list cached daily on red5 — in progress), (b)
   asks every agent to reply in it (the heard hook's language line,
   `e4ac427`, silent for English), and (c) translates the app's own text —
   **done 27 Sep 2026** (sasonica-app `aa1ae4f`…`47989a5`): `app/i18n/`
   (en source by area, ~800 keys; es, fr, zh, de, it, sv, ja typed against
   it; `t()`/`useT()`, Intl dates, `<html lang>`), native strings in
   `res/values-*` applied by the app's language (`Lang.java`), every screen
   checked at 390 px / 1.5 text scale. The language is now **with its
   country** (`49ddeda`, locale en-AU): Voice shows only that country's
   voices, Google's not offered (`e001941`), tap a voice to hear it
   (`b87405d`). Left English: server/agent text, What's new entries, demo
   content. Installed on p8a (run 36274279961).

7. **Sibling bases, and Home Assistant as the map of the house** (David,
   27 Sep 2026) — the "Sibling bases" section of
   `docs/proposals/2026-09-23-accounts-and-the-identity-seam.md`.
   **First step, needs no accounts** (David, 27 Sep 2026): the app pairs
   with more than one base and does the joining itself. It keeps a list of
   pairings instead of one (today changing server wipes the other's
   threads, `forgetSnapshots` in `app/lib/snapshots.ts`), asks each base
   for `/targets`, merges them with each row tagged by base, and sends a
   thread's opens and replies to the base that owns it. The server only
   adds a name for itself to `/pair` or `/targets` ("red5", "Pixel"). No
   base trusts another; the phone already holds a token for each.
   The rest waits for base-to-base links, in order: (a) a base lists its
   siblings' sessions (for clients that paired with only one); (b) each
   base says what it can do
   (phone: mic, ringer, alerts; tablet: reading, canvas; TV: big screen,
   room speakers; server: harnesses, uptime) and jobs route by that;
   (c) Home Assistant answers *where*: which room David is in, which TV or
   speaker is there, so a base speaks and shows in the right place without
   our own device list; (d) the bases appear in HA as sensors (sessions,
   open alerts, speaking now) for automations. (c) and (d) extend
   `packages/voice-bridge/homeassistant` rather than adding another
   integration.
   **Where it leads: work that follows you** (David, 1 Oct 2026, picked from
   "what can it do that other chat apps can't"): with (b) and (c) in place,
   a conversation moves with David — the phone in the car, the TV and the
   room's speakers at home, the e-ink canvas at the desk — the voice and the
   figures going where he is, the thread the same everywhere. Not a step of
   its own; the test that (b)–(c) are done.
   **Plan for the first step** (20 Oct 2026, David: "y" to starting it).
   *Where the app holds one server today:* one pairing in
   `sasonica.chat.device` (sealed in the keystore in the shell, read by
   `NotifyService.java` too) and the address in `sasonica.chat.baseUrl`.
   Every call goes through `api/index.ts` `reach()`/`request()` with
   `serverBase()` + `authHeaders()`: the lists, the thread stream
   (`openThreadStream`), replies, the Organiser (`api/org.ts`),
   `pictureUrl`. Also on the one pairing: `relocate()` (its lookup),
   Settings, Devices (revoking itself), the profile menu (sign-in, sign
   out), About, `useSpeech`, `lib/bridges.ts`, the background notifier,
   and `finishAccountSignIn` (replacing a pairing on the same server).
   Pairing another server replaced the device; `forgetSnapshots` runs only
   on leaving the demo, so the old server's cached list painted until the
   new one answered. *The server already named itself:* `POST /pair`,
   `POST /enrol` and `POST /devices/code` answered `server: {name, base}`
   (`gethostname()`). Added (20 Oct 2026, with the several-servers
   proposal, item 36): `server_id` on `/pair`, `/enrol` and `/me` (the id
   sasonica.com's list already used), and `MEDIA_SERVER_NAME` for the name,
   so a base on the phone is "Pixel". Later, `server` on `GET /targets`
   so rows from a pairing made before the id was kept get one.
   *Storage:* the current pairing stays exactly where it is
   (`sasonica.chat.device`); the others go in `sasonica.chat.otherPairings`,
   a JSON array of the same Device shape, sealed the same way. So the
   existing pairing needs no migration (it is the current one), the native
   notifier is unchanged, and an older build ignores the new key and keeps
   working. A pairing is known by its `server_id` (kept as `server.id`)
   when both sides have one, else by its normalised address: the same
   server paired again replaces its own pairing, another is added.
   *Against docs/proposals/2026-10-20-several-servers.md* (item 36, written
   the same day): the same idea, a list with a token each and `server_id`
   as the key, which this uses. It differs in four places, to be merged:
   (i) storage — the proposal has `sasonica.chat.servers` (one key per
   entry's token) and `sasonica.chat.active`; this keeps the current one in
   `sasonica.chat.device` and the rest in one sealed list, so nothing
   migrates and the notifier, older builds and the e2e suites that seed
   `sasonica.chat.device` are untouched. The proposal's shape can come when
   the notifier takes a list (slice 4). (ii) The proposal's switcher in the
   header is superseded by David's "Merged, with a filter" (20 Oct 2026):
   one list, rows labelled, a chip per server; slice 1's Switch in Settings
   is only the way to reach another server until the merged list lands,
   and then names the home base (below). (iii) Unpair here forgets the
   pairing on the device only, as before; the proposal also revokes the row
   on that server while the token works — worth adding. (iv) The proposal
   puts roles step 1 and Settings in three sections before the list; this
   starts with the list, which needs no roles. Snapshots and drafts keyed
   by server (the proposal) are slice 2 here.
   *Which base a request goes to:* in slice 1, the current one, as now. From
   slice 3, a per-thread call takes the base of the row that owns the
   session (a `session → base` map filled by the merged list; a link to a
   session not in it asks each base); anything not per-thread (Settings,
   voice, Organiser, People, Media, alerts) stays with the current base,
   the "home" one. `relocate()` becomes per pairing.
   *Merged vs per base:* merged — `/targets` sessions (each row tagged with
   its base and label), `/sessions/state`, later Home and alerts. Per base —
   places and projects (a new chat asks which server), settings, voice,
   Organiser, contacts, devices, account.
   *UI:* Settings → This device lists the current pairing, then "Also
   paired with" (each: Switch to it, Remove) and "Pair another server"; the
   pairing screen says another server is added and the same one replaced,
   and a pairing link still waits for a tap. With more than one pairing the
   thread list labels each row with its server, and (David, 20 Oct 2026:
   "Merged, with a filter") the filter row gains a chip per server to show
   just that one (`lib/threadFilter.ts`, `test/filter.mjs`).
   *Slices, smallest first:* **(1) done 20 Oct 2026** (sasonica-app
   `ac5b156`, merged): the list of pairings, pairing another server adds it
   (it becomes current, the other kept with its token), Settings switches
   and removes, Unpair (and sign out, revoking itself in Devices) falls back
   to another pairing, the list and Home snapshots forgotten on a switch
   (each thread's own snapshot kept: sessions are unique), `server_id` kept
   as the pairing's key; `test/multibase.mjs` against two mocks.
   **(2) and (3) done 20 Oct 2026** (sasonica-app `cab79fe`, merged; the
   merged list cannot ship without the routing): `getTargets` and `getSessionsState` ask every pairing in
   parallel, the current server's answer first (its failure is the list's
   error; its places, groups and mail stay its own), each row tagged
   `server {base, name}`, a thread two servers list shown once (the current
   server's, else the first's), a server that does not answer keeping its
   last rows; a strip of server chips above the projects (All servers, then
   each) and a server label on each row, both only with more than one
   server; the chosen server kept with the filter, not per project.
   Routing (`app/api/servers.ts`): each other server's threads noted
   (localStorage `sasonica.chat.threadServers`), and every call about one
   thread — the session in the path (`/threads/<s>/…`), in `?session=`, or
   in the body — goes to the server that listed it with that server's
   token, pictures included; a thread not yet listed (a link, a notice)
   that the current server does not have (404 on its stream) is asked of
   the others and noted. Remove also signs this device out on that server
   (`POST /me/signout`, which any device may do) while its token works.
   **Offline and search, done 20 Oct 2026** (sasonica-app `feeed04`,
   merged): a server whose last list or state ask got
   no answer says so on its chip ("hpo · offline"), its rows staying as last
   had, across a restart too (the saved list's); search (⌕) asks every
   server, or the one chosen on the same chip strip under its box (kept in
   the address as `server=`, seeded from the list's choice), its threads and
   messages merged (messages in the asked order, best taken in turn; each
   server paged from its own cursor) and labelled like the list; a thread's
   own find bar still asks only that thread's server.
   **(4) Home, needs pairing, documents and memory, done 20 Oct 2026**
   (sasonica-app `a6b567f`, merged): Home's Needs you,
   Working, unread replies, Recent and alerts are every server's, each
   labelled, threads noted for routing, alerts kept apart by server (their
   ids are each server's own) and Ack sent to the server that raised it;
   speaking now, the queue, hosts, digests, sign-ins, places and agents stay
   the current server's. A server that refuses this device's token (a 401,
   or a 403 to the list or its states) says "needs pairing" on its chip
   instead of offline, and the chip
   opens the pairing screen with its address (pairing again replaces that
   server's pairing). Search's documents and memory follow its server choice
   like threads, each labelled; a document opens from its own server
   (`/doc?path=…&server=…`, the reader's calls routed there), and a chat
   another server starts (a document's Ask) is that server's thread from
   then on. Still the current server's: a new chat, Settings, terms.
   **Home's strip, Fix it, the 403 rule and the notifier, done 20 Oct
   2026** (sasonica-app `03ff708`, merged): Home has the same
   server chip strip, the choice shared with the list's filter; Fix it on
   another server's alert starts the chat on that server; a 403 flags a
   server only from `/targets` or `/sessions/state` (elsewhere it can be a
   role refusing one route), a 401 from anywhere. The background notifier:
   `NotifyService.java` keeps its stream to the current server as it was and
   runs one lighter loop per other server (`Siblings.java`, reading
   `sasonica.chat.otherPairings` through SecretBox), threads and alerts
   only, each with its own alerts cursor (`alerts_after@<key>`), NotifyRules
   and Backoff, a refused token parking that loop alone; its notices name
   the server (sub-text), a "Needs you" from one has Open only (the answer
   buttons are the current server's), and a tap opens `/t/<s>?server=<base>`,
   which the thread page notes before it asks anything; Settings →
   Notifications turns another server off on this phone (`server_off@<key>`,
   `setServerNotify`). JUnit (`SiblingsTest`) covers the list, the keys, the
   plan of loops and the cursors; the APK builds (`assembleDebug`), and the
   rest wants a phone.
   **Answers and a refused server named, built 20 Oct 2026** (sasonica-app
   branch `multi-base-6`, not merged): a "Needs you" from another server
   has its answer buttons, AnswerReceiver sending to that server with its
   own token (the notice carries the server's key, the token is looked up),
   the follow-up after a 409 and the Answered line posted the same way; the
   listening notification names a server only when its loop stopped on a
   refused token ("hpo needs pairing"), and drops the name once it gets
   through again.
   (5) A server picker in Settings for the per-server things (voice, speech
   default, language, the Organiser, coding agents, presets, devices, the
   account), the phone's own settings staying global; a new chat on a
   chosen server; `relocate()` per pairing.
   *Decided (David, 20 Oct 2026):* sign out signs out of the current server
   only (as built); Remove also revokes this device on that server while
   its token still works (built in slice 2–3); Settings gets a server
   picker for the per-server things, the phone's own settings stay global
   (slice 5); a server that cannot be reached gets a small visible note, its
   chip saying offline and its last rows kept; search covers every server
   and is selectable, All servers or one, merged and labelled like the list
   (both built on `multi-base-3`); "Switch to it" stays until slice 5's
   picker; Home merges the thread sections and alerts from every server,
   each labelled, an alert's Ack going to its server, speaking now, the
   queue, hosts, digests and sign-ins staying the current server's; a
   server that refuses the token says "needs pairing" on its chip, which
   opens the pairing screen for it; search's documents and memory are
   selectable like threads, and a document opens on its own server (all
   three built on `multi-base-4`); Fix it on another server's alert starts
   the chat on that alert's server, Home gets the same server strip, and the
   notifier listens to every server (built on `multi-base-5`); a "Needs
   you" from another server gets its answer buttons, sent to that server
   with its token, and the listening notification names a server only when
   its loop stopped because it refused this phone (both built on
   `multi-base-6`). *The
   coordinator's call (20 Oct 2026):* "needs pairing" only on a 401, or on a
   403 from the list or its states. *Assumed, David not asked:* a thread listed by two servers (one
   day, siblings listing each other's) is shown once.
8. **A Media tab** (David, 28 Sep 2026) — a fourth tab in the app, not a
   companion app, because speech and music share the phone's player and its
   handoff already lives here. Music plays in the app's own ExoPlayer (a
   second channel beside speech, the same mpv-style IPC), so it works with no
   Termux setup. Music comes first, then books (in full: position synced to
   Audiobookshelf, chapters, sleep timer, offline, which retires Sasonica
   ABS for playback), then podcasts. Replays stay on their threads.
   **Step 0 done 28 Sep 2026:** the app's media player (sasonica-app
   `5372c64`, `Media3Music` on 6615, installed on p8a) and the server's
   `sasonica` music target (agent-media `f8e380a`, `e4cce51`;
   `MEDIA_MUSIC_SASONICA_ENDPOINT=tcp://p8a:6615` on red5). A YouTube track
   plays from the phone's own cache through Termux's new `music-files`
   service (localhost:6616), not via red5. Heard on p8a: play (7 s to
   start), pause, resume, seek, stop, and a reply pausing it and it
   resuming after (David: "perfecto"). The music default since 28 Sep 2026
   (`MEDIA_MUSIC_DEFAULT_TARGET=sasonica` on red5).
   **Step 1 done 28 Sep 2026:** the Media tab (sasonica-app `8ef310e`,
   installed on p8a): now playing with the chapter, a scrubber, play/pause,
   −15/+30, previous/next, Like, Stop, a mix's tracks (tap to jump), and
   "Plays on" for the next play; `GET/POST /music` (agent-media `d4f1d13`),
   a mix's chapters read on the phone with ffprobe so next/prev move by
   track (`b37108b`).
   **Lock-screen player done 28 Sep 2026** (sasonica-app `77a0cdc`,
   installed on p8a): `MusicSession`, a Media3 MediaSession and media
   notification over the music player; its next/prev move by the mix's
   chapters, which the server hands over at the load (agent-media
   `86b675c`); a tap opens the Media tab. Since then (28 Sep 2026):
   Recently played as a shelf with each track's thread (`GET /music/recent`),
   thumbnails (now playing, the shelf, the lock screen), a mix's tracks
   folding away, a chat box at the tab's foot. **Queued:** a **video or
   audio only** choice (David, 28 Sep 2026: "an option for video or audio
   only") — a YouTube track's video in the Media tab (and full screen) as
   well as its sound, with audio only kept as the default for the lock
   screen and the pocket; then the queue (step 2), then search. Sketch and
   order:
   [proposals/2026-09-28-music-tab.md](proposals/2026-09-28-music-tab.md).
9. **Hands-free: hold the mic** (David, 28 Sep 2026) — **First version built 29 Sep 2026** (sasonica-app `881dd91`, agent-media voice mode): the hold, the in-app listener with a chime, the countdown, sends marked `voice` (short spoken answers via the prompt hook, `voice_mode.py`), the next listen after the reply finishes speaking, two silences / "stop" / the bar's Stop end it, "cancel" listens again. Still to come: barge-in (speaking over a reply), "new chat" by voice, the headset button, and a spoken yes wait enforced by the app rather than only asked of the agent. The original plan: holding the mic key
   (as Send is held for New chat) turns on a conversation loop: after each
   reply the mic listens again, then the dictated words send after the
   3 s countdown (the mic key's since 28 Sep), so a misheard word can still
   be caught. Listening starts when the reply's *speech* ends, not its text,
   so the mic does not hear the phone. A chime marks each listen. The loop
   ends after two silent listens, on a tap, or on "stop". Spoken commands
   ("cancel", "stop", "new chat") are handled on the phone, never sent.
   Later: the headset button as the mic, for use with the phone pocketed.
   The one real piece of engineering is the listener: `dictate()` opens the
   platform recogniser's own screen each time, so the loop needs a quiet
   in-app `SpeechRecognizer` in the Android shell, with its own "listening"
   state in the composer.
   Replies in the loop (David, 28 Sep 2026: "much shorter replies so they're
   easier to reply to on the fly"): the app says the loop is on, and the
   server adds a voice-mode note to each message: one to three spoken
   sentences, the detail left in the thread; a decision ends the reply as its
   question, yes/no or two options. "More" or "go on" reads the rest.
   Speaking over a reply stops it and listens (barge-in). Anything hard to undo
   (a push, a delete, a send) is asked aloud and waits for a spoken yes. A
   long job is said as started ("running the tests, I'll tell you") and
   spoken again when it ends.
10. **Answer from the notification** (David, 1 Oct 2026) — "Needs you"
   carries the question's options as buttons (Allow / Deny, or one
   single-select question's options), answered by a native receiver through
   `POST /session/answer` with the question's `key`, the lock screen
   included; a tool's Allow asks for the unlock first. The server adds a
   trimmed `approval` to `/sessions/events`' rows. Anything multi-select or
   free text still opens the thread. **Built 1 Oct 2026** (agent-media
   session_events `brief`, sasonica-app `dc74d5d`, `AnswerReceiver`); not
   yet on p8a (next CI build, and a server restart on red5). Still to come:
   the Settings switch that asks for the unlock on every answer.
   [proposals/2026-10-01-answer-from-the-notification.md](proposals/2026-10-01-answer-from-the-notification.md).
11. **Routines from the phone** (David, 1 Oct 2026: "look good too") — a
   prompt on a schedule ("every weekday at 8, check X and tell me"), made
   and listed in the app, run as a headless session; its result arrives as
   a thread plus a digest or alert through the alert store (§6.17), so it
   plays from Home like the other digests. Proposal to write.
   (The third idea from the same list, share sheet and camera into a thread,
   was already built 25–26 Sep; only direct-share icons remain.)
12. **See what it's building** (David, 1 Oct 2026) — a dev server or HTML
   page an agent starts shows as a card in its thread; Open is a full-screen
   panel, through a per-preview tailnet port behind the device token, with
   hot reload passing through.
   [proposals/2026-10-01-see-what-its-building.md](proposals/2026-10-01-see-what-its-building.md).
13. **The phone as eyes and hands** (David, 1 Oct 2026) — an agent asks the
   phone for a photo, Do Not Disturb until a time, or its rough location
   (`phone_ask`, modelled on the mic asks §6.20); every ask needs David's
   yes, Allow through the unlock, and is kept in an audit list. Photo first.
   [proposals/2026-10-01-the-phone-as-eyes-and-hands.md](proposals/2026-10-01-the-phone-as-eyes-and-hands.md).
14. **Speaks when you're free** (David, 1 Oct 2026) — one "free now?" signal
   on the server (call, quiet ringer, meeting, manual); while busy,
   non-urgent speech waits, and on becoming free one short spoken catch-up
   ("While you were busy…") replaces the replay.
   [proposals/2026-10-01-speaks-when-youre-free.md](proposals/2026-10-01-speaks-when-youre-free.md).
   Add-on, proposed: Jev (TypeSafe's typed-decision model) answers the
   soft half (is this event really busy, should this break through, is
   the catch-up worth a voice), with the rule as its fallback.
   [proposals/2026-10-01-jev-for-free-now.md](proposals/2026-10-01-jev-for-free-now.md).
   **Step 1 built 2 Oct 2026** (server side: `free.py`, `/device/state`,
   `/free`, the gate; contract §6.22), with Jev in shadow. **Step 2 built
   2 Oct** (sasonica-app 3d7de7f, on p8a): `Holds` reports call, voice and
   quiet. **Step 3 built 2 Oct**: the spoken catch-up on busy → free
   (`catchup.py`; the gateway summary, the template on failure). Its
   notification ("While you were busy · 2 replies, 1 alert") is not built.
   **Step 4 built 2 Oct**: Settings → "Hold replies during meetings" reads
   the phone's calendar (meeting under way, chained back-to-back, its end
   and title only); Jev's `event_busy` asked in shadow. **Step 5, part 1
   built 2 Oct**: the morning digest joins the catch-up; "catch me up" on
   demand (`POST /catchup`, `media catchup`); the "While you were busy"
   notification (`catchup` frame). **Part 2 built 2 Oct**: Settings → "When
   you're busy" (silent holds replies or only alerts; the calendar; Catch
   me up), the "Holding 2 for later" card (Catch me up / Speak as it comes
   / Later; `free` frame, `POST /free`). Night: the ringer only (David,
   2 Oct). Item 14 is done; Jev stays in shadow until its log is reviewed.
15. **Off the tailnet** (David, 3 Oct 2026: "move away from the
   dependencies on tailscale for sasonica"). The phone reaches the server
   through a Cloudflare Tunnel to an app-routes-only listener. Everything
   red5 opens into the phone (speech on 6614, music on 6615, ssh paths)
   moves onto the phone's own stream. Long term, a `sasonica.com` lookup
   (then maybe a relay) so other users never need a Cloudflare account.
   [proposals/2026-10-03-off-the-tailnet.md](proposals/2026-10-03-off-the-tailnet.md).
   **Step 1 built 3 Oct**: the desk-route hardening §19 asked for
   (constant-time token compare, a 10-failure lockout per source).
   **Step 2 built 3 Oct**: the app-routes-only listener (`--public`,
   127.0.0.1:8789 on red5), tested through a quick tunnel; `/img/` no longer
   serves the spool's `pair-code`. **Step 3 built 3 Oct**: the named tunnel
   `https://red5.sasonica.com` (`sasonica-tunnel.service`), `pair --server`,
   and a 60 s ping cap for tunnelled streams. **Step 4 done 3 Oct**: p8a on
   `https://red5.sasonica.com` (Settings → Advanced → Server address, its
   own section since sasonica-app 47b3811; same device token), seen from
   its public address. **Speech as frames, live 3 Oct** (agent-media
   speech_frames.py, sasonica-app cac6e3d, build 1154): red5 no longer
   dials p8a for speech; a test line played, then the switch
   (`MEDIA_SPEECH_SOCKET_SASONICA=tcp://127.0.0.1:16624`). **Music as frames
   live 3 Oct** (build 1157, port 16625); its YouTube work (cached?, fetch,
   title, chapters, mixes) runs on the phone's Termux worker, dialled out,
   since 3 Oct (`MEDIA_PHONE_JOBS=1`); only the rooms lane's fetch is still
   `ssh p8a`. **8 Oct: notifications, books cache and book state off ssh**
   (#5 a `notes` frame, contract §6.23, agent-media 2bd60c3, sasonica-app
   44c846b, installed and a test note shown and taken down; #4/#6 on the
   Termux worker, 5143153), and the per-reply music duck no longer dials
   p8a:8773/6601 (two unused endpoints commented out in the env file).
   Left dialling in: the 6614 relay spares (speech frames' fallback), #7
   handoff (6617), the rooms-lane fetch, and dev tools; the proposal lists
   them. **9 Oct: #7 built as frames** — the radio's hand-off player is a
   third frame channel, `handoff` (agent-media fff076f, canvas on 16626,
   refuses at once with no device; sasonica-app `handoff` merged to main,
   0a3ff99, no 6617 socket, reports which way of asking started each song).
   APK install pending (CI's artifact storage is full), then
   `MEDIA_RADIO_HANDOFF_ENDPOINT=tcp://127.0.0.1:16626`.
   **For other users (David, 8 Oct 2026, "we're planning on moving off
   tailscale anyway… work towards that")**: a stranger's install has no
   tunnel and its pair link names a Tailscale IP the app refuses, so
   sasonica.com/start says a computer server is "coming soon". Chosen: the
   installer sets up a **quick tunnel** (trycloudflare, no accounts) to the
   `--public` listener and pairs with its URL, **plus the sasonica.com
   lookup** (a Worker keeping each install's current URL, signed by a key
   made at install) so the app re-finds the server after a restart instead
   of pairing again. `_pair_host` stops falling back to a bare hostname.
   The lookup was on hold pending Matrix from 8 Oct (Sasonica's own
   homeserver may make it throwaway); **the hold was lifted 16 Oct 2026**
   (David: "you decide"; the Matrix transport proposal may still replace
   it). Until the Worker is deployed and the app ships, the phone **pairs
   again whenever the quick tunnel's address changes** (a reboot, a crash);
   the installer and `pair` say so. **Quick
   tunnel built 8 Oct** (agent-media 21c5ead): `sasonica install` fetches
   cloudflared 2026.9.3 (pinned sha256 per platform) to ~/.local/bin, writes
   `MEDIA_VISUAL_PUBLIC=127.0.0.1:8789`, and adds a third service,
   `sasonica-quick-tunnel` (`sasonica tunnel` = `media-tunnel run`, which keeps
   the URL in `<state>/tunnel.json`). Skipped where the phone already has a
   way in: `MEDIA_VISUAL_PAIR_SERVER`, `~/.cloudflared/config.yml`, a
   checkout's canvas unit (red5 has all of these but the first), or
   `--no-tunnel`. `pair --device` prefers the running tunnel; with no tunnel,
   no tailnet and no `--server`/`--host`, it fails with what to do instead of
   naming the hostname. install.sh waits for the tunnel's URL and pairs with
   it. Tested on red5 with a throwaway HOME and other ports: a device paired by
   `POST /pair` through the tunnel; after the tunnel restarted under a new
   name, the old one answered 530 and the same token worked at the new one (so
   Settings → Server address would also do, instead of pairing again). The
   real one-liner in a Debian bookworm container (binary af70566) fetched
   cloudflared, wrote the listener, and (no systemd there; started by hand)
   paired a device from red5 through its tunnel. CI fix e837fba, 41d3216.
   **Windows too (16 Oct, branch `lookup`)**: cloudflared-windows-amd64.exe
   (2026.9.3, sha256 pinned) to ~/.local/bin, the logon task
   Sasonica\quick-tunnel (task XML, restarted when it fails), cloudflared in
   a kill-on-close job so it never outlives its runner, install.ps1 waiting
   for the URL; the binary's Windows smoke test pairs through a stand-in
   cloudflared, and a network step (allowed to fail) through a real one. The
   lookup as built (a Worker at sasonica.com/r/<id>, signed by a key made at
   install; the app re-finding its server) is revived on agent-media branch
   `lookup` and sasonica-app branch `lookup` (16 Oct; from `lookup-hold` and
   `tunnel-lookup`), not merged or deployed yet; KV namespace
   `sasonica-lookup` is kept on the South Pen Labs account.
   **The real app paired, 8 Oct 2026** (David: the website changes "after a
   real phone pairs"; the app in a browser accepted): the released binary
   (2026.10.07+e8eab48) in a throwaway HOME, `install --no-services` (it
   fetched cloudflared), listener 18789, a quick tunnel; the app's own web
   bundle served at `http://localhost` as the APK loads it, headless Chromium
   at 412×900. Welcome → Connect → the pasted link paired through
   `https://…trycloudflare.com` (no CORS or mixed-content trouble); Home,
   Threads and New chat loaded from the server (agent list: opencode, free
   models), and a message got opencode's reply. Tunnel restarted: the old
   name answered 530 ("Could not reach …"), Settings → Advanced → Server
   address with the new one said "OK", the same device token, and a reply
   came live on a thread stream open 3 min (the ping cap holds). Found and
   fixed: the app says `sasonica pair --device`, which the binary did not
   have (agent-media 6f77632), and a short reply with no speech keys stood
   one letter a line (sasonica-app 6df3bb8). Screenshots
   /tmp/playwright-mcp-headless/pairtest-*.png.
16. **The hosted relay** (David, 4 Oct 2026: the next step towards other
   users, chosen over a sasonica.com Matrix homeserver and over hosted
   compute — Sasonica runs the relay, the customer's own machine runs the
   agents, so no per-account isolation is needed). **Built and live at
   `https://relay.sasonica.com` (sasonica-shell `1c9b785`, `9b5da67`)**: one Worker, a Durable Object per tenant
   over the same schema and code as a self-hosted Worker; idle runners wait
   on a hibernating WebSocket doorbell (0.3 s round trip, no idle requests,
   under `wrangler dev`). Tenants are made with an admin token
   for now (red5 `~/.config/sasonica-relay/admin.env`). Next:
   `sasonica install --hosted`, joining by a Sasonica
   account sign-in (an OAuth client for the relay on cms.sasonica.com).
   sasonica-shell [docs/hosted-relay.md](https://github.com/southpenlabs/sasonica-shell/blob/main/docs/hosted-relay.md).
   **Opening it (David, 8 Oct 2026, "let's get it ready for opening")**:
   a **free beta first**, billing later. Before `RELAY_ALLOW_ACCOUNTS=*`:
   rate limits (`/join/start`, MCP calls, runner endpoints), one machine
   per account, retention enforced on the relay (a Durable Object alarm
   drops rows past 30 days and dormant tenants), a per-account tenant list
   with remove, and account deletion in Drupal removing that account's
   tenants. Also: a terms-of-service draft for David to review, a Windows
   end-to-end run with a second Sasonica account, publishing Google
   sign-in (now that /privacy-policy is live), and the privacy policy's
   "not yet open to everyone" line changed on opening. sasonica.com/start
   and /download say Windows is "coming soon" until then.
   **Built and deployed 8 Oct 2026** (sasonica-shell `a3d7943`, `90e0bc5`,
   docs `b0202c1`, `1206fa5`; websites `f153eab`, `29ddbf0`, ToS draft
   `943ee08`): rate limits counted in Durable Objects (vars: 5 joins/min
   per IP, 120 MCP calls/min per connector URL, 300 runner requests/min;
   Cloudflare's rate-limit binding let everything through live), one
   machine per account with a second join *replacing* the first (said
   before the button), a daily alarm deleting rows past 30 days and
   tenants with no runner contact for 90, `relay.sasonica.com/account`
   (list, last seen, Remove) plus a Bearer API, and `hook_user_delete`
   calling `/hooks/account-deleted` (tested live). The 4 Oct test tenant
   is removed. Found: non-admin accounts could not sign in to any OAuth
   client (missing `grant simple_oauth codes`); fixed. Test account
   `sasonica-test2` (uid 5) made and allowed beside David's (sasonica-shell
   74fc418). **Windows end-to-end passed 8 Oct** on GitHub's Windows runner
   (manual workflow `windows-hosted-e2e.yml`; red5 has no KVM): the real
   one-liner, Join by sasonica-test2 from red5's headless browser, replace
   on a second join, and `hostname`/`ver` through the relay over MCP
   (runs 37701441963, 37701570159; hosted-relay.md 0858f45). Google sign-in
   published 8 Oct. Terms of service published 8 Oct
   (sasonica.com/terms). **Opened 8 Oct 2026** (David's go-ahead):
   `RELAY_ALLOW_ACCOUNTS=*` deployed (sasonica-shell 6af1773); the privacy
   policy, Get started, Download and the front page give Windows the
   one-liner.

17. **Background agents you can act on, and for Codex** (David, 7 Oct
   2026, after asking whether Sasonica has what the arrow key on an empty
   prompt opens in Claude Code and Codex). The strip and an agent's own
   thread (§6.12, built 22 Sep) are read-only and Claude Code only. Add:
   (a) **Stop** on a running subagent and (b) **send it a message**, as the
   Claude Code viewer does — the server half needs a way into the live
   session to do either (a pane's keys, or the headless session's input),
   to be found out; (c) **Codex subagents** in the same strip, since Codex
   now spawns and shows them too: where its rollouts record a child thread
   and its parent (codex-cli 0.160.1 on red5) is not yet looked at; the rows
   and log keep §6.12's shapes, so the app needs no Codex-specific code.
   **(a) and (b) built 7 Oct** (agent-media `913389a`, §6.12a; sasonica-app
   `f1c99c5`): Stop in an agent's page header while it runs, a message line
   at its foot. Headless Stop is `stop_task`; a pane's Stop and every
   message are asked of the main agent (TaskStop, SendMessage). **(c)
   researched 7 Oct**: a spawned child is its own rollout whose
   `session_meta` has `session_id` = the root thread and
   `source.subagent.thread_spawn {parent_thread_id, depth, agent_nickname,
   agent_role}`; the parent's `spawn_agent` call returns `{agent_id}`, and
   ends arrive as `<subagent_notification>` user messages. Stop and message
   for Codex go through the main agent too (`close_agent`, `send_input`):
   the children live inside the TUI. Waiting on a real child rollout to
   build against (red5's Codex login had expired).

18. **Signed installers** (David, 8 Oct 2026: "a link that can be downloaded
   from and ran rather than having to type an install command"; "scripts
   now, signing later"). sasonica.com/download offers the server installers
   as files: `Sasonica-Setup.cmd` (Windows, runs install.ps1) and a zipped
   `Install Sasonica.command` (macOS, runs install.sh), from websites
   `front/sasonica/installers/`. They are unsigned, so SmartScreen ("More
   info → Run anyway") and Gatekeeper ("Open Anyway" in Privacy & Security)
   warn once. Before a wider launch: a signed Windows `.exe` (an OV/EV code
   signing certificate, or Azure Trusted Signing) and a notarised macOS
   `.pkg` (Apple Developer Program, US$99/yr), built in CI.
   **Tested 8 Oct on GitHub's machines**: the `.cmd` downloaded (marked as
   from the internet) and run on windows-latest, joined by sasonica-test2,
   `hostname`/`ver` back through the relay (sasonica-shell
   `windows-hosted-e2e.yml`, entry=cmd, run 37716447816); the macOS zip
   unpacked with ditto on macos-14, the `.command` run, three launchd
   services up and `/healthz` "ok" through the quick tunnel (agent-media
   `macos-installer.yml`, run 37716796623). Not testable there: the
   one-time SmartScreen/Gatekeeper click.

19. **Account sign-in from a browser** (David, 14 Oct 2026) — **DONE 14 Oct
   2026**: off the native platform the sign-in comes back to
   `https://sasonica.com/auth` (websites `front/sasonica/app/auth`, Drupal
   `sasonica-app`'s second redirect via `drush/accounts-setup.php`, run on
   red4), which reads the way back from the state (`<random>~<pairing URL,
   base64url>`) and asks "Continue to <host>?" before handing the code on —
   no consent screen, so no silent forward (David chose the confirm). The tab
   itself navigates; `/enrol` needed no change (it passes the app's
   `redirect_uri` through). Plain-http pages have no `crypto.subtle`, so the
   PKCE hash falls back to `lib/sha256.ts`. Round trip checked from red5's
   `:8795` preview with a fake code (sasonica-app `de6467d`, `bf8181d`;
   websites `6a0e8f9`). A real sign-in to the end is still to do by hand.
   **Next: item 20.**
   The original note: the app's web
   build (red5 `:8795`, or any server's) can only pair: "Sign in with a
   Sasonica account" sends the code back to `sasonica://auth`
   (sasonica-app `app/api/auth.ts` `AUTH_REDIRECT`), which only the Android
   shell catches, so a browser stops after the cms.sasonica.com / Google
   page. Off the native platform, use an `https://` redirect on the page's
   own origin (a `/auth` route that hands the code to the same
   `finishAccountSignIn`), register those redirect URIs with the
   cms.sasonica.com OIDC client, and have `POST /enrol` accept the
   `redirect_uri` the app sent rather than assume the custom scheme. Name the
   device from the browser ("Firefox on …") as the phone's is named.

20. **Sign in on sasonica.com: your account and your servers** (David,
   14 Oct 2026: "a way to log in and see a list of servers available to
   connect with from sasonica.com"). **Built and live 14 Oct 2026**:
   sasonica.com/account (Account in the menu) signs in with the
   `sasonica-site` client and shows the account (Edit account → cms), Your
   servers (name, address, last seen, "opens on your tailnet" for 100.64/10,
   **Open** → the server's web app at `/pairing?server=<api>`, Remove) and
   hosted-relay machines. Drupal `/api/servers` in sasonica_oidc (user.data,
   pruned after 30 days); agent-media `servers_report.py` reports on sign-in
   and daily; red5 reports `https://red5.sasonica.com` + its `:8795` preview
   (canvas drop-in `servers.conf`). Checked live with sasonica-test2: sign
   in, list, Open link, Remove, sign out (websites `ca8281f`; agent-media
   `e9082fe`; sasonica-app `5758737`). Still open: a server's web app for
   installs other than red5 (only red5 sets MEDIA_WEB_APP_URL, so others show
   "Open it in the app").
   The plan as written: today the site has no sign-in, on
   purpose: it does not know where anyone's server is, and an account only
   enrols devices on the server (item 19). Three parts:
   - **Sign in / Account** in the site's header: a Sasonica account sign-in
     (a third redirect on the `sasonica-app` client, or a confidential
     `sasonica-site` client with a session cookie), then a page with the
     account's name and picture and a link to cms.sasonica.com for password,
     email and deletion (what the app's Edit profile already does).
   - **Your servers**, on that page: each agent-media server the account has
     a device on, with its name, when it was last seen and an **Open**
     button to its web app (`/pairing`, which then signs in by item 19).
     Nothing holds this list yet. Proposed: a server tells cms.sasonica.com
     its name and the addresses it answers on when an account enrols there
     and then daily, with that account's own access token (it already keeps
     the refresh token for `/me`); Drupal keeps a row per (account, server)
     and drops it after 30 days unheard; Remove on the page, and revoking the
     last device on a server drops it too. The parked lookup (agent-media
     `lookup-hold`, sasonica-app `tunnel-lookup`) is the same need from the
     app's side; reuse its shape.
   - **Hosted-relay machines** in the same list: relay.sasonica.com/account
     already lists an account's machines (item 16); read it through its
     Bearer API so one page shows both.
   Open questions: a tailnet address (red5's `100.x`) only opens on the
   tailnet, so say so beside it rather than hiding it; whether the site
   should show an address at all, or only "Open" through a per-server link;
   and whether the list lives in Drupal or in a Worker beside the relay.

21. **A web app for every server** (David, 14 Oct 2026: "app.sasonica.com
   … it should at least work on the Wi-Fi network though, right? … should
   we offer any help or advice in setting up tailscale"). Only red5 has a
   web app today (the `:8795` preview), so Open on sasonica.com/account
   works for red5 alone. An https page cannot call a plain-http LAN or
   tailnet address (mixed content, Chrome's private-network rules), so two
   halves (David chose both, plus help):
   - **app.sasonica.com**: the sasonica-app web build, hosted once over
     https, for servers with a public https address (named or quick
     tunnel). **Built 14 Oct 2026**: red4's Caddy serves
     `/srv/sasonica/web` (SPA fallback, hashed assets a year, the rest
     no-cache; DNS a proxied A record like sasonica.com's); websites
     `bin/publish-web` (run by hand) builds sasonica-app's origin/main both
     ways and publishes the root build, with `sasonica-web.tar.gz`,
     `sasonica-web-app.tar.gz` (the `/app/` build) and `version.json` beside
     it (websites 50ca5f8). On that host the app has no default server
     (sasonica-app 96a097d, `hostedApp()`): welcome, then pairing, or
     `?server=`; account sign-in goes through sasonica.com/auth back to it.
     CORS needed nothing: the app routes already answer `*`.
   - **The server serves it too**: agent-media serves the same bundle at
     `/app/` on its own listener (http on the LAN/tailnet), shipped with
     the server (the binary build fetches the release's web bundle).
     servers_report sends `web` = that address when no MEDIA_WEB_APP_URL.
     **Built 14 Oct 2026**: `web_app.py` serves the bundle at `/app/`
     (through `app.dispatch`, so the canvas, its public listener and the
     canvas-less server all have it; serve.mjs's caching, SPA fallback,
     gzip) from MEDIA_WEB_APP_DIR, else `web_app/` beside the module.
     sasonica-app builds it with `SASONICA_BASE=/app/ pnpm build`
     (build-app/client; default build unchanged), where serverBase() is the
     page's own origin; its CI attaches `sasonica-web-*.tar.gz` to each
     build release. `deploy/binary/build.sh` copies it in from
     SASONICA_WEB_BUNDLE, else (since 14 Oct 2026, no token: David skipped
     the secret) fetches https://app.sasonica.com/sasonica-web-app.tar.gz,
     which is only as new as the last `bin/publish-web`.
     servers_report: `web` = `<api>/app`.
   - **When it can't be reached**: **built 14 Oct 2026** (websites
     50ca5f8). The Account page asks each https server from the browser
     (`GET <api>/enrol`, 6 s): no Sasonica answer → "Not reachable from
     here" and a link to sasonica.com/help/reach; a plain-http LAN address
     says it opens on its own network; 100.64/10 keeps "opens on your
     tailnet". Open = the server's own web app when https (it matches the
     server's version), else app.sasonica.com for a public https `api`,
     else the server's own http one. The help page: re-run
     `sasonica install` for the quick tunnel, `sasonica media-tunnel url`,
     `sasonica pair --device NAME`; or Tailscale (install on both, same
     account, `http://<100.x>:8781/app`); what "not reachable" usually
     means. Remove left the rows (David): servers drop off after 30 days
     unheard, and the page says so.
   **Done 14 Oct 2026.** Left: `bin/publish-web` on a timer like
   publish-app, if the binary should always carry main's app.

22. **A light mode for sasonica.com** (David, 14 Oct 2026, from the
   admin's accent: "should we add a light mode for sasonica.com?"). The
   site, the app and the brand are dark-only today, and the mint
   (`#7fd1ae`) only reads on the dark ground: on white it is about 1.7:1 for
   links, and white button text on it worse. A light mode needs a second
   palette, not an inversion: a deeper green for text and buttons on light
   (the mark's `#578e77` is a start, about 3.6:1, so likely darker still),
   light grounds and borders, then every page of front/sasonica (Next.js,
   `app/globals.css`), the chat, the social-preview image and the wordmark's
   mint "sonic" checked in both. Follows the device (`prefers-color-scheme`),
   with a switch only if asked for. The mark's own dark tile works on either.
   Then the admin (Gin, mint accent and always dark since 14 Oct 2026)
   can take the light palette's green in light mode; until then the Gin
   login pages (websites `sasonica_brand/login`) stay dark.

23. **Listen to a note, with follow-along** (David, 15 Oct 2026: "how might
   I have an MD file read to me?", then yes to a button, "nice to have
   follow along for it too"). Today it is `media doc play <path>` or asking
   an agent in a thread; the app's Notes reader opens `.md`/`.org`/`.txt`
   (`GET /org/read`) and has no way to hear one.
   **Steps 1 and 2 DONE 15 Oct 2026** for notes (agent-media `1204f2b`,
   sasonica-app `1fd0e02`, contract §6.10 `/org/listen`; red5's canvas
   restarted). The play key on a note is now Listen; `/org/say` is kept but
   unused by the app. **Next (David chose "Notes, then project docs"):** a doc
   path mentioned in a thread opens in the same reader with Listen — needs a
   read route outside the Org tree, and the reader drawing Markdown (today it
   draws Org, so a `.md` heading is skipped and a fence is plain text).
   **Step 3 DONE 15 Oct 2026** (agent-media `f347b96`, sasonica-app `b4406a2`; red5's canvas restarted). As planned:
   - Server: `GET /doc/read?path=<abs>` for `.md/.txt/.org` under the doc
     roots (`docs.doc_roots()`, ~/projects; no dot-dirs, no escaping) →
     `{path, title, text}`, gated like /org/read. `/org/listen` takes
     `{"doc": "<abs path>"}` instead of `path` (same render, sidecar `doc`
     instead of `path`; GET answers `doc`). Contract §6.10, tests beside
     test_org_listen.py.
   - App: a `/doc?path=` page sharing the note page's Listen + follow
     (hooks/useNoteListen.ts, taking a `{doc}` or `{path, at}` key) and a
     line-aware Markdown body (lib/md.tsx, `data-line` per block like
     lib/org.tsx's OrgBody); in a thread, an inline-code or linked path
     ending .md/.org/.txt opens it (resolved against the thread's cwd).
     Mock + test/doc.mjs; What's new line; restart red5's canvas; push.
   1. **Listen** in the reader (the whole note, or from the heading it is
      opened at): a server endpoint over `docs.render_doc` and the book
      channel, `target` the app's usual choice (phone by default). The
      cached render, chapters per heading and resume all come with it.
   2. **Follow-along:** the reader marks the sentence being spoken and
      scrolls with it. No forced alignment is needed (unlike the ePub
      read-along, `proposals/2026-09-09-book-read-along.md`), because we
      synthesise the audio ourselves: `_render_sections_to` already ffprobes
      each section's length for the chapter marks. Rendering per paragraph
      (or sentence) gives a timings sidecar `{start, end, line, text}`
      beside the cached audio; the reader matches the book position against
      it. The projection drops code fences and tables, so a timing points at
      the source line it came from, and skipped blocks are passed over.
      Tapping a paragraph seeks there.

24. **Edit a document from the reader** — **DONE 16 Oct 2026** (contract
   §6.10 `POST /doc/ask`; red5's canvas restarted). As built, the box is about
   the paragraph tapped, else the one being read, else the whole document
   (not the one at the top of the screen: a general question would have
   quoted the title). The page stays put after sending, with a link to the
   chat. **Then, the same day** ("let's do that too"): an Undo key (POST
   /doc/undo, the last edit when committed on its own and under 2 h old), Edit
   by voice while listening (the mic key pauses the reading, dictates, sends
   about the paragraph being read, resumes; the phone's dictation only), and
   margin notes (the menu's Note…, kept on the device with a ✎ in the margin,
   Apply sends them as one message, `action: "notes"`). The voice edit aims
   at the paragraph, not the sentence: the timings are per paragraph.
   (David, 16 Oct 2026: "a chat box at
   the bottom of the new document reader ... a reply takes into account the
   current position as possible context as a way of editing the document",
   then "maybe a little pop-up context menu for the action to be
   performed").
   - **Chat box** at the foot of the `/doc` page, as the Organiser's
     (`POST /org/ask`, `org_chat.py`): a `POST /doc/ask {"path", "at"?,
     "quote"?, "text"}` opens a session in the document's project (its git
     root), first message `About <title> (<path>), under "<heading>", this
     paragraph: "<quote>": <text>`. The paragraph sent is the one tapped,
     else the one at the top of the screen, else the one being listened to.
     It is quoted whole, because a line number moves once the file changes.
     Recorded against the document like a note's `chats`.
   - **Context menu**: long-press a paragraph (tapping it already seeks the
     listening) → a small pop-up: Ask, Shorten, Expand, Plainer, Rewrite…,
     Delete. Each one fills the chat box with that action on that paragraph.
     Ask and Rewrite… wait for your words; the others send at once.
   - **After the edit**: the reader reloads when the file changes and marks
     the changed paragraphs. Each edit is its own commit, so Undo is a
     revert. The cached Listen render is invalidated as any edit would.
   - Later: "cut that" / "say that more plainly" by voice while listening,
     aimed at the sentence follow-along is on; margin notes gathered while
     reading and applied in one pass.

25. **Gemini CLI as an agent** (David, 17 Oct 2026: "let's add Gemini to
    sasonica settings" — then "Full agent"). Plan in
    `docs/proposals/2026-10-17-gemini-as-an-agent.md`: phase 1 Settings row,
    speech and threads; 2 start from the phone and resume; 3 headless over
    `gemini --acp`; 4 model picker, API key, profiles. Installed on red5
    (0.64.0), not signed in; its context hook is in (`e53eae2`). Sign-in from
    the phone is the open question (`NO_BROWSER=true` gives a code to paste).
26. **Quick chat** (David, 17 Oct 2026; renamed from "Quick question" the same day). A chat that skips the coding
    agent: the question goes straight to a fast model (Haiku, Gemini Flash)
    through red5's LiteLLM gateway — the route the speech summaries already
    take (`intake/_summary.py`) — so the reply starts in about a second
    instead of after an agent's system prompt, tool list and loop. Spoken
    like any reply, kept as a thread, and handed to a full agent (with the
    conversation so far) when it turns into real work. Replaces the idea of
    Simon Willison's `llm` as a light agent, parked the same day: its speed
    is the direct call, which the server can make itself; `llm` added only
    another route to models opencode and pi already reach.

27. **An agent can post into another thread** (David, 17 Oct 2026: "add it
    to the roadmap"). Handing work to an existing conversation — not a fresh
    one — has no route now that sessions run headless. `agent-mail-deliver`
    types into a tmux pane, and with no pane in the folder it starts a *new*
    session; `agent-handoff` always starts a fresh chat; and the one way into
    an idle Sasonica thread, `/reply`, wants a device's or an ABS bearer, which
    an agent should not borrow to speak as David. Met when the Paseo-trial
    session tried to give the compaction thread (`a1af5423`, the one that did
    Gemini) agent mail #110, the Grok CLI as the seventh agent, and David had
    to paste the nudge himself. Wanted: a local, host-only way for an agent
    on the box to put a message into a named thread (by session id), marked
    as from that agent rather than from David — reviving the thread
    headless if it has ended, queued behind a turn in progress, refusing over
    a draft like the pane path does — and `agent-mail-deliver --to <session>`
    using it.
28. **Messages: search, Show and Sort** (David, 17 Oct 2026: "a way to
    search in the messages tab… maybe even some selections for show and
    sort"). A box that narrows rooms by name or last message; Show Active,
    Unread, Archived, Everything or one service; Sort Most recent, Unread
    first, Name A–Z or By service. ⌕ searches inside the texts, which needs
    the rooms in the search index (§6.14, `96c26b2`). **DONE 17 Oct 2026**
    (sasonica-app `c970862`), deployed to all three web builds and the
    canvas.
29. **Contacts: people, not rooms** (David, 17 Oct 2026). One row per
    person across their SMS and Signal rooms, a contact card, texting a
    contact, and edits written back. The sources are Google Contacts and the
    Drupal CRM on red4, with Google winning where they disagree. The plan is
    docs/proposals/2026-10-17-contacts.md: open questions first, then five
    steps, starting with Google read-only. **Timeline** (18 Oct 2026):
    daily per-contact rollups kept in the CRM as a distributable
    `crm_activity` module; texts, Signal and agent notes on by default,
    calls and email off; after step 3. **Server and module built 18 Oct
    2026** (§6.25a: `GET /contacts/<id>/timeline`, `POST
    /contacts/<id>/notes`); live steps in the proposal. **App built 19 Oct
    2026** (sasonica-app `1692432`): Messages one row per person, a room's
    person chip (their card, the switch between their rooms), People
    (`/people`) and the card (`/people/<id>`: phones, emails, address,
    notes, the CRM's fields, their rooms, the timeline by day, a note box).
    Still to do: edits (step 4), texting someone new (step 5), and "not in
    the CRM yet" on an entry whose `synced` is false. **Google per
    account, built 18 Oct 2026** (§6.25 "Google, per book"; sasonica-app
    `1a509de`): Settings → Contacts signs this account's own book in to
    Google (a loopback listener on the phone, the browser for consent),
    Sign in again when the testing-mode 7 days are up, Sign out. The Google
    project stays in testing: David adds each person as a test user.
    **Bridged networks and labels, built 19 Oct 2026** (§6.25 "The bridged
    networks", "Labels"): the person of each one-to-one Messenger,
    Instagram, Teams and SMS chat is a contact (a shared number or address
    joins; a same name off SMS is "Same person?" on the card, Join / Not
    the same), their rooms roll up as their own channels, and Google's
    labels (the phone's groups, "My Contacts" included) are on each person
    with a filter on People. **The full list, built 19 Oct 2026** (§6.25
    "Exports", `POST /contacts/import`): everyone in a bridged group chat
    ("Messenger groups", "Instagram groups", "Teams" labels), and Facebook
    friends and Instagram followers / following from each network's own
    JSON export, uploaded in Settings → Contacts → Import a file (the export
    steps are in the proposal's "The full list"). **The export cycle, built
    19 Oct 2026** (§6.25 "The export cycle"): a reminder 90 days after each
    import, Meta's "ready" mail noticed (opt-in, IMAP), and the zip
    imported from the phone's Downloads by itself. **Past conversations,
    built 19 Oct 2026** (§6.25 "Past conversations"): Messenger and
    Instagram threads and Gmail's old Google Talk chats as timeline days
    (counts only, one-to-one only, merged by the larger count), any size
    through `media contacts import` or the import folder; the app's import
    note says the days added. **Show and Sort** for People (19 Oct 2026):
    Recent, Name, Most talked to; the last 30 days, 3 months or year; one
    service; each row's last talked (agent-media 908422f, sasonica-app
    aae0c58).
30. **Search: Show and Sort** (David, 19 Oct 2026: "show and sort filters
    for the search page"). Sort: Newest first, Oldest first, Best match
    (bm25 on the server) or By thread (the hits under each thread, the
    busiest first); the sort is kept per device. Show adds, beside the
    list's status, agent and project: the sections (Threads, Messages,
    Documents, Memory), Said by (me / others), When (today, past week,
    month, year) and Kind (coding threads / message rooms). Who, when, kind
    and sort are asked of the server (§6.14 `sort`, `since`, `who`,
    `rooms`), so a page is full of what is shown. **Built 19 Oct 2026**,
    not yet deployed (the server change needs a canvas restart).
31. **Card links open in the app** (David, 19 Oct 2026: "yeah queue it").
    The contacts write-back (agent-media `contacts_writeback.py`, `9042e77`)
    puts a link per bridged account on the person's Google entry,
    `https://<server>/app/people/<id>?account=<matrix id>`; the app's App
    Links claim only `/app/t/`, so on the phone a tap opens the browser. Add
    `pathPrefix="/app/people/"` beside it in AndroidManifest.xml (the
    server's assetlinks.json already vouches for the host), let
    threadLinks.ts treat a card link as the app's (open `/people/<id>` in
    place), and have the card note `?account=` (scroll to, or highlight,
    that account's row). Check a cold start and a warm one from Google
    Contacts.
32. **Find in this thread** (David, 19 Oct 2026: "Can we have a search
    feature within a thread?" → a find bar that jumps). "Find in this
    thread", first in a thread's ⋮ menu (not a ⌕ beside it: the labels'
    row is full at the phone's text size), opens a box under the header.
    The words go to the server for that thread only (§6.14 `session=`,
    oldest first, a `total`), so matches in pages not yet loaded are found;
    "3 of 12", starting at the newest; ↑ / ↓ (Enter, Shift+Enter) step
    through them with the search jump (loaded in, centred, the words lit);
    "No matches"; Escape closes. Tool steps too with Advanced on.
    **Built 19 Oct 2026** (sasonica-app `FindBar.tsx`, test/find.mjs).

33. **Messages: spaces** (David, 19 Oct 2026: "I think we need a way to
    better manage the messages tab. maybe we can use spaces as well?").
    The plan is docs/proposals/2026-10-19-messages-spaces.md: Matrix spaces
    as a chip row across Messages (All, Unread, then spaces), the bridges'
    own spaces for free, spaces David makes (Family, South Pen Labs, Rye
    Remedial), and contact labels filling them. Step 1 is a fix on its
    own: the bridges' space rooms are probably taken in as empty threads.
    Plan only; open questions first.

35. **Mail in Sasonica** (David, 20 Oct 2026: "I'd like to handle emails
    from Sasonica"). Both a Mail tab (accounts, folders, search, compose)
    and mail as a room on a person's Messages row; Gmail first (fifteen
    accounts already in red5's notmuch, msmtp set up), Outlook next through
    the calendar's Microsoft Graph sign-in. The plan and its open questions
    (All Mail or Gmail's API, mbsync or lieer, spam, read aloud) are in
    docs/proposals/2026-10-20-mail-in-sasonica.md. Not started.

36. **Several servers, and settings by server** (David, 20 Oct 2026: "with
    multi-server pairing, there are some settings and sign in features that
    could be managed per server rather than per device. maybe under an
    administrator role?"). The app pairs with several servers (a list of
    pairings, a switcher, notifications from all), and Settings splits into
    This phone, You on <server> (per-person voice, speech default,
    language, alert levels) and <server> for admins (coding agents,
    presets, project roots, personas, bridges, contacts, devices). Needs
    roles step 1 first (docs/proposals/2026-10-18-roles.md). The plan and
    its open questions are in docs/proposals/2026-10-20-several-servers.md.
    **The pairing list and `server_id` are item 7's first step** (built
    20 Oct 2026, branch `multi-base`; item 7's plan lists where it differs
    from the proposal). Its switcher gives way to one merged list with a
    filter per server (David, 20 Oct 2026: "Merged, with a filter"). The
    settings split is not started.

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
37. **Sasonica in Android Auto** (David, 20 Oct 2026: "would this app work
    on Android auto?" — "y"). Today it doesn't show in the car: speech and
    music come through the car's speakers, but the app declares no
    automotive_app_desc, so there's no car entry. Its notices aren't
    MessagingStyle, so Auto never reads them out, and a sideloaded app
    stays hidden unless Auto's developer settings allow Unknown sources.
    First step, the messaging route: declare `<uses name="notification"/>`,
    and post each thread's finished reply or waiting question from
    NotifyService as a MessagingStyle conversation (one per thread, the
    session as the sender). Add a RemoteInput reply that goes to that
    thread the way the in-app composer does (item 10's answer from the
    notification, reused), and a mark-as-read action. Auto then reads it
    aloud and takes a spoken reply. Later, maybe: a MediaBrowserService
    over SpeechSession and MusicSession so replies and rooms can be browsed
    on the car screen. Not started.

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
- Sasonica's speech service starts after a reboot or an update as `specialUse`
  (Android 15 will not start `mediaPlayback` from `BOOT_COMPLETED`), since
  26 Sep 2026 — not yet seen through a real reboot.
- Sasonica's speech takes audio focus (it is the app's only player), so while
  both apps are installed a reply pauses the old app's book through Android
  rather than in process. That is the intended behaviour, but it is the
  arbitration the old app does more carefully, and it has not been heard yet.

[^next-rename]: Sasonica Next became Sasonica (`com.sasonica.app`) on 26 Sep
    2026, and the older Sasonica app became Sasonica ABS (`com.sasonica.abs`).
    Their speech targets followed on 27 Sep: `next` → `sasonica`, `app` → `abs`
    (`MEDIA_SPEECH_*_NEXT` → `*_SASONICA`, `*_APP` → `*_ABS`). The Done list
    keeps the names it was written with; `android-next` is the frozen fork
    branch.
