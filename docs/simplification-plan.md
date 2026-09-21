# Simplifying agent-media for the Sasonica rebuild (draft, 21 Sep 2026)

Companion to `sasonica-shell/docs/umbrella.md` (the repo was `runlet` until 21 Sep 2026) (Sasonica = app + server + link + shell).
The rebuild puts an assistant-ui front end on agent-media directly and retires
Audiobookshelf. That is the moment to shrink the server side too.

## Where the size actually is

Measured today (non-test Python):

| Where | Lines | Note |
| --- | --- | --- |
| `core` total | 39,750 | |
| `core/cli.py` | 8,201 | 106 subcommands in one file |
| `core/intake/submit.py` | 4,371 | the hook path; every channel's routing |
| `core/call_guard.py` | 1,855 | |
| `core/setup.py` | 1,597 | |
| `core/book_tracks.py` | 1,514 | ABS publishing |
| `visual` | 7,282 | **this is the Sasonica server**: `canvas.py` + `reply.py` carry `/targets`, `/ask`, `/conversation`, `/events` |
| `abs-bridge` | 956 | |
| `snapcast-room` | 301 | already its own package |
| `core/snapcast.py` | 197 | plus ~50 references in routing, setup, CLI |

So Snapcast is small. The weight is the CLI, the intake path, and the
Audiobookshelf coupling.

## 1. Rooms become a plugin destination

Snapcast is not worth extracting for its line count. What is worth it is that
core knows the word `"rooms"`: it is hard-coded in the music `--where` choices,
the speech and book sinks, `route/policy.py`, and the coordinator's duck logic
(`test_coordinator_rooms_duck.py`).

`extensions.py` already defines three seams and says sinks are "not a
third-party seam (yet)". Add a fourth: **destinations**, entry-point group
`agent_media.destinations`, each providing `play / pause / duck / status`.

- `phone` and `local` ship in core.
- `rooms` moves into `snapcast-room` along with `core/snapcast.py` and the
  `am-sinks` / `am-snapfifo@` units from `setup.py`.
- Core routing asks "which destinations are installed and up", never
  `if where == "rooms"`.

Do **not** delete it: Mopidy (both instances) outputs to the am-music Snapcast
sink, `snapcast-sp4-exclusive` routes the house audio, and the music skill
still documents rooms as the output. Uninstalling the package should leave
core working with phone + local.

## 2. The Audiobookshelf exit

Goes with the ABS server: `abs-bridge` (956), most of `book_tracks.py` (1,514),
the ABS parts of `session_feed.py`, `library.py`, `share.py`, the `abs-scan`
command, `SSRF_REQUEST_FILTER_WHITELIST` notes, the progress-duration and
`isFinished` workarounds. Conversations are served from agent-media's own
store instead; this is the largest single deletion.

Keep: the book *channel* (listening to actual audiobooks), which does not
depend on ABS being the conversation store. Decide separately whether books
stay in ABS or move to the same native player.

## 3. Split `cli.py`

8,201 lines, 106 subcommands. Split by channel into a `cli/` package
(`speech`, `music`, `book`, `session`, `doctor`, `setup`), each registering its
own parsers. Mechanical, no behaviour change, and the prerequisite for letting
a destination plugin add its own subcommands (`media rooms …`).

Candidates to drop in the same pass, once the app replaces them: the
tmux-popup family (`popup-status`, `popup-channel`, `now-pane`, `goto-*`,
`open-*`) if the phone app is where status is now read. Check usage before
cutting; David drives some of these from tmux keys.

## 4. Name the server

`visual` started as the canvas and became the app's API. Rename or split:
`visual` keeps the canvas page; a `server` package holds the JSON/SSE API the
app talks to, with the endpoint contract written down. That contract is what
the assistant-ui `ExternalStoreRuntime` adapter binds to:

| App concept | Endpoint |
| --- | --- |
| thread list | `/targets`, `/sessions/state` |
| new thread / rename | `/ask`, `/rename` |
| messages + live updates | `/conversation`, `/events` |
| send / stop | `/input`, `/stop` |
| asks, permissions | `extras.ask` on the message, answered via `/input` |
| speech playback + follow-along | `/speech`, `/speech/now`, `/speech/ctl` |

## Rendering: now and later

- The Capacitor app must ship a static bundle; nothing is server-rendered
  on the phone.
- Build it with **React Router v7 in SPA mode**. Same components can move to
  its SSR mode later for a browser client, without a framework change.
- **Drupal**, if used, owns sasonica.com: public site, docs, and accounts /
  billing for a hosted tier. It stays out of the chat path, which streams live
  from agent-media. A browser client can embed or link to the app; it does not
  proxy messages through PHP.

## Order

1. Write down the server contract (step 4), since the new front end needs it
   first.
2. Split `cli.py` (step 3), mechanical.
3. Destinations seam + move rooms out (step 1).
4. ABS exit (step 2), after the new app reads conversations from agent-media.
