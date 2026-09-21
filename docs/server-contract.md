# The Sasonica server contract (21 Sep 2026)

What the phone app and agent-media say to each other, so the assistant-ui
front end (see `app-redesign-options.md`) can be built against a spec instead
of against `canvas.py`. Step 1 of `simplification-plan.md`.

The document has two halves:

- **v0 — what runs today.** Every route the app calls, as the code answers
  it on 21 Sep 2026. Pinned by `packages/server/tests/test_contract.py`; if a
  shape here and the code disagree, the test is the arbiter and this file is
  the bug.
- **v1 — what the rebuild needs.** Four changes decided with David on
  21 Sep 2026: device tokens instead of the Audiobookshelf login, threads
  keyed by session instead of by library item, a per-thread event stream,
  and stop. **Device tokens (§9) and threads keyed by session (§10) are
  BUILT (22 Sep 2026)** — those sections now give the shapes as
  implemented, and the deviations from the first draft. The stream (§11),
  stop (§12) and error codes (§13) are still specified only.

Then the binding to assistant-ui's `ExternalStoreRuntime`, the gaps, and an
appendix of everything that is *not* part of the app contract.

The proposal for splitting this API out of `visual` into its own package is a
separate document: `proposals/2026-09-21-server-package.md`.

---

## 1. Decisions (21 Sep 2026)

| Question | Decision |
| --- | --- |
| How does the app prove who it is once ABS is gone? | **Paired device tokens** — one per device, revocable, minted through a one-time pairing code (§9). |
| How do messages reach the app live? | **A per-thread SSE stream** (§11). Polling `/conversation/log` stays as the fallback. |
| What does stop do? | **The one thing that is happening** — interrupt the turn while it works (speech said before the stop plays on), or stop this thread's speech when it is only talking; a second press within 5 s silences this thread's speech too. Other threads' speech is never touched (§12). |
| What does this spec cover? | **The app contract in full**, plus the v1 additions. Canvas-only, media-share and speech-state routes are listed in the appendix and are not part of it. |

---

## 2. Servers

Three HTTP servers, all stdlib `http.server`, all on the tailnet.

| Server | Code | Port | Who talks to it |
| --- | --- | --- | --- |
| **canvas** | `packages/visual/src/agent_media_visual/canvas.py` (routes), `reply.py` (conversation logic), `agents.py` (harness setup), `item.py` | 8781 (`MEDIA_VISUAL_PORT`) | Sasonica, sasonica-web, the canvas page, the companion's WebView, completions-shim, OWUI pipe |
| media-share | `packages/core/src/agent_media_core/entrypoints/share_listener.py` | 8771 | the companion app only |
| speech-state | `packages/core/src/agent_media_core/entrypoints/speech_state.py` (red5 actually runs `~/dotfiles/packages/voice/.local/bin/speech-state-server.py`) | 8675 | the phone's call guard, hpo's SMTC ducker |

**The app contract is the canvas.** media-share and speech-state belong to
the companion and the voice plumbing; they are in the appendix.

The canvas binds the Tailscale IP on red5 (the shipped unit passes it), not
loopback — `curl 127.0.0.1:8781` from red5 fails. p8a runs its own canvas on
the same port.

### Base URL

A per-device setting in both clients (`agentMediaBaseUrl()` in Sasonica's
`plugins/localStore.js`; `sasonica.canvasUrl` in sasonica-web). Blank means
"the ABS host, port 8781". v1 replaces this with the address the pairing link
carried (§9).

---

## 3. Conventions (v0)

**Bodies.** Requests and responses are JSON (`Content-Type:
application/json`). POST bodies are capped at 64 KiB — a larger
`Content-Length` is refused with **413** (plain text) before a byte is read.
A body that is not valid JSON is read as `{}` by every app route, so a
malformed body fails on its missing fields, not with a parse error.

**The envelope.** Every app route answers `{"ok": true, ...}` or
`{"ok": false, "error": "<a sentence for a person>", ...}`. Clients must
treat `ok: false` as failure whatever the HTTP status, and show `error` as
written — it is phrased to be put on screen. The status carries the class:

| Status | Meaning |
| --- | --- |
| 200 | done (`ok: true`) |
| 300 | `/ask` only: the words named more than one conversation; pick one (§6.1) |
| 400 | the request is wrong (not a session id, empty text, unknown action) — **and the default for any failure that set no status of its own** (see §15) |
| 401 | the credential was rejected. **Only this.** The app answers a 401 by refreshing its ABS token, and logs out if that fails |
| 403 | signed in, but not allowed (`may_reply` said no) |
| 404 | the thing named is not there (item, session, directory, setup window) |
| 409 | the state moved (the question changed, nothing is being asked, a setup is already running) |
| 410 | a setup window has gone |
| 413 | body too large |
| 422 | a share the pipeline refused |
| 500 | something broke here (a draft could not be written, the log could not be read) |
| 502 | ABS answered with something unexpected, **or** the words were typed but the session never took them (`submitted: false`) |
| 503 | ABS did not answer. **Never 401** — an outage must not log anybody out |
| 504 | an answer was pressed and the dialog is still on screen |

**Headers.** Every JSON answer carries `Cache-Control: no-store`. The app
routes (the `_CORS_PATHS` set, §3.1) also carry
`Access-Control-Allow-Origin: *` and `Access-Control-Expose-Headers:
Content-Encoding` — on refusals too, or a browser shows "network error"
instead of the server's own sentence.

**Compression.** Only `GET /item` compresses (gzip, when `Accept-Encoding`
allows and the body is over 4 KiB). Nothing else does.

**Picture URLs** come back canvas-relative (`/img/<name>`); the client
prefixes its base URL. An absolute URL is another host's spool and is used as
given.

### 3.1 CORS

`OPTIONS` on an app route answers 204 with `Allow-Methods: GET, POST,
OPTIONS`, `Allow-Headers: Authorization, Content-Type`, `Max-Age: 3600`. On
any other path it answers 405. The token routes (`/input`, `/show`, `/ctl`,
`/say`, `/play`) are deliberately absent: their credential is ours, not the
caller's, and CORS is what stops a page someone happens to be visiting from
spending it. The Capacitor app never needed CORS (native HTTP); sasonica-web
does, because it is served from the ABS port.

The app routes: `/conversation`, `/conversation/log`, `/conversations`,
`/targets`, `/item`, `/reply`, `/ask`, `/focus`, `/session/resume`,
`/session/close`, `/session/answer`, `/draft`, `/speech/now`, `/speech/ctl`,
`/sessions/state`, `/commands`, `/rename`, `/harnesses`, `/harnesses/run`,
`/harnesses/screen`, `/harnesses/keys`, `/harnesses/close`, `/share`.

`/pair` (22 Sep 2026, §9) is open cross-origin for **POST and its preflight
only** (`app.CORS_POST_PATHS`; the preflight's `Allow-Methods` is `POST,
OPTIONS`). `GET /pair` is the canvas's amux-token page, and an
`Access-Control-Allow-Origin` on it would let any page's script read the
amux token out of the answer — so it never carries one.

---

## 4. Auth (v0)

Three credentials, and which one a route takes is the route's, not the
caller's, choice.

**Since 22 Sep 2026 every app route takes a fourth, first:** a paired
device token (§9) in the same `Authorization: Bearer` header. It is checked
locally before anything below, in one place (`agent_media_server/auth.py`),
and only a bearer that is not a known device token reaches §4.1. What §4.1
says about ABS is therefore the fallback, unchanged.

### 4.1 The caller's ABS bearer — every app route

`Authorization: Bearer <the token the client already holds for
Audiobookshelf>`. No secret of ours is on the phone.

1. **Who is it?** `reply.abs_identity` hands the bearer to ABS's own
   `POST /api/authorize`, trying each server in `abs_urls()` (the configured
   one, plus `ABS_URLS` in `~/.config/agent-media/abs-bridge.env` or
   `MEDIA_ABS_URLS`) until one recognises it. This list is an allow-list
   that the caller cannot extend — otherwise we would be forwarding their
   login to a host of their choosing. Successes are cached per token for 60 s.
   Refusals are not cached.
2. **May they?** `reply.may_reply`: the ABS root account (unless
   `MEDIA_REPLY_ROOT=0`), or a username listed in `MEDIA_REPLY_USERS`. Admin
   is not enough — managing a library is not the same as being trusted with
   a keyboard.

Failure mapping (`reply._identity_error`):

| ABS said | We answer |
| --- | --- |
| no bearer at all | 401 `{"ok": false, "error": "Audiobookshelf rejected that login"}` |
| 401 from every server | 401, same |
| nothing (unreachable), 502, 503, 504 | 503 `"Audiobookshelf did not answer"` |
| anything else | 502 `"Audiobookshelf answered <n>"` |
| a user, but not allowed | 403 `"<name> is not allowed to reply"` |

Item lookups (`/conversation?item=`, `/conversation/log`, `/reply`,
`/commands?item=`) also use the *caller's* bearer, so ABS enforces its own
library permissions: an item the caller cannot see is an item they cannot
reply to.

### 4.2 The amux token — desk routes

`X-Auth-Token: <token>` or `Authorization: Bearer <token>`, where the token
is `~/.amux/auth_token`. Guards `/input`, `/show`, `/ctl`, `/say`, `/play`.
With no token configured, those routes are closed. `MEDIA_VISUAL_TRUST_TAILNET=1`
drops the check entirely (a testing switch). The only way a browser gets
this token is `GET /pair` (appendix). **It is never an app credential.**

`/focus` accepts either the amux token or an allowed ABS bearer.

### 4.3 `MEDIA_SHARE_TOKEN` — media-share only

`X-Agent-Media-Token`, compared with `hmac.compare_digest`. The canvas never
reads it. It is optional on loopback and required when bound anywhere else —
media-share refuses to start otherwise.

---

## 5. Identifiers

| Name | Shape | Notes |
| --- | --- | --- |
| **session** | a uuid (Claude Code, Codex, pi), or Hermes's `YYYYMMDD_HHMMSS_hex` | `reply._SESSION`. The thread id. Every session-taking route rejects anything else with 400 `"not a session id"` |
| **pane** | tmux `%23`, or herdr `herdr:<id>` | where a live session runs. Display and `/focus` only — never a key; panes are recycled |
| **item** | an ABS library item id — a plain uuid on red5's server (the `li_…` in examples here is illustrative) | **ABS-specific; goes in v1** (§10). Resolved to a session through the item's folder tail and the book-tracks manifest |
| **key** (line) | a reply's dedup key | joins a line to its pictures. `""` on listener lines |
| **key** (approval) | 12 hex chars, sha1 of the dialog text | fingerprints a question so an answer can only land on the question that was read |
| **id** (line) | an integer speech-history row id | only on lines that were spoken; used for `replay-id` |
| **at** (line) | epoch seconds, 3 dp | when the turn was spoken (or typed). **The de facto line identity**: a live line keeps its `at` when it becomes a finished one |

---

## 6. The app contract, v0

Each route: method and path, request, response, errors, and who calls it
today. "Gated" means §4.1: all of its 401/403/502/503 answers apply and are
not repeated.

Client abbreviations: **S** = Sasonica (`~/projects/sasonica`, Nuxt), **W**
= sasonica-web (`~/projects/sasonica-web`, React).

### 6.1 Threads

#### `GET /targets` — gated

Everything a message can be pointed at.

```json
{"ok": true,
 "sessions": [
   {"session": "0f1e…", "title": "Sasonica web", "live": true, "pane": "%42", "recap": null,
    "archived": false},
   {"session": "6c73…", "title": "Sasonica music", "live": false, "pane": null, "at": 1790000000.1,
    "recap": {"text": "We're making the conversation page show what Claude is doing. Next: try it on the phone.",
              "at": 1789807216.618},
    "archived": true}],
 "places": [{"name": "agent-media", "path": "/home/ryer/projects/agent-media", "at": 1790000000.1}]}
```

- `sessions` is `sessions_index()`. Live sessions come first, titled from the
  pane (Claude's terminal title with the spinner stripped; Codex, pi and
  Hermes from their own session files, cut to 60 chars). A live session
  with no title is left out. Then up to 40 shelved conversations, newest
  first, titled by their folder name, with `at` = the manifest's mtime.
  **`at` is only on shelved rows.** A session appears once, live if it
  is live.
- `recap` (every row, 22 Sep 2026): Claude Code's latest "while you were
  away" summary for that session, `{"text", "at"}`, or `null`. See
  [Recaps](#recaps) below. The app uses it as the row's preview line.
- `archived` (every row, 22 Sep 2026): whether the thread is archived — a
  flag this server keeps per session (`archive.py`, `<state_dir>/archived.json`),
  set with `POST /session/archive` (§6.4). **Archived rows stay in the list**;
  the app files them under an "Archived" section itself. Talking to a thread
  (a reply, or an `/ask` routed into it) clears the flag.
- `places`: up to 6 directories sessions have run in, newest first
  (running sessions count as "now"). These are the only directories a new
  chat may be opened in — `/ask` checks against this list (with no limit).

Clients: S (`utils/sasonicaTargets.js`, drawer and ask page), on open.

##### Recaps

When you come back to a Claude Code session after being away, Claude Code
writes a short paragraph of where things stand into the session's
transcript: a `{"type": "system", "subtype": "away_summary", "content":
"… (disable recaps in /config)", "timestamp": "<ISO>"}` line. The server
reads it (`agent_media_server/recaps.py`, read-only) and hands it on as

```json
{"text": "Runlet is now Sasonica Shell, running on red5. Next: add the connector.",
 "at": 1789974112.291}
```

- `text`: the paragraph, with Claude Code's trailing hint stripped (a
  trailing parenthetical that mentions recaps; any other trailing "(…)" is
  kept).
- `at`: epoch seconds, 3 dp, from the line's timestamp.
- `null` when the session has no recap yet, and **always** for Codex, pi and
  Hermes, which write nothing like it. (Later, one could be generated for
  them through the follow-up gateway call, `intake/_followup.py`.)
- It is **not a message** and never becomes a line: nobody said it, and it is
  not part of what the agent sees. The app draws it as a "While you were
  away" card in the thread (§14) and as the thread list's preview line.
- Only the latest is on any route. `recaps.recaps(session, since)` lists
  every one, and is there for when something wants a history of them.

**Cost.** It is on `/targets`, which covers ~44 transcripts of 1–15 MB each.
The latest recap is found by reading each file **backwards** in 256 KB
chunks, parsing only lines that contain `away_summary`. The answer is cached
per file by (inode, size, mtime) along with the offset it was read to, and
transcripts are append-only, so a live session's growing file costs only its
new bytes. A file that shrank, was replaced or was rewritten in place is read
again from scratch. Measured on red5's 44 real transcripts (107 MB, 22 Sep
2026): the body of `/targets` took 0.16–0.24 s on a fresh process's first
call and ~0.13 s warm, both before and after; `latest_recap` over all 44
took 0.058 s cold (page cache warm) and 0.3 ms warm. The one real cost is
the first read of files that are not in the page cache at all (0.5–0.72 s
seen; red5 runs with ~1.5 GB free, so the cache does get evicted). That is
paid once per canvas process: after it, the in-process cache means an
unchanged file is only stat'ed and a growing one only read at its end.

#### `GET /conversations` — gated

`{"ok": true, "sessions": [...]}` — the same rows as `/targets.sessions`.
Clients: W only (`NewChat.tsx`, the picker). Kept separate for historical
reasons; v1 drops it in favour of `/targets`.

#### `GET /sessions/state` — gated

What each live session is doing.

```json
{"ok": true,
 "sessions": [{"session": "0f1e…", "tail": "p-agent-media/Sasonica web", "state": "working",
               "mem_mb": 364}],
 "host": {"mem_total_mb": 7758, "mem_available_mb": 1568, "sessions_mem_mb": 3009}}
```

- `state`: `working` | `waiting` (has answered, waiting on you) |
  `approval` (stopped on a dialog). Read from the pane's screen, not a hook.
- `tail`: the item folder's `<project>/<title>` — **ABS-specific**, there so
  the shelf can match items without asking for each. `""` when the session
  has no shelf entry yet.
- Only live sessions are listed. Absent means not live.
- `mem_mb` (22 Sep 2026): resident memory of the session's agent process
  (the claude / codex / pi / hermes process `live_sessions` found) **and all
  its descendants** — MCP servers, sandboxes — in whole MB (`procmem.py`).
  RSS is summed per process, so shared pages count once per process: the
  same measure `ps` shows, and an overestimate of what closing would free.
  `null` when it cannot be read (the process ended mid-sweep).
- `host` (22 Sep 2026): `mem_total_mb` and `mem_available_mb` from
  `/proc/meminfo` (`null` if missing), and `sessions_mem_mb`, the sum of the
  rows' known `mem_mb` (0 with no rows). What the phone needs to say "ten
  sessions hold 3 GB of 7.7, 1.5 left — close some".
- Cached 3 s server-side (each poll is a /proc sweep and a capture per pane).
  Memory is computed inside the same cached sweep: the pids come from the
  sweep that finds the sessions, and one more pass over `/proc` (each
  process's parent, then `statm` for tree members only) answers every
  session at once. Measured on red5 with 10 live sessions: the sweep
  took ~0.24 s with or without it; the memory pass alone is ~15 ms.

Clients: S (`mixins/sasonicaArchive.js`, `pages/ask.vue`), **polled every 5 s**.

### 6.2 One conversation

#### `GET /conversation?item=<item>` — gated

"Is this item a conversation I can reply to?" — the app asks before it
draws the reply box.

```json
{"ok": true, "session": "6c73…", "live": false, "pane": null,
 "resumable": true, "suggestion": ""}
```

- `resumable`: some harness still has the transcript, so a reply will revive it.
- `suggestion`: the next prompt to offer (§6.2.1).
- 404 `"no such item"` (ABS 404), `"not a conversation (no session behind it)"`,
  `"item has no path"`. ABS failing on the item lookup is a 404 whose
  error names the status ("Audiobookshelf (…) did not answer"), not a 503 —
  see §15.

#### `GET /conversation?session=<session>` — gated

The same question from the other side — a session the phone just started,
and whether the library has an item for it yet.

```json
{"ok": true, "session": "0f1e…", "item": null, "scanning": false,
 "live": true, "pane": "%42", "resumable": true, "suggestion": ""}
```

- `suggestion` (§6.2.1) since 22 Sep 2026 (§10), as `?item=` has it.
- `ok` from the first poll (the session is real). `item` fills in once ABS
  has the item **and has built its tracks**. `scanning: true` means the item
  exists but ABS has no tracks for it yet — don't navigate to it.
- `session` takes precedence only when `item` is absent.

Clients: S (`pages/ask.vue`) and W (`NewChat.tsx`) after an `/ask`, **every
3 s for up to 5 min**. S and W both call `?item=` (`ReplyBox.vue`,
`useConversationSession.ts`) on page open.

#### 6.2.1 The suggestion

`suggestion_for`: the ghost prompt Claude Code draws, dim, on its input
line, scraped from the pane — unless the terminal cut it with "…" (a
28-column phone window does). In that case, the follow-up line the Stop
hook wrote for that same reply (`intake/_followup.py`). A follow-up is only
offered for the reply it was written for. `""` when there is neither, or
while a turn is pending.

#### `GET /conversation/log?item=<item>` · `?session=<session>` — gated

The conversation as lines — the chat itself. The `?session=` form (22 Sep
2026, §10) answers the same envelope with the same line shapes and wins when
both are given; it never asks ABS, so `start`/`end` are always `null` on it.

```json
{"ok": true, "session": "6c73…",
 "lines": [ …line… ],
 "pending": false,
 "working": null,
 "approval": null,
 "suggestion": "",
 "recap": null}
```

**Envelope**

- `pending`: the last line is the listener's, or a turn is running — show
  "thinking" and poll faster.
- `working`: `null`, or what the running turn is doing:
  `{"since": <epoch>, "count": <steps so far>, "current": "<step text>",
  "current_at": <epoch|null>, "steps": ["…", …], "server_time": <epoch>}`.
  `steps` holds the latest few (`MAX_STEPS`), newest last.
- `approval`: `null`, or the dialog the session is stopped on (see below).
- `suggestion`: §6.2.1, `""` while pending.
- `recap` (22 Sep 2026, both forms): the session's latest Claude Code
  recap, `{"text", "at"}`, or `null` — the same object as on the session's
  `/targets` row ([Recaps](#recaps)). **Not a line**, and never inserted
  among them. Only the latest, not every recap since the first line: the
  app shows one card, and listing them all would add a list to every poll
  for no reader. Its `at` says where it falls among the lines if the app
  wants to place the card rather than pin it to the top.

**A line**

| Field | Always | Meaning |
| --- | --- | --- |
| `who` | yes | `"you"` (the listener) or `"agent"` |
| `text` | yes | the words. For an ask, the question(s) alone |
| `at` | yes | epoch seconds, 3 dp. The line's identity |
| `key` | yes | the reply's dedup key, `""` for the listener |
| `start`, `end` | yes | seconds into the ABS audio item, or `null` until the next publish places it — **ABS-specific** |
| `id` | spoken lines | speech-history row id, for `replay-id` |
| `ask` | asks | `[{"question", "options": [{"label", "description"}], "multiSelect"}]` — an AskUserQuestion, as asked |
| `command` | slash commands | `{"name": "review", "args": "123", "text": "/review 123"}` — the chip for a slash command typed from the box (`agent_media_core/slash.py`); `text` on the line is `command.text`. Settings commands (`/model`, …) never become lines |
| `images` | when drawn | `["/img/…", …]` — the pictures the canvas drew for this reply, still in the spool |
| `figure` | with `images` | `true` for a `[[visual:]]` figure, `false` for ambient art |
| `work` | agent lines | `{"seconds", "count", "steps": [...]}` — what the turn did before this reply |

**The live line.** At most one line — the one being spoken now, or a
replayed one, marked in its place — also carries:

| Field | Meaning |
| --- | --- |
| `live` | `true` |
| `sentences` | the reply split into sentences, as spoken |
| `sentence` | index of the sentence playing, or `null` |
| `offsets` | seconds from the start of the reply at which each sentence begins |
| `elapsed` | seconds since the reply started, **as of `server_time`** |
| `server_time` | epoch when `elapsed` was read — the route re-reads the clock just before sending |
| `delay` | playout delay to subtract (bridge hop, far player) — `0` when offsets were measured by the player |
| `paused` | `true` while paused (then `elapsed` is frozen) |

(`conversation_log`'s own dict also has `measured`, `target` and
`history_id`, but they stay on the server — the line only takes the fields
above.)

**Follow-along:** bold the sentence whose offset is the last one ≤ the
reply's current position, advancing on a local clock between polls and not
advancing while `paused`. Compute the position **from the phone's own clock
only**:

```
position = elapsed + (now − received_at) + rtt / 2 + lead − delay
```

- `received_at` is when the phone got the answer and `rtt` is that request's
  round trip, both measured on the phone.
- `lead` is ~0.3 s, so the bold arrives with the voice rather than after it.
  This is what the Nuxt app does.

Do **not** use `now − server_time`. That mixes the phone's clock with the
server's, and any skew between them moves the bold by exactly that much.
`server_time` is there so the *server* can age `elapsed` up to the moment
it sends the answer, which it already does.

**Line order and identity.** Shelved turns (in manifest order), then the
live tail (turns spoken but not yet published, by `at`), then the live
line. When a live line ends, it becomes an ordinary line **with the same
`at`**. Replace by `at`, never append twice. Listener turns that were
really harness asides (task notifications, system reminders) are filtered
out.

**Errors:** as `/conversation?item=`, plus 404 `"no manifest for that
conversation"` and 500 `"could not read the conversation (…)"` (logged).
The `?session=` form: 400 `"not a session id"`, and 404 `"no conversation
for that session yet"` only for a session with no manifest, nothing said, no
pane and no transcript (§10).

Clients: S (`ConversationLog.vue`), W (`useConversationLog.ts`). **Adaptive
poll, by `setTimeout`:** 1 s while a line is live or just after, 2 s while
`working` or `approval` (S), 15 s idle. S holds auto-scroll for 8 s after a
manual scroll.

##### `approval`

```json
{"question": "Do you want to proceed?",
 "partial": false,
 "options": [{"n": 1, "label": "Yes", "detail": ""},
             {"n": 2, "label": "Yes, and don't ask again", "detail": ""},
             {"n": 3, "label": "No", "detail": ""}],
 "key": "3f9a1c0b7e22",
 "agent": "claude"}
```

This is read off the screen (`parse_dialog`): Claude Code's permission
prompt, its AskUserQuestion modal, Codex's command approval and hooks review
all draw the same numbered list. The fields:
- `detail` is the indented text under an option: the rest of a wrapped
  label, or a description.
- `partial` is true when the list scrolled and some numbers are missing.
- `question` is `""` when the top of the dialog is off screen. The numbers
  still answer it.

Answer with `POST /session/answer`.

#### `GET /commands` — gated

The slash menu for the reply box. It belongs to a directory, so the query
names one of: `item`, `session`, `project` (a series name) or `cwd` (a
`place` from `/targets`; anything else is ignored). With none of them, the
home directory.

```json
{"ok": true, "cwd": "/home/ryer/projects/agent-media",
 "commands": [{"name": "review", "description": "Review a PR", "aliases": []}]}
```

Built from `claude -p --output-format stream-json`'s init event and cached
per directory until Claude Code's version changes (`slash_menu.menu`). 404
when an `item` resolves to no session.

Clients: S (`mixins/sasonicaSlash.js`).

#### `GET /draft?session=` · `POST /draft` — gated

Half a reply, held server-side so it survives switching conversations and
reinstalls. Keyed by session, not by user.

- POST `{"session", "text", "at"?}`: `at` is **the writer's clock**, stored
  as given (it is compared with the client's own offline copy). Empty or
  whitespace-only text deletes the draft. Text is capped at 8192 chars.
- Both answer `{"ok": true, "session", "text", "at"}`; a missing draft is
  `{"text": "", "at": 0}`.
- 400 for a bad session id; 500 when it cannot be written.

Clients: S (`ReplyBox.vue`), POST debounced 800 ms.

### 6.3 Sending

#### `POST /reply` — gated

Type into the session behind a conversation, reviving it if it has ended.

Request: `{"session": "<session>" | "item": "<item>", "text": "…", "quote"?: "…", "mode"?: "continue" | "branch"}`

- `session` (22 Sep 2026, §10) wins when both are given. 400 `"not a
  session id"`; 404 `"no such session <8 chars>"` when it has no pane and
  no transcript. `branch` works from either form.

- The text is flattened to one line before it is typed (a newline would
  submit half the message). The quote rides in front as `Re: "<quote, ≤160
  chars>" — <text>`. The **unflattened** text is recorded as the listener's
  turn.
- `continue` (default): into the live pane. If the session has ended, it is
  resumed in a background tmux window first. This can take a minute
  (Claude resumes from a summary, which compacts first) — hence `opened`.
- `branch`: a **fresh** session in the same directory, seeded with the
  quote — the nearest thing to forking a conversation.
- After typing, the server watches the composer and presses Enter again
  until the line leaves the box (`_ensure_submitted`, ~3 s).

Response:

```json
{"ok": true, "session": "6c73…", "pane": "%42", "opened": false, "submitted": true}
```

`branch` adds `"branched": true`. `opened: true` means a window was
revived. Show "opening" rather than "sent".

Errors: 400 `"empty reply"`. 404 when the item is not a conversation.
**502 with `submitted: false` and `pane`**: the words are in the composer
but were never taken — say so, don't show a typing indicator forever.
Failures with no status of their own, which answer 400 (§15):
- `"session … has no transcript to resume"`
- `"no attached tmux session to open a window in"`
- `"%N did not come up within 45s"`
- `"session … is already running outside tmux (pid N)"`

Clients: S (`ReplyBox.vue`), W (`ReplyBox.tsx`).

#### `POST /ask` — gated

The assistant button, and "new chat": words that may or may not name where
they go.

Request:

| Field | Meaning |
| --- | --- |
| `text` | required |
| `target` | a session id the picker chose, or `"new"` to force a fresh session |
| `player_session` | the thread in the player, by session id (22 Sep 2026, §10). Wins over `player_item`; 400 if it is not a session id; a session that is gone (no pane, no transcript) is skipped, not an error |
| `player_item` | the ABS item in the player — **ABS-specific** |
| `sticky` | the session this device last spoke to (S keeps it in `sasonica.askLast`) |
| `parse` | default `true`: read a target from the words ("reply to drones, …", "new codex chat, …") |
| `dry` | `true`: say where it would go, send nothing |
| `agent` | `claude` \| `codex` \| `pi` \| `hermes` for a fresh session (default `MEDIA_ASK_AGENT`, else claude) |
| `project` | open a fresh session in that series' directory |
| `cwd` | open a fresh session in that directory — must be a `/targets` place |

Routing, first match wins:
1. `target`: a session id (`how: "picked"`), or `"new"` (`how: "asked"`).
2. A target spoken at the start of the words (`how: "spoken"`).
3. The player's thread — `player_session`, else `player_item` (`how: "player"`).
4. `sticky`, if it still exists (`how: "sticky"`).
5. A fresh session (`how: "default"`), in `cwd`, `project`, or the scratch
   amux registration (`MEDIA_ASK_SESSION`).

Responses (`mode` tells them apart):

```jsonc
// dry run
{"ok": true, "dry": true, "mode": "new" | "continued", "how": "…",
 "agent": "claude",               // only when mode is "new"
 "session": null | "…", "title": "", "item": null | "li_…", "text": "…"}

// a spoken name and nothing else: switch to it, send nothing
{"ok": true, "mode": "switched", "how": "spoken", "session": "…", "title": "…",
 "pane": "%42" | null, "item": null | "li_…", "text": ""}

// sent to a fresh session
{"ok": true, "mode": "new", "how": "…", "session": "…" | null, "pane": "%51",
 "opened": true, "fresh": true, "tmux": "amux-scratch", "agent": "claude",
 "submitted": true, "title": "", "text": "…"}

// sent to an existing session
{"ok": true, "mode": "continued", "how": "…", "session": "…", "pane": "%42",
 "opened": false, "submitted": true, "title": "…", "item": null | "li_…", "text": "…"}
```

`session` can be `null` on a fresh Claude session whose id had not
registered within 10 s. The words were still sent.

Errors:
- 400 `"empty message"`.
- **300** `{"ok": false, "error": "which conversation?", "ambiguous": [<sessions
  rows>], "text": "<the rest of the words>"}`. Nothing was sent; show the
  candidates and resend with `target`.
- 404 when `cwd` or `project` is unknown.
- 400 for an unknown agent.
- 502 when the words were not taken.
- 400 for window failures, as `/reply`.

**Client pattern** (both S and W): anything the server would be *guessing*
is asked with `dry: true` first. A fresh session commits straight away; a
guessed thread gets a 4-second confirm naming it; an ambiguous name becomes
the picker. After a fresh send, poll `/conversation?session=` for the item.

Clients: S (`pages/ask.vue`), W (`NewChat.tsx`).

### 6.4 Managing a thread

#### `POST /rename` — gated

`{"item"? , "session"?, "title"}` → `{"ok": true, "session", "title",
"terminal": bool, "why": null | "…"}`.

The name is kept by agent-media (`book_tracks.rename`) and typed into the
running session as `/rename <title>`. `terminal: false` with a `why`
(ended, or someone is mid-sentence in its box) is **not** a failure: the
shelf has the name, and the next session starts with it. 400 `"no title"`;
500 `"could not rename"`.

Clients: S (`ReplyBox.vue`).

#### `POST /session/resume` · `POST /session/close` — gated

`{"session"}`.

- resume, already live: `{"ok": true, "session", "pane", "live": true, "opened": false}`
- resume, revived: same with `"opened": true`. 404 when there is no transcript.
- close, not live: `{"ok": true, "session", "live": false, "closed": false}`
- close, closed: `{"ok": true, "session", "pane", "live": false, "closed": true}`.
  Only the pane hosting that very session is killed; the transcript stays,
  so close is undone by resume.

Clients: S (`ReplyBox.vue`), W (`useConversationSession.ts`).

#### `POST /session/archive` — gated (22 Sep 2026)

`{"session", "archived": true | false}` → `{"ok": true, "session", "archived"}`.

- File a thread under Archived, or take it back out. The flag is kept here,
  per session, in `<state_dir>/archived.json` (`{"<session>": <archived at>}`,
  written atomically under a thread lock and an flock) — not an ABS tag.
- `archived` defaults to `true` when absent; anything but a JSON boolean is
  400 `"archived must be true or false"`. Setting what is already set is a
  200 that writes nothing.
- **Archiving ends nothing.** A live session stays live. "End & archive" is
  two requests: `/session/close`, then this.
- **Talking un-archives.** A reply, or an `/ask` routed into the thread,
  clears the flag once the words are in (`send.deliver`); a send that
  failed leaves it. A `branch` reply does not: it opens a new thread.
- The row stays in `/targets` and `/conversations` with `archived: true`.
- 400 `"not a session id"`; 404 `"no such session <first 8>"` for a session
  that is not live, has no transcript and is not on the shelf.
- In `CORS_PATHS`.

Pinned by `packages/server/tests/test_archive_and_memory.py`.

#### `POST /session/answer` — gated

`{"session", "choice": <n>, "key": "<approval.key>"}`. Presses the number
and Enter — never text — and only while that same dialog is on screen.

- 200 `{"ok": true, "session", "pane", "answered": n, "label", "waiting":
  bool, "approval": <the next dialog> | null}` — `waiting` means another
  dialog followed.
- 400 `"not a session id"` / `"no option n"` (with `approval`).
- 404 not live.
- 409 `"that session is not waiting on a question"`.
- 409 `"the question has changed"` with the current `approval` — re-render
  and let the person choose again.
- 504 `"the question is still on screen"` — the keys went nowhere.

Clients: S (`ConversationLog.vue`).

#### `POST /focus` — amux token or gated

`{"pane": "%23"}` → pulls the desk's attached tmux client to that pane (or
focuses the herdr pane). Only panes hosting an agent are eligible. Answers
`{"ok": bool, "detail": "<pane or why not>"}`, 200 or 400. A refusal is
**401 `{"error": "unauthorized"}` with no `ok`** (§15).

Clients: S, W — the "opened in %23" link.

### 6.5 Speech

#### `GET /speech/now` — gated

The speech bar.

```json
{"ok": true, "live": true, "speaking": true, "paused": false,
 "sentence": "…", "session": "6c73…" | null, "title": "…", "item": "li_…" | null,
 "pos": 12.0, "dur": 40.0, "speed": 1.6, "muted": false}
```

- `live` = speaking or paused.
- `session`, `title` and `item` are filled only while live and only for a
  valid session id. Title and item are cached per session for 60 s.
- `pos`, `dur` and `speed` may be `null`.

Clients: S (`SpeechBar.vue`). **Polled 1.5 s while speaking, 5 s idle, 15 s
after failures.** The server logs one line per device on the first answer
and on every refusal.

#### `POST /speech/ctl` — gated

`{"action", "arg"?}` → `{"ok": true, "out": "<what media printed>"}`.

Actions (`_APP_SPEECH_ACTIONS`): `toggle`, `skip-`, `skip+`, `para-`,
`para+`, `jump-end`, `prev`, `replay`, `replay-id`, `speed-`, `speed+`,
`speed0`, `vol-`, `vol+`, `mute`.
- `arg` is the turn index for `prev` and `replay` (1 = latest, clamped
  1–999), or a history row id for `replay-id` (not clamped).
- Anything else is 400 `"unknown action"`.
- `ok: true` means the command ran, not that it did anything — read `out`.

Clients: S (`SpeechBar.vue`; `replay-id` from `ConversationLog.vue`).

### 6.6 Harness setup — gated

Getting an agent onto the host and signed in, from the phone.

| Route | Request | Response |
| --- | --- | --- |
| `GET /harnesses` | – | `{"ok", "agents": [{"name", "present", "path", "version", "auth": "in"\|"out"\|"unknown", "account", "actions": ["install", "login"], "installed_action": "install"\|"update"}]}` |
| `POST /harnesses/run` | `{"agent", "action": "install"\|"login"}` | `{"ok", "pane", "agent", "action", "cmd"}` · 400 unknown agent/action, 409 already running, 503 no window |
| `GET /harnesses/screen?pane=` | – | `{"ok", "pane", "agent", "action", "cmd", "lines": [...], "done": bool, "exit": int\|null}` · 404 not ours, 410 gone |
| `POST /harnesses/keys` | `{"pane", "text"?, "key"?}` | `{"ok", "pane"}` · 400 bad key / nothing to type, 404, 410 |
| `POST /harnesses/close` | `{"pane"}` | `{"ok", "pane"}` · 404 |

Only windows `/harnesses/run` opened can be read or typed into.

Clients: S (`utils/sasonicaAgents.js`, `AgentSetup.vue`), screen **polled 1.5 s**.

### 6.7 Share — gated

`POST /share {"text", "channel"?: "music" | "book"}` → `{"ok", "url",
"channel", "title", "line"}`. It is media-share's pipeline, called with the
ABS bearer. The verdict comes back for the toast while the fetch runs
behind it. 400 `"nothing shared"`; 422 when the share pipeline refuses.

Clients: S (`SasonicaShareActivity.kt`, the share sheet).

### 6.8 ABS-only — not part of the contract

`GET /item?id=` — the ABS library item trimmed to what Sasonica's item
page reads, gzipped (1267 KB → 25 KB). It exists to get the payload across
the WebView bridge. W does not use it. It leaves with ABS.

---

## 7. `/events` (v0) — canvas-wide, not the app's stream

One SSE stream for every screen. The canvas page, the wake watcher and the
smoke test read it; **neither app does**. It is documented because v1's
per-thread stream copies its conventions.

- `GET /events`, no auth. 503 `"too many canvas clients"` over the cap.
- The stream opens with `retry: 2000`, then a `hello` frame. Then it replays
  the last `show`, `state` and `video` frames.
- Every frame is an unnamed `data: <json>` message, told apart by `kind`
  (the `show` frame has no `kind`).
- If nothing is sent for 15 s, a real `{"kind": "ping"}` data frame goes
  out — not an SSE comment, because `onmessage` has to fire for the
  client's watchdog (which reconnects after 45 s of silence).

| Frame | Payload |
| --- | --- |
| `hello` | `{"kind": "hello", "page": "<12-hex digest of the page>"}` — a client holding a different page reloads |
| `state` | `{"kind": "state", "speaking", "paused"?, "pos"?, "dur"?, "sentence"?, "visual"?, "speed"?, "muted"?, "session"?, "lines"?: [...], "lidx"?, "wake"?}` — ~1 Hz while speaking, on change otherwise |
| show | `{"image": "/img/…"}` or `{"sequence": [{"image", "at": 0..1}], "estdur", "gen_secs"}`, plus `caption`, `prompt`, `purpose`, `session`, `t`, `wake`? |
| `video` | `{"kind": "video", "vid": null \| "<yt id>", "chan", "ts"?, "t"?, "paused"?, "rate"?}` |
| `ping` | `{"kind": "ping"}` |

---

## 8. What v1 changes, in one table

| | v0 | v1 | Status |
| --- | --- | --- | --- |
| Credential | the caller's ABS bearer, checked with ABS | a device token, checked locally (§9) | **built 22 Sep 2026** |
| Thread id | ABS item id on half the routes | session id everywhere (§10) | **built 22 Sep 2026** |
| Live updates | poll `/conversation/log` 1–15 s | `GET /threads/{session}/events` (§11) | specified |
| Stop | none | `POST /session/stop` (§12) | specified |
| Errors | `ok` + `error`, status as §3 | the same, plus a machine `code`; every error has `ok` (§13) | specified (`/pair` already answers with `code`) |

The v0 routes keep working through the migration. v1 adds; it removes
nothing until the ABS exit (plan step 2).

---

## 9. v1: device tokens — BUILT 22 Sep 2026

Code: `packages/server/src/agent_media_server/devices.py` (the store, the
codes, the CLI's listing), `auth.py` (the gate), `app.py` (`POST /pair`).
Pinned by `packages/server/tests/test_devices.py`.

### Pairing

1. At the desk: `media-visual-canvas pair --device "Pixel 8a"` (the existing
   command, gaining `--device`; without it, the amux link is unchanged). It
   mints a one-time 8-hex code, valid for `MEDIA_DEVICE_PAIR_TTL` seconds
   (default: `MEDIA_VISUAL_PAIR_TTL`, else 1800 — the same 30 min as
   `PAIR_TTL_S`), and prints a QR of the app link plus both links:
   - `sasonica://pair?server=<base url, percent-encoded>&code=<code>` — what
     the app reads;
   - `http://<host>:<port>/pair?c=<code>&device=1` — for the person at the
     terminal only. Opened in a browser it is **refused** (it reaches
     `GET /pair`, the amux page, which does not know device codes).

   `--host` and `--port` set the base (default: this machine's hostname and
   `MEDIA_VISUAL_PORT`/8781), as they do for the amux link.
2. The app redeems it:

   `POST /pair {"code": "…", "device": "Pixel 8a"}` — no auth.

   ```json
   {"ok": true, "token": "<43 chars: secrets.token_urlsafe(32)>",
    "device_id": "d_7f3a09c1b2e4", "server": {"name": "red5", "base": "http://red5:8781"}}
   ```

   - `server.name` is the host's `gethostname()`. `server.base` is the
     address the request came in on: `Host` as the client sent it, and
     `https` when a TLS-terminating proxy in front says so in
     `X-Forwarded-Proto` (the Cloudflare link), else `http`.
   - 403 `{"ok": false, "code": "bad_pairing_code", "error": "invalid or
     expired pairing code"}` for a wrong, used or expired code — the same
     answer for all three.
   - 429 `{"ok": false, "code": "rate_limited", "error": "too many pairing
     attempts; try again in a few minutes"}` after 10 failures from one
     source address in 10 minutes. It is answered before the code is looked
     at, so a good code sent while limited is not burned.
   - **A code dies on its first *successful* use, or at the end of its
     window — never on a failure.** Failures are what the rate limit is
     for. (The first draft burned the code on any attempt; that would let
     anyone on the tailnet cancel a pairing by guessing badly.)
   - The name stored is the one given at the desk. The body's `device` is
     used only when the desk gave none — the person with the shell decides
     what a device is called, not the device.
3. The app stores the token in the Android keystore (not WebView
   localStorage), and the base URL from the pairing link becomes its server
   address — replacing the "ABS host on 8781" guess.

`POST /pair` hands out a *device token*. It never returns the amux token.
The device codes live in their own store (`device-pair-codes.json` in the
state dir); the canvas's `GET /pair` code is the spool's `pair-code`. Neither
route reads the other's, so a device code never opens the amux page and the
amux code never redeems a device token (both pinned). `GET /pair` stays for
the canvas page only, is not part of the app contract, and carries no CORS
headers (§3.1).

### Use

`Authorization: Bearer <device token>` on every app route, exactly where
the ABS bearer goes today. The server looks the token up locally — a sha256
and a constant-time compare against each stored hash — with no network
call, so there is no "ABS did not answer" and **auth never produces a 503**
for a device.

The single choke point is `agent_media_server/auth.py`:
`gate(bearer)`, `may_control_speech(bearer)`, `identity(bearer)` and
`may_reply(user)`. Every route that asked `auth_abs` directly now asks
these. Each tries the device store first and falls through to `auth_abs`
unchanged. A device stands for the owner, `{"username": "device:<name>",
"type": "root", "device": "<id>"}`. `may_reply` admits it whatever
`MEDIA_REPLY_ROOT` says: that switch is about who ABS's root is.

**A device token is never sent to ABS.** Item lookups made for a device
caller (`?item=`, the `item` field of `/conversation?session=`, `/ask` and
`/speech/now`, and `GET /item`) go out under the host's own ABS login
(`auth.abs_bearer`), since a device is the owner. With no ABS configured
they find nothing, and the item fields stay `null`.

### Storage and revocation

- `<state dir>/devices.json` (`~/.local/state/agent-media/` on red5), mode
  600, written atomically (a temp file created 600, then renamed):
  `[{"id": "d_<12 hex>", "name", "sha256", "created", "last_seen",
  "last_ip"}]`. Only the hash is stored. `last_seen` and `last_ip` are
  updated at most once a minute. The pairing codes file is mode 600 too.
- `media-visual-canvas devices` lists them (id, name, when paired, last
  seen, from where); `devices --revoke <id>` removes one.
- **A revoked token is simply unknown**, so it falls through to the ABS
  check like any other bearer, and ABS refuses it: 401 `{"ok": false,
  "error": "Audiobookshelf rejected that login"}`, as in §4.1. It does
  **not** carry `code: "bad_token"` yet. That arrives with §13's codes,
  which this pass did not add to the shared envelope, and `test_contract`'s
  key sets are unchanged. It is also not a 503 unless ABS is down, in which
  case the §4.1 mapping applies to it as to any unknown bearer.
- A device token carries the rights `may_reply` grants the owner. There is
  a single scope in v1. Scopes arrive with the hosted tier, if it needs
  them.

### Migration

The gate is "a known device token, **or** an ABS bearer that passes §4.1".
The device token is checked first, since it is local and cheap. The ABS
branch (the fallback lines in `auth.py`, and `auth_abs.py`) is deleted at
the ABS exit. Both clients keep working throughout.

Over the Cloudflare link, device tokens must travel over https only. The
tunnel terminates TLS; the canvas itself stays plain http on the tailnet.

### Deviations from the 21 Sep draft

- A failed redemption does not burn the code (above). Failures are
  rate-limited instead: 10 per source address per 10 minutes, then 429
  `rate_limited`.
- A revoked or unknown device token answers the ABS fallback's 401, with no
  `code: "bad_token"` (above).
- The pairing window has its own knob, `MEDIA_DEVICE_PAIR_TTL`, which
  defaults to the canvas's.
- The browser link carries `&device=1` and is for reference only. There is
  no browser flow for device pairing.
- The desk's name wins over the device's own `device` field.
- Device callers' ABS item lookups use the host's ABS login rather than
  failing, so the `item` fields keep filling until the ABS exit.

---

## 10. v1: threads keyed by session — BUILT 22 Sep 2026

Every route that takes `item` has a `session` form, and the session form
is the contract. Pinned by `packages/server/tests/test_session_keys.py`
(and `test_contract.py`'s `/conversation?session=` key set, which gained
`suggestion` on purpose).

| v0 | v1 | As built |
| --- | --- | --- |
| `GET /conversation?item=` | `GET /conversation?session=` — gains `suggestion` | built: `suggestion` as in §6.2.1. `session` is still read only when `item` is absent (the v0 rule, kept: the two forms answer different shapes) |
| `GET /conversation/log?item=` | `GET /conversation/log?session=` | built: same envelope and line keys; `session` wins when both are given |
| `POST /reply {item, …}` | `POST /reply {session, …}` | built: `session` wins; `branch` works from it |
| `GET /commands?item=` | `GET /commands?session=` | already there (`test_commands_shape`) |
| `POST /rename {item}` | `POST /rename {session}` | already there (`test_rename_shape`) |
| `POST /ask {player_item}` | `POST /ask {player_session}` | built: routes as `how: "player"`, wins over `player_item` |

Every session-taking form rejects a malformed id with 400 `"not a session
id"` (the `_SESSION` pattern, §5).

**`/conversation/log?session=` never asks ABS.** It finds the manifest by
session in the book-tracks dir and calls `book_tracks.conversation_log(…,
positions=False)`. `positions=False` is new: it skips `_abs_ready` entirely,
so `start`/`end` are `null` on every line of this form, even while ABS is
up. They are ABS-shaped and go at the exit anyway, and the point of this
form is to answer when ABS is slow or gone. (The item form still asks for
positions, as before.)

**No manifest yet is not, by itself, a 404.** A session gets its manifest on
its first publish, which is debounced, so a session the phone started
seconds ago has none. But `conversation_log` reads the manifest *by
session* and needs the folder only for positions. The listener's turn is
already in speech history, and the live line comes from the player. So it
is asked regardless, and:
- lines, or a live pane, or a transcript → 200 (possibly `"lines": []` for
  a real session that has said nothing yet — so a client polling a thread
  it just opened sees an empty chat, not an error);
- no manifest **and** no lines **and** no pane **and** no transcript → 404
  `"no conversation for that session yet"`.

**`POST /reply {session}`** checks the session before typing: 404 `"no such
session <first 8>"` when it has no pane and no transcript. `deliver` would
refuse it too, but `branch` would otherwise open a fresh session in no
particular directory.

**`POST /ask {player_session}`** only counts when the session is real (live
or has a transcript). One that is gone is skipped, and routing falls
through to `sticky`, then a fresh session, as a `player_item` with no
session behind it does.

The ABS-shaped fields go with the ABS exit:
- `start`/`end` on lines;
- `item` and `scanning` in `/conversation?session=`, `/ask` and `/speech/now`;
- `tail` in `/sessions/state`;
- `GET /item`.

Until then they stay, and are `null` or `""` where nothing fills them.

### Deviations from the 21 Sep draft

- `start`/`end` are always `null` on `/conversation/log?session=`, not
  only "where nothing fills them": this form never asks ABS for positions.
- A session with no manifest answers from history (200) rather than 404,
  and the 404's words are `"no conversation for that session yet"`.
- When both are sent, `session` wins over `item` on `/conversation/log`
  and `/reply`, and `player_session` over `player_item` on `/ask`.
  `/conversation` keeps its v0 rule (`session` only when `item` is absent).

---

## 11. v1: the per-thread stream

```
GET /threads/{session}/events
Authorization: Bearer <device token>
Accept: text/event-stream
```

**Auth over SSE.** The browser's `EventSource` cannot set headers, so
clients use a fetch-based reader that can (e.g.
`@microsoft/fetch-event-source`; Capacitor's WebView supports streamed
fetch). `?access_token=` is accepted as a fallback for a plain
`EventSource`. The server must never log that query string.

**Frames.** Named events (`event: <type>`), JSON `data`, and a
per-connection increasing `id`.

| Event | Data | When |
| --- | --- | --- |
| `snapshot` | the whole `/conversation/log` envelope plus `{"state", "live", "pane", "resumable"}` | first frame on every connection |
| `line` | `{"op": "append" \| "replace", "line": <line>}` | a line appears, or an existing one (matched by `at`) changes: ends, gains pictures or `work`, is placed |
| `live` | `{"at", "sentence", "elapsed", "server_time", "paused", "delay"}` | the live line's sentence, pause or clock changes — **not** at 1 Hz; the client runs the clock between frames |
| `working` | the `working` object, or `null` | a step starts, or the turn ends |
| `approval` | the `approval` object, or `null` | a dialog appears, changes or goes |
| `suggestion` | `{"text": "…"}` | the ghost or follow-up arrives or clears |
| `state` | `{"state": "working" \| "waiting" \| "approval" \| "ended", "live": bool, "pane"}` | the session changes state; `ended` when its pane goes |
| `ping` | `{}` | 15 s of silence |

`isRunning` is `state == "working"` or `pending` from the snapshot
(§14).

**Reconnection.** The server keeps no per-client history. On every
(re)connect it sends a fresh `snapshot`, and `Last-Event-ID` is accepted
but ignored. A client replaces its thread state with each snapshot. This
is deliberate: the full log is small, and a missed-event protocol is
more to get wrong than it saves.

**Server side.** One watcher per *subscribed session* (not per
connection), fanning out to that session's subscribers. It re-reads at 1 s
while a line is live or a turn is working, and at 3 s otherwise. It diffs
against the last snapshot and emits the smallest events that turn one
into the other. The watcher stops when its last subscriber leaves. Cap: 8
subscribers per session, 32 streams in total; over the cap is 503, as on
`/events`.

**Errors before the stream opens:** 401/403 (auth), 400 not a session id,
404 no such session (no transcript and not live). After it opens, errors
are not reported in-stream — the connection closes, and the reconnect's
`snapshot` (or its 404) says what happened.

**The thread list** stays polled (`/sessions/state` at 5 s, `/targets` on
open). A list stream is left for later: it would be a second watcher over
every pane, for a list that changes slowly.

---

## 12. v1: stop

```
POST /session/stop
{"session": "…", "speech": "auto" | "silence"}      // default "auto"
```

**Stop does one thing: the thing that is happening.** It never interrupts
the turn and silences speech in the same press. Pressing it is always safe:
it is idempotent and never errors just because there was nothing to stop.

With `"speech": "auto"` (the default, which `onCancel` sends):

| When pressed | Stop does | Speech |
| --- | --- | --- |
| the pane is **working** | **interrupt the turn**: Escape, then up to 3 s watching the pane leave `working`, the way `/session/answer` verifies | **left alone.** What is playing or queued was said *before* the stop landed, so it is still true and still worth hearing. Only this thread's speech submitted **after** the stop is dropped (the cutoff, below) |
| not working, and **this thread's speech** is playing or paused | **stop the speech**: the current clip (`SinkSpeech().stop`, what `media stop` does) and this thread's queue | stopped |
| neither | nothing | untouched |

`"speech": "silence"` means "also stop talking" — whatever the turn is
doing, this thread's speech is stopped and its queue dropped. The app sends
it on **a second press of stop within 5 s of the first**. The first press is
gentle, and the second is the explicit one, so speech is never lost by
accident. The speech bar's own pause and stop stay separate from this.

Rules that hold in every case:

- Speech from **another thread** is never touched.
- When the pane is **waiting on a dialog**, nothing is pressed. Escape on a
  permission prompt means "no", and that decision belongs to
  `/session/answer`.
- Escape is the interrupt for Claude Code and Codex. For pi and Hermes it is
  TBD; they answer `interrupted: false, "why": "not supported for pi"`.

**The cutoff.** A per-session marker: any reply from this session
**submitted after** the stop time is skipped at the last checkpoint before
it would play. It still writes its history row, marked flushed, so the
transcript keeps the words. The marker lets through what came before it —
the reverse of the supersede marker, which drops what came before. Its
mechanism is the model to reuse: the same file-per-session marker and the
same checkpoint in `intake/submit.py`. The marker expires once the session
next submits a turn from the listener, so the next exchange speaks
normally. Core work; `request_speech_flush()` (global) is not it.

"This thread's queue" (the idle case and `silence`) is the same
per-session marker, set with the stop time *and* covering replies already
queued: every reply from this session not yet playing is skipped. So the
core change is one marker with two modes: `after` (the cutoff) and `all`.

Response:

```json
{"ok": true, "session": "…",
 "did": "interrupted" | "silenced" | "both" | "nothing",
 "interrupted": true, "why": null,
 "speech": "left" | "stopped" | "idle" | "other_thread",
 "cutoff": 1790000000.123,
 "state": "waiting"}
```

- `did` is the one-word summary the app can put in a toast.
- `why` explains `interrupted: false` (`"not working"`, `"waiting on a
  question"`, `"not live"`, `"not supported for pi"`).
- `cutoff` is the stop time, when one was set.
- `state` is the pane's state afterwards.
- `both` appears only for `silence`.

Errors: 400 not a session id; 401/403 auth; 504 `"still working after
Escape"`, since that means the keys went nowhere.

---

## 13. v1: error envelope

The same envelope, tightened:

- Every error answer has `ok: false`, `error` (a sentence) and **`code`**
  (a stable snake_case token the client can branch on without matching
  prose): `bad_token`, `forbidden`, `bad_session_id`, `not_found`,
  `not_live`, `empty_text`, `ambiguous_target`, `question_changed`,
  `not_waiting`, `unsent`, `window_failed`, `unknown_action`, `too_large`,
  `upstream_down` (ABS, during the migration), `internal`.
- A failure whose status is not set explicitly becomes **500 `internal`**,
  not 400 — a tmux that would not open a window is not the caller's
  fault.
- `/focus` and the desk-token refusals gain `ok: false` like everything
  else.

---

## 14. Binding to assistant-ui `ExternalStoreRuntime`

The thread state lives in the app (from the stream); the runtime reads it
and calls back.

### Messages

`convertMessage(line) → ThreadMessageLike`:

| ThreadMessageLike | From the line |
| --- | --- |
| `id` | `` `${session}:${line.at}` `` — stable across live → finished |
| `role` | `who == "you"` → `"user"`, else `"assistant"` |
| `createdAt` | `new Date(line.at * 1000)` |
| `content` | a `text` part with `line.text`; the pictures (below); for an `ask` line, a `tool-call` part `{toolName: "AskUserQuestion", args: {questions: line.ask}}` |
| `status` (assistant) | `{type: "complete"}`; `{type: "running"}` for the live line while it is speaking |
| `metadata.custom` | `{work, command, id, figure, live: {sentences, offsets, …}}` for the follow-along, work summary and slash-command chip components |

**Pictures are not `image` parts, yet.** assistant-ui silently drops an
image part whose URL is not https, `blob:` or `data:`, and the canvas serves
plain http on the tailnet. Until the app reaches the server over the
Sasonica link (https), pictures ride as a custom part rendered by the app's
own component. The prototype does this. Once every URL is https they can
become ordinary image parts.

`working` (the running turn's steps) is not a message. It renders as the
thread's in-progress indicator, where today's clients show the dots.

`recap` (the envelope's, §6.2) is not a message either — not a `system`
message, not a part. It renders as a "While you were away" card above the
composer or at the top of the thread, outside `messages`, and disappears
when `recap` is `null`. The same object on the thread's `/targets` row is
the thread list's preview line.

### Runtime callbacks

| Runtime | v0 | v1 |
| --- | --- | --- |
| `messages` | `/conversation/log` poll (`?session=` since 22 Sep 2026) | `snapshot` + `line` events |
| `isRunning` | `pending` | `state == "working"` or `pending`. Note that assistant-ui disables the composer while running, but a Claude Code session takes messages mid-turn (they queue), so the app passes sends through while running |
| `onNew` in a thread | `POST /reply {item, text}`; a thread not on the shelf yet has no item, so it goes through `POST /ask {text, target: session}` | `POST /reply {session, text}` — **available since 22 Sep 2026**, for every thread, shelved or not |
| `onNew` in a new thread | `POST /ask {text, target: "new", cwd?, agent?}` | same; the returned `session` becomes the thread id |
| `onCancel` | — (gap) | `POST /session/stop`; a second cancel within 5 s sends `speech: "silence"` |
| `onEdit` | not supported — a transcript cannot be truncated. Nearest: `POST /reply {mode: "branch", quote}` as a "branch from here" action | same |
| `onReload` | not supported | not supported |
| `suggestions` | `suggestion` → one suggestion | `suggestion` event |
| `adapters.attachments` | — (gap) | — (gap) |
| `adapters.speech` | not used — speech is agent-media's, rendered by a custom speech bar on `/speech/now` and `/speech/ctl` | same |

### Human tool UI (asks and permissions)

Two things look alike and are answered the same way:

- `approval` on the thread (a permission prompt, an AskUserQuestion modal,
  a Codex approval) is **what is on screen now**. Render it as a tool UI with
  its numbered options and answer with `POST /session/answer {session,
  choice, key}`. On 409 `question_changed`, re-render from the returned
  `approval`.
- `ask` on a line is **what was asked**, kept in the transcript. When the
  latest `ask` line is still the one on screen (an `approval` is present),
  attach the options to that message's tool-call part. Otherwise render it
  as answered and read-only.

AskUserQuestion's multi-select and free-text "Other" cannot be answered by
number. For those, the tool UI offers "answer at the desk" (`/focus`) —
that is the v0 behaviour, and a gap (§16).

### Thread list adapter (`ExternalStoreThreadListAdapter`)

| Adapter | Source |
| --- | --- |
| `threads` | `/targets.sessions` rows with `archived: false`, with `status: "regular"`, `title`, and `live` / `/sessions/state` for badges (and `mem_mb`, for a "close some" hint when `host.mem_available_mb` runs low); `recap.text` as the preview line under the title |
| `archivedThreads` | `/targets.sessions` rows with `archived: true`, `status: "archived"` — the server lists them, the app splits them out |
| `threadId` | the session id |
| `onSwitchToThread(id)` | open `/threads/{id}/events`; draft from `GET /draft` |
| `onSwitchToNewThread()` | a local, unsent thread (with a `place` / `agent` picker from `/targets.places` and `/harnesses`); it becomes real on the first `onNew` |
| `onRename(id, title)` | `POST /rename {session, title}` |
| `onArchive` / `onUnarchive` | `POST /session/archive {session, archived: true \| false}` (§6.4). Ends nothing; an "End & archive" action also sends `/session/close`. A reply into an archived thread un-archives it server-side, so the app just re-reads `/targets` |
| `onDelete` | — (gap; `/session/close` ends a session, it does not delete a thread) |

---

## 15. Inconsistencies found while writing this (v0, not fixed)

- **Stale comments on the token routes.** `/say` and `/play` are gated by
  the amux token (`canvas.py:2005`), but their handlers' comments say
  "open", and so does the module docstring's reasoning. The code is right;
  the comments are stale.
- **Failures default to 400.** Every route does `detail.pop("status",
  400)`, so tmux failures ("no attached tmux session", "did not come up
  within 45s", "session … is already running outside tmux") reach the
  client as 400 — the client's fault — when they are ours. v1 makes the
  default 500 (§13).
- **An ABS outage on an item lookup is a 404.** `session_for_item` turns
  "ABS did not answer" into a 404 whose *text* names the outage, where
  identity failures get a 503.
- **`/focus`** answers `{"ok", "detail"}` instead of `{"ok", "error"}`, and
  refuses with `{"error"}` and no `ok`. The desk-token routes do the same.
- **Two lists of sessions.** `/conversations` and `/targets.sessions`
  are the same rows. W reads one and S the other.
- **The Caddyfile and `bg.js` are stale.** They say the canvas page fetches
  `/ctl` and `/status`; it does not. The OWUI Caddy `@canvas` route still
  proxies `/ctl /show /input /play /say /status`.
- **Glance is probably failing.** It checks `127.0.0.1:8781`, and red5's
  canvas binds only the tailnet IP (not verified).
- **The module docstring's endpoint list** in `canvas.py` is incomplete
  (it lacks `/conversation/log`, `/commands`, `/speech/now`, `/speech/ctl`,
  `/rename`, `/share`, `/pageid`, `/seen`, `/agents`, `/peek`, `/pair`,
  `/speech`, `/last`). This file supersedes it.

---

## 16. Gaps: what the new app needs that nothing provides

| Need | Status |
| --- | --- |
| Device auth | **built 22 Sep 2026** (§9). Left: `code: "bad_token"` on a revoked token's 401 (with §13), and the app side (scan, keystore, send the token) |
| Session-keyed log and reply | **built 22 Sep 2026** (§10) |
| Live thread updates | specified (§11), not built |
| Stop | specified (§12), not built; needs a per-session speech marker in core (`after` / `all`) |
| Machine-readable error codes | specified (§13), not built |
| Archive / unarchive a thread | **built 22 Sep 2026**: `POST /session/archive` and `archived` on `/targets` rows (§6.1, §6.4), a server-side flag in `<state_dir>/archived.json`. Left: the app side, and moving any existing ABS `archived` tags over (not done — the tag and the flag are independent until then) |
| Session memory on the phone | **built 22 Sep 2026**: `mem_mb` per `/sessions/state` row and its `host` block (§6.1). Left: the app side |
| Delete a thread | none, and deliberately not proposed: transcripts are the harness's. Needs a decision |
| Attachments (a photo, a file) | none. `/reply` is text only. Needs an upload route and a way to hand a file path to the harness |
| Answer a multi-select or free-text ask from the phone | none. `/session/answer` presses one number. Needs keystroke sequences per harness |
| Edit / regenerate | not possible with the harnesses; `branch` is the substitute |
| A thread-list stream | deliberately deferred (§11) |
| Offline reading and downloads | none. ABS provided them for audio; the new app needs its own cache of the log (and of speech clips, if listening offline matters) |
| Thread search | none. ABS search did it. `/targets` covers the latest 40 only |
| Push notifications (a session waiting on you) | none. Matrix or FCM; out of scope here |

---

## Appendix A — routes outside the app contract

### Canvas, for screens and the desk

| Route | Auth | What | Callers |
| --- | --- | --- | --- |
| `GET /` | none | the canvas page | canvas screens, companion WebView, S's canvas panel iframe, `media speech-web` |
| `GET /pageid` | none | the page digest, plain text | companion (30 s) |
| `GET /healthz` | none | `ok` | intake-owui check, smoke test, Glance |
| `GET /events` | none | §7 | canvas page, wake watcher, smoke test |
| `POST /seen` | none (source IP names the screen; `screen` override needs the amux token) | screen-activity beacon `{focused?, blank?, screen?}` | canvas page (≤ every 30 s on activity, 10 min while focused), wake watcher |
| `GET /seen` | none | viewer registry dump | none (debug) |
| `GET /last` | none | `{t, kind}` of the last show | `intake/_visual.py` reveal wait (1 s) |
| `GET /img/<name>` | none | a spool image, or the viewer page for a navigation (`Accept: text/html`, `?view=1`; `?raw=1` forces bytes) | canvas page, S, W, `view.html` |
| `GET /persona/<slug>/<file>` | none | a portrait sprite | canvas page, via tts-shim's `/show` |
| `POST /show` | amux token | put an image or sequence on every screen | tts-shim, `media` replay, `media-visual` |
| `GET /status?channel=` | none | the popup's controller state | OWUI smoke test only |
| `POST /ctl` | amux token | a whitelisted `media` transport command | **none** |
| `GET /agents` | none | the agent strip's live session states | completions-shim, Glance |
| `GET /peek?pane=` | none | a pane's session as turns | completions-shim |
| `POST /input` | amux token | type into a pane (`{text, target}`) | completions-shim, OWUI pipe |
| `GET /sessions` | none | amux session names + last speaker | **none** |
| `GET /pair?c=` | one-time code (the spool's `pair-code`, never a device code) | HTML page that installs the amux token into the browser; no CORS headers | companion Settings |
| `GET /speech` | none | speech state + recent events + local audio | tmux-relay fast lane (`d1-runner.sh`) |
| `POST /play` | amux token | replay a pane's last clip | **none** (the `/play` in `phone_player.py` is Sasonica's own control server) |
| `POST /say` | amux token | speak text | **none** (`deploy/phone/say-http.py` is a separate server) |

Dead routes — no caller anywhere: `GET /sessions`, `GET /seen`, `POST /ctl`,
`POST /play`, `POST /say`. Candidates for deletion in the package split.

### media-share (:8771) — `X-Agent-Media-Token`

Every answer is `{"ok": bool, ...}`; 401 `"bad or missing token"`.

| Route | Answer | Caller (companion) |
| --- | --- | --- |
| `GET /`, `/health` | `{"ok", "service": "media-share"}` | none |
| `GET /recent?…` | `{"ok", "rows"}` | `RecentList.java` |
| `GET /channels` | `{"ok", "channels"}` | `Channels.java` (1 s loopback, 3 s remote) |
| `GET /chapters?channel=` | `{"ok", "rows"}` | `Transport.java` |
| `GET /ask?channel=` | `{"ok", …ask_status}` | `AskRequest.java` |
| `POST /share` | `{"ok", "url", "channel", "content_type", "title", "reason", "line"}` · 422 refused · 400 empty/oversized (8 KiB) | `ShareRequest.java` |
| `POST /play` | replay a recent row | `RecentList.java` |
| `POST /control` | `{"channel", "action", "arg"}` → `{"ok": rc == 0, "rc"}` · 422 | `Channels.java` |
| `POST /ask` | put a question to the live conversation (synchronous) | `AskRequest.java` |

When the companion is folded into Sasonica (see `sasonica-monetization`),
these either move to the canvas under device tokens or stay loopback-only.
That is a later decision, not part of this contract.

### speech-state (:8675) — no auth, peer network only

| Route | Answer | Caller |
| --- | --- | --- |
| `GET /speech` | `{"playing": bool}` | hpo's SMTC ducker (150 ms) |
| `GET /input-claim` | `{"held", "claim"}` | none |
| `POST /input-claim` | `{"owner", "ttl_s"?, "source"?}` → `{"ok", "owner", "ttl_s"}` (TTL clamped); also holds speech for 1.5 × TTL | the phone's call guard heartbeat (15 s, TTL 45 s) |
| `DELETE /input-claim?owner=` | `{"ok", "cleared"}` | call guard release |

---

## Appendix B — keeping this true

- `packages/server/tests/test_contract.py` pins every v0 app-route shape in
  §6 over real HTTP. It covers key sets exactly, the auth failure mapping
  across every gated GET, the SSE opening frames, and that the token routes
  refuse without a token. No test can reach a pane: every typing path is a
  recorder.
- `test_devices.py` (§9) and `test_session_keys.py` (§10) pin the built v1
  parts with the same rig, imported from `test_contract`. Among other things,
  every gated GET passes on a device token without ABS being asked, and the
  device token never reaches ABS.
- `test_recaps.py` pins recaps ([Recaps](#recaps)): parsing, the backwards
  read, the cache, and the `recap` field on both routes. The server conftest
  points `CLAUDE_CONFIG_DIR` at a throwaway dir, so no test reads a real
  transcript.
- `test_archive_and_memory.py` pins `POST /session/archive`, `archived` on
  the rows, un-archive on send, and `mem_mb` / `host` on `/sessions/state`
  against a fake `/proc` tree. The conftest points `procmem.PROC` at an empty
  throwaway dir, so no test reads the machine's real processes for memory.
- Run all three packages' tests together (`packages/server/tests
  packages/visual/tests packages/core/tests` in one pytest run): basename
  collisions and cross-suite isolation faults only show up that way.
- When a shape changes, change this file and the test in the same commit.
- When a v1 section is built, move it into §6 and pin it the same way.
