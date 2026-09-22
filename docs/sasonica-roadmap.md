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

## Done

Pairing and device tokens; threads by session; transcript messages and
per-thread SSE; speech bar with full controls; follow-along; recaps; exit
and archive; idle closer; audio destination picker and Android output
switcher; multi-line sends; per-session stop; multi-select questions; Home
dashboard; background agents in a thread; thread-list sorting and filter
(Active / Live / Closed / Archived / Everything, one project, and any
number of states — Needs you / Working / Your turn; `/targets`
now lists every archived thread, past the 40-row cap; By project's
headings fold, kept per device); project
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
when agent-memory answers; the app's ⌕ on Home and Threads, a hit opens its
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

1. **Typed tools for Sasonica Shell.**
2. **Sasonica Shell OAuth** — the proposal is written, not built.

## Loose ends

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
