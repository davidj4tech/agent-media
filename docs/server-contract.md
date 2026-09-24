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
  and stop. **Device tokens (§9), threads keyed by session (§10) and the
  per-thread stream (§11) are BUILT (22 Sep 2026)** — those sections now
  give the shapes as implemented, and the deviations from the first draft.
  Error codes (§13) are still specified only. The log
  also gained **messages** (§6.2.2): the thread read from the agent's own
  transcript, as the terminal shows it, rather than from speech. **Stop
  (§12) is BUILT (22 Sep 2026)**, with its per-session speech marker (`after` / `all`).
- **Headless sessions (§17), behind `MEDIA_HEADLESS` (off by default).**
  With the flag on, a new chat from the app runs as a `claude -p` process
  held by `media-sessiond` instead of a TUI in a pane. Every shape below
  holds for it; the few additions are marked "headless" where they occur.
  With the flag off nothing in this file changes.
- **Layouts (§18).** Where an app chat's pane opens is a host setting:
  `projects-per-tmux-session` (David's desk: amux, `p-<project>` sessions) or
  `default` (one `sasonica` tmux session, projects are folders). Detected,
  written at install; no route's shape changes.

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

Three HTTP servers, all stdlib `http.server`, all on the tailnet (§19:
the tailnet is the development path; a reverse proxy or an outbound tunnel
is how anyone else reaches one).

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

**Compression.** `GET /item` and (since 22 Sep 2026) `GET
/conversation/log` compress: gzip, when `Accept-Encoding` allows and the
body is over 4 KiB. Nothing else does.

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
`/session/close`, `/session/answer`, `/session/stop`, `/draft`, `/speech/now`, `/speech/ctl`,
`/sessions/state`, `/commands`, `/rename`, `/harnesses`, `/harnesses/run`,
`/harnesses/screen`, `/harnesses/keys`, `/harnesses/close`,
`/harnesses/logout`, `/harnesses/updates` (23 Sep 2026), `/share`,
`/search` (23 Sep 2026), and (22 Sep 2026) `/threads/{session}/events` — matched as a pattern, not
listed (`app.cors_path`), so its preflight and its answers, refusals
included, carry the same headers.

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
| **id** (message) | the transcript `uuid` of the message's first record (`"line:<at>"` for harnesses read from lines) | a message's identity (§6.2.2): stable as it grows, what `message` events (§11) and `?before=` name |

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
    "archived": false, "rested": null, "pinned": true},
   {"session": "6c73…", "title": "Sasonica music", "live": false, "pane": null, "at": 1790000000.1,
    "recap": {"text": "We're making the conversation page show what Claude is doing. Next: try it on the phone.",
              "at": 1789807216.618, "source": "agent-media"},
    "archived": true, "rested": {"at": 1790001000.2, "reason": "idle-tight"}, "pinned": false}],
 "places": [{"name": "agent-media", "path": "/home/ryer/projects/agent-media", "at": 1790000000.1}]}
```

- `sessions` is `sessions_index()`. Live sessions come first, titled from the
  pane (Claude's terminal title with the spinner stripped; Codex, pi and
  Hermes from their own session files, cut to 60 chars). A live session
  with no title is left out. Then up to 40 shelved conversations, newest
  first, titled by their folder name, with `at` = the manifest's mtime —
  plus **every archived one**, however old (23 Sep 2026): the caps on
  closed rows (40 shelved, 20 ended headless) count only rows not archived,
  so the app's Archived filter shows them all.
  Then the conversations only a harness's store knows about (§6.16).
  **`at` is only on rows that are not live.** A session appears once, live
  if it is live.
- `recap` (every row, 22 Sep 2026): the latest "where this thread was"
  summary for that session, `{"text", "at", "source"}`, or `null` — Claude
  Code's own "while you were away" paragraph (`"source": "claude"`), or the
  one agent-media wrote before the idle reaper rested the session
  (`"source": "agent-media"`), whichever is newer. See [Recaps](#recaps)
  below. The app uses it as the row's preview line.
- `archived` (every row, 22 Sep 2026): whether the thread is archived — a
  flag this server keeps per session (`archive.py`, `<state_dir>/archived.json`),
  set with `POST /session/archive` (§6.4). **Archived rows stay in the list**;
  the app files them under an "Archived" section itself. Talking to a thread
  (a reply, or an `/ask` routed into it) clears the flag.
- `rested` (every row, 22 Sep 2026): `{"at", "reason"}` when the idle reaper
  closed the session (`reap.py`, [Resting](#resting) below), else `null`.
  `reason` is `"idle"` (past `MEDIA_REAP_IDLE_H`, 12 h) or `"idle-tight"`
  (past the shorter `MEDIA_REAP_TIGHT_IDLE_H`, 6 h, because the host was
  short of memory). **Always `null` on a live row**, whatever the file says:
  a running session is not resting. A session you ended with
  `/session/close` is never rested.
- `pinned` (every row, 22 Sep 2026): kept open against the idle reaper, set
  with `POST /session/pin` (§6.4). A pin affects the reaper only.
- `speech` (every row, 24 Sep 2026): the thread's speech level —
  `"interrupt"`, `"auto"`, `"normal"` or `"quiet"` — set with
  `POST /session/priority` (§6.4) or `media priority`. `priority` (same
  day) is true for interrupt and auto: its replies are never held by the
  desk toast and never silenced by a pane mute.
- `harness` (every row, 23 Sep 2026): which agent holds the conversation —
  `"claude"`, `"codex"`, `"pi"` or `"hermes"`. Rows the app has never seen
  before carry `source: "store"` as well: conversations known only from
  their harness's own store, because they never spoke and are not running
  (§6.16, where the window and the caps are).
- `cwd` and `project` (every row, 22 Sep 2026): where the thread ran — the
  first `cwd` its Claude Code transcript records (Codex/pi/Hermes: their
  own session files), and the project that names, as the layout calls one
  (`sessions.project_of`, `agent_media_core/layout.py`). David's layout: the
  series its shelf folder is filed under (`p-agent-media`), else `p-<name>`
  for a directory under `~/projects` (a worktree inside one counts), else
  `null`. Default layout: the folder's basename (home is `sasonica`). Both
  `null` when unknown. The app draws `project` as a small line under the
  title and groups its By-project order by it. The same two keys are on
  `/conversations` rows, `/dashboard`'s `recent`, `working` and
  `needs_you` rows, and the thread stream's snapshot (§11).
- `places`: up to 6 directories sessions have run in, newest first
  (running sessions count as "now"). These are the only directories a new
  chat may be opened in — `/ask` checks against this list (with no limit).
- **Headless rows** (`MEDIA_HEADLESS`, §17): the sessions `media-sessiond`
  holds are listed after the live pane sessions — the running ones, then up
  to 20 ended ones (parked or closed), newest first — with the same keys
  plus four that only they carry: `"driver": "headless"`, `"drivable": true`
  (the phone can send to it, answer it and stop it), `"harness": "claude"`
  and `"source": "sessiond"`. `pane` is always `null`; `live` is whether its
  process is running; an ended one has `at` (its last event) like a shelved
  row. Title: the shelf's name, else Claude's (`/rename`, then `ai-title`),
  else the first message. Pane rows never carry the four keys.

Clients: S (`utils/sasonicaTargets.js`, drawer and ask page), on open.

##### Recaps

When you come back to a Claude Code session after being away, Claude Code
writes a short paragraph of where things stand into the session's
transcript: a `{"type": "system", "subtype": "away_summary", "content":
"… (disable recaps in /config)", "timestamp": "<ISO>"}` line. The server
reads it (`agent_media_server/recaps.py`, read-only) and hands it on as

```json
{"text": "Runlet is now Sasonica Shell, running on red5. Next: add the connector.",
 "at": 1789974112.291, "source": "claude"}
```

- `text`: the paragraph, with Claude Code's trailing hint stripped (a
  trailing parenthetical that mentions recaps; any other trailing "(…)" is
  kept).
- `at`: epoch seconds, 3 dp, from the line's timestamp.
- `source` (22 Sep 2026): `"claude"` for Claude Code's own, or
  `"agent-media"` for one the idle reaper wrote before resting the session:
  when a session it is about to close has no recap newer than its last
  message, it asks the follow-up gateway (Haiku, `MEDIA_FOLLOWUP_MODEL`, the
  `intake/_followup.py` call style; `MEDIA_REAP_RECAP_MODEL` /
  `_TIMEOUT` override, 20 s) for one to three sentences from the end of the
  conversation, and keeps it in `<state_dir>/generated-recaps.json`. A failed
  call never blocks the close. `recaps.recap_for` answers the newer of the
  two; that is what `/targets`, `/conversations` and `/conversation/log`
  carry.
- `null` when the session has neither. Codex, pi and Hermes never have a
  `claude` one, so theirs is `null` until the reaper has rested them once.
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

##### Resting

The idle reaper (`agent_media_server/reap.py`, `media session-reap`, a
systemd user timer every 15 min — see `notes/2026-09-22-session-reaper.md`)
closes agent sessions nobody has spoken in for 12 h, or 6 h when the host is
short of memory (`MemAvailable` under 20 % of `MemTotal` or under 1500 MB).
**Idle** is time since the last message in either direction — the last
user/assistant record in the transcript, or the last speech of it — not pane
quietness. It never closes a session that is pinned, is the one running the
reaper, is the source of live (speaking or paused) speech, is working, is
stopped on a dialog or question, has text half-typed in its composer, or has
a non-empty `/draft` written in the last 6 h; and it only ever considers
agent panes `live_sessions` finds, never a shell. Its default mode is a dry
run (`MEDIA_REAP_MODE=dry-run`) that only logs.

A session it closes goes through the same `send.close_pane` as
`/session/close`, and is recorded in `<state_dir>/rested.json` as
`{"<session>": {"at", "idle_h", "reason"}}`; rows show `{"at", "reason"}`.
The mark is dropped when the session is used again — a reply or a routed
`/ask` into it, `/session/resume`, a later reaper run that finds it live —
and by a `/session/close` from the person (who has now decided).

#### `GET /conversations` — gated

`{"ok": true, "sessions": [...]}` — the same rows as `/targets.sessions`.
Clients: W only (`NewChat.tsx`, the picker). Kept separate for historical
reasons; v1 drops it in favour of `/targets`.

#### `GET /sessions/state` — gated

What each live session is doing.

```json
{"ok": true,
 "sessions": [{"session": "0f1e…", "tail": "p-agent-media/Sasonica web", "state": "working",
               "mem_mb": 364, "title": "Sasonica web"}],
 "host": {"mem_total_mb": 7758, "mem_available_mb": 1568, "sessions_mem_mb": 3009}}
```

- `state`: `working` | `waiting` (has answered, waiting on you) |
  `approval` (stopped on a dialog). Read from the pane's screen, not a hook.
  A headless row (§17) has it from the agent's own events instead, and
  carries `"driver": "headless"` (a fifth key only it has); `mem_mb` is its
  process tree, measured the same way.
- `title`: the session's name as `/targets` gives it (a pane's own title, or
  its first message while Claude still calls it "Claude Code"; a headless
  row's §17 title). So a notice can name a chat started since the app last
  fetched `/targets`; before this it fell back to the id's first 8 characters.
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

The conversation — the chat itself — as **messages** read from the agent's
transcript (§6.2.2, 22 Sep 2026) and as the older **lines** built from
speech. The `?session=` form (22 Sep 2026, §10) answers the same envelope
with the same shapes and wins when both are given; it never asks ABS, so
`start`/`end` are always `null` on it.

Query (both forms): `limit` — how many messages, newest first page
(default 60, at most 500); `before` — a message `id`: only messages before
it (the next page back). Neither touches `lines`.

`around` — a message `id` (a search hit, §6.14; `?session=` form only;
messages come back without `messages=1`): the page holding that message,
from 5 messages before it **to the newest**, so the live thread joins on
below it; `older` as usual. When that would be more than 500 messages, the
page is `limit` long from 5 before it, and `newer: true` says the thread
goes on past it. The answer also carries `"around": {"id", "found",
"newer"}` and `newer`. An id that is not there (`found: false`) answers the
newest page, as without it. Other harnesses' threads find their line ids
(`line:<at>`) the same way.

```json
{"ok": true, "session": "6c73…",
 "messages": [ …message… ],
 "older": true,
 "lines": [ …line… ],
 "pending": false,
 "working": null,
 "approval": null,
 "suggestion": "",
 "recap": null}
```

**Envelope**

- `messages` (22 Sep 2026): the thread as its transcript has it, oldest
  first — §6.2.2. The client's source of truth for what was said.
- `older` (22 Sep 2026): messages exist before the first one here; ask
  with `?before=<messages[0].id>`.
- `lines`: **deprecated** (22 Sep 2026). The spoken turns, as before; kept
  so clients that read them keep working, and removed once both clients
  read `messages`. New clients should not read them.
- `pending`: the last line is the listener's, a turn is running, or (a
  live session) the last message is the listener's — show "thinking" and
  poll faster.
- `working`: `null`, or what the running turn is doing:
  `{"since": <epoch>, "count": <steps so far>, "current": "<step text>",
  "current_at": <epoch|null>, "steps": ["…", …], "server_time": <epoch>}`.
  `steps` holds the latest few (`MAX_STEPS`), newest last.
- `approval`: `null`, or the dialog the session is stopped on (see below).
- `suggestion`: §6.2.1, `""` while pending.
- `recap` (22 Sep 2026, both forms): the session's latest recap,
  `{"text", "at", "source"}`, or `null` — the same object as on the
  session's `/targets` row ([Recaps](#recaps)); `source` is `"claude"` or
  `"agent-media"` (written before the idle reaper rested the session). **Not a line**, and never inserted
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
| `unheard` | held replies | `true` for a reply held for the listener and never played (the desk toast, `intake/toast.py`: history `extras.held` without `extras.heard`); absent once a replay has played it, and on the line being heard (24 Sep 2026) |
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

**The newest turn's timeline, without `live` (23 Sep 2026).** The live line
exists only while `now_playing` names the reply, and there are several ways
to lose that row while the audio plays on: a barge-in, a submit that died, a
phone that was handed the clips and is playing them itself. A reader
following the bold then had nothing to follow for the rest of the reply.

So the **newest spoken turn** carries two extra fields whether or not
anything is live:

| Field | Meaning |
| --- | --- |
| `sentences` | the turn split into sentences, as spoken |
| `offsets` | seconds from the start of the turn at which each begins |
| `measured` | `true` when the player measured the offsets; `false` when they were apportioned from clip lengths (that one drifts within a reply) |

There is **no `live`, no `elapsed`, no `server_time`** — this is not a claim
that the turn is playing. It is the timeline, handed over so a player that
knows its own position can bold from its own clock; on the phone lane the app
*is* the player. When the line is live, the live line's fields win and these
are not written over it.

Only the newest turn: a whole conversation's sentences is payload nobody
reads, and the newest is the only one a player still holds.

`clip_starts_s` (what the player reached) now survives onto the ended history
row, so these offsets are measured rather than apportioned whenever the reply
played far enough to measure them. A clip the cache has swept takes its start
with it, so a surviving sentence is never bolded against another's audio.

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
manual scroll. v1 clients open the stream (§11) instead and fall back to
this poll.

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

**Question form** (22 Sep 2026). Claude Code's AskUserQuestion on a pane is
read by `agent_media_server/asks.py`, and its approval carries the fields
above **and**:

```json
{"kind": "question",
 "multiSelect": true,
 "free_text": true,
 "questions": [{"question": "Which colour?", "header": "Colour", "multiSelect": false,
                "free_text": true,
                "options": [{"n": 1, "label": "Red", "description": "warm",
                             "detail": "warm", "checked": false}, …]},
               {"question": "Which pets?", "header": "Pets", "multiSelect": true, …}],
 "current": 1, "review": false,
 "tool_use_id": "toolu_01…", "source": "hook"}
```

- `questions` is every question asked, in order, options numbered from 1.
  Claude Code writes the tool call to its transcript only once it is
  answered, so the words come from the PreToolUse hook, which keeps the tool
  input (`agent_media_core/pending_asks.py`, `<state_dir>/asks/<session>.json`):
  `source: "hook"` when that copy's question is the one on screen, which is
  checked every read. Otherwise (`source: "screen"`) only the tab on screen is
  known: one question — or, when the dialog has other tabs (or is the review
  page, or its list scrolled off a short pane), `partial: true` and
  `questions: []`: the card says "answer it at the desk", while the v0
  `question` / `options` still describe the tab on screen.
- `checked` is what the screen shows ticked, for the question on screen
  (`current`); `review` is true on the "Review your answers" page.
- `free_text`: every question offers an "Other" row ("Type something").
- `key` hashes the tool call (`source: "hook"`), so it holds across tabs and
  ticks; from the screen alone it hashes the question and its labels.
- The v0 `options` are this tab's rows as numbered on screen, the free-text
  and "Chat about this" rows included, with `checked`.

What the dialog is, measured on Claude Code 2.1.278 (captures in
`packages/server/tests/fixtures/asks/`): a tab row (`←  ☐ Colour  ☒ Pets
✔ Submit  →`; one single-select question has just ` ☐ Size`), the question,
the options — `N. [ ] label` / `N. [✔] label` with the description under it
for multi-select, `N. label ✔` for the chosen single-select one — then the
free-text row, a `Submit` row (multi-select), a rule, `N. Chat about this`
and the footer; after the last tab, "Review your answers" with `1. Submit
answers` / `2. Cancel`. The keys: a digit picks a single-select option and
moves to the next tab (one question: sends it); on a multi-select it toggles
that box without moving; the free-text row's digit moves the cursor there,
where typing fills and ticks it and digits are text; Tab goes to the next
tab (from the free-text row, to the Submit row, where Enter moves on); Left
goes back; `1` on the review page sends.

**Headless form** (22 Sep 2026, §17). A headless session's approval is not
read off a screen: it is the agent's own pending permission request
(`control_request can_use_tool`), kept by `media-sessiond` until answered —
it never times out. The object carries the request's fields **and** the v0
fields above, so a client that answers by number keeps working:

```json
{"id": "34c79dca-81d5-47ff-a698-e9fd66575082",
 "kind": "tool",
 "tool": "Bash", "display_name": "Bash",
 "input_summary": "touch allowed.txt",
 "input": {"command": "touch allowed.txt", "description": "Create a file"},
 "description": "Create a file", "blocked_path": null,
 "tool_use_id": "toolu_01QZ7KJFURZG1GqikT7cdVHc",
 "suggestions": [{"type": "addRules", "destination": "localSettings", …}],
 "at": 1790000123.4,
 "question": "Allow Bash: touch allowed.txt?",
 "partial": false,
 "options": [{"n": 1, "label": "Allow", "detail": ""},
             {"n": 2, "label": "Deny", "detail": ""}],
 "key": "5d1f0e9a2b7c", "agent": "claude"}
```

- `id`: the CLI's request id — what the structured answer echoes.
- `kind`: `"tool"`, or `"question"` for AskUserQuestion, which also carries
  `questions`: `[{"question", "header", "options": [{"n", "label",
  "description", "detail", "checked": false}], "multiSelect", "free_text":
  true}]`, as asked, and `multiSelect` / `free_text` — the pane's question
  form, so one card serves both.
- `input_summary`: the same one-line summary a message's tool part has
  (§6.2.2). `input`: the request's input, strings cut at 300 chars.
- `suggestions`: the request's `permission_suggestions`, verbatim (spike:
  `addRules → localSettings`, `setMode acceptEdits → session`, …). Nothing
  here applies them yet.
- v0 fields: `question` is "Allow <tool>: <summary>?" for a tool, the first
  question's words for a question. `options` is Allow/Deny for a tool; for a
  question, the options numbered **only when one single-select question is
  asked** — otherwise `[]` and `partial: true`, because a number cannot
  answer it. `key` is a hash of `id`.
- One approval at a time: the oldest pending request. The next appears once
  it is answered.

#### 6.2.2 Messages — BUILT 22 Sep 2026

Code: `agent_media_server/transcript.py` (the parser and the speech join),
`threads.messages_for`. Pinned by `packages/server/tests/test_transcript.py`.

Why: lines come from speech history, so a reply appeared only once it had
been queued and rendered for speech — behind any other speech waiting — and
only as the words spoken. The terminal has the reply the moment it is
written, with its steps and narration. Messages are read from the same file.

```json
{"id": "8a1f…-uuid",
 "role": "assistant",
 "at": 1790000123.456,
 "parts": [
   {"type": "reasoning", "text": "", "redacted": true},
   {"type": "reasoning", "text": "Found the route table; reading it next.", "redacted": false},
   {"type": "tool", "name": "Bash", "title": "Run the tests",
    "input_summary": "pytest -q packages/server/tests", "status": "done",
    "result_summary": "466 passed in 24s", "tool_use_id": "toolu_01…"},
   {"type": "ask", "ask": [{"question", "options": [{"label", "description"}], "multiSelect"}],
    "status": "done", "answer": "A", "tool_use_id": "toolu_02…"},
   {"type": "text", "text": "All fixed. The log now…"}],
 "spoken": {"id": 4711, "key": "3f9a…", "at": 1790000131.2,
            "images": ["/img/…"], "figure": true,
            "live": {"sentences": […], "sentence": 1, "offsets": […], "elapsed": 3.2,
                     "server_time": 1790000134.4, "delay": 0.0, "paused": false},
            "timeline": {"sentences": […], "offsets": […], "measured": true}},
 "turn": {"running": false}}
```

| Field | Meaning |
| --- | --- |
| `id` | the transcript `uuid` of the message's first record — Codex's item id (`msg_…`, `ctc_…`), pi's record id. Stable: a message keeps it as it grows. Hermes (below): `"line:<at>"` |
| `role` | `"user"` or `"assistant"` |
| `at` | epoch seconds, 3 dp, of the first record |
| `parts` | in order, as the terminal draws them (below) |
| `spoken` | the speech of this message, or `null` if it was not spoken (or cannot be recognised). `id` is the history row for `/speech/ctl replay-id` (`null` while it is still playing for the first time); `key` the reply's dedup key; `images`/`figure` as on lines, only when drawn; `live` only while it plays — the §6.2 live-line fields, moved here; `timeline` instead of `live` on the newest spoken turn when it is not playing (`{sentences, offsets, measured}`, §6.2); `unheard: true` on a reply held and never played (as on lines) — the app makes its play key the big one |
| `turn.running` | the turn is still going: the last record asked for a tool, or a tool has no result yet. Always `false` when the session is not live |
| `command` | user messages that are a slash command only: `{name, args, text}`, the line's chip (`slash.py`); settings commands are never messages |
| `peer` | user messages another session delivered into this one (a `<cross-session-message>`): `{name}`; the text is only the message body. Not the listener's words — the app shows a small "From <name>" and never counts it as a send of its own |

**Parts.**
- `text` — the words, **as Markdown** (Claude writes Markdown; the client
  renders it — assistant-ui's `MarkdownText`). `[[visual: …]]` and
  `[[reveal: …]]` markers are removed from assistant text (since 22 Sep
  2026): they are instructions to the canvas, never words to read. A marker
  mid-sentence leaves one space; one on its own line leaves no blank line.
  The speech join (below) keys on the raw words before they are removed.
  Cut at 32 KB.
- `reasoning` — `redacted: false` with the text the model wrote, or
  `redacted: true` with `""`: thinking whose text Claude Code does not keep.
  A run of redacted blocks is one part. **What the transcripts on red5
  hold** (Claude Code 2.1.263–2.1.278, Sep 2026, 40 recent transcripts):
  ~90 % of `thinking` blocks are signature-only (`"thinking": ""`) — the
  reasoning itself is not in the file. The ones with text are the model's
  short running *narration* between tool calls ("Spec corrections are
  committed. Next I'll…"); their signatures are marked `narration`, the
  empty ones `thinking`. So the phone gets the narration verbatim and a
  "thought" marker where the model thought. `redacted_thinking` blocks (none
  seen) become redacted parts too.
- `tool` — `name`; `title`, the step in plain English (`activity.describe`,
  what `working.steps` already says); `input_summary` — Bash's command line,
  Read's path and line range, Edit's path and `−old +new` line counts,
  Write's path and line count, Grep/Glob's pattern, WebFetch's URL, Agent's
  description; other tools their input as compact JSON; all cut at 300
  chars; `status` `running` | `done` | `error` (an interrupted turn's
  unanswered tools are `error`, "interrupted"); `result_summary` — the
  first 300 chars of the result, except Read, which says `"N lines"`.
  **File contents are never shipped**: `toolUseResult` (which holds whole
  files) is never read. A subagent is one `Agent` tool part; its own turns
  (sidechain records) are not messages — they are its own log (§6.12).
- `ask` — an AskUserQuestion, in the §6.2 `ask` shape, with the `answer`
  once given. Claude Code writes it to the transcript only after it is
  answered; the one on screen now is `approval`.

**What a message is.** A prompt starts a user message; the assistant
records after it, up to the next prompt, are one assistant message (every
text, thinking and tool block of the turn, with tool results attached by
`tool_use_id`). A prompt typed while a turn runs (`queued_command`) is a
user message where it landed, and the turn continues in a new assistant
message. Left out: `isMeta` records, compact summaries, sidechains,
attachments and bookkeeping records, `system` records (a recap is the
envelope's `recap`), and the harness's asides in the prompt stream (task
notifications, reminders, local command output — `strip_system_blocks`, the
speech path's rule): those are not messages, but the turn after one is a
new message.

**The speech join** (`transcript.join_speech`), in order:
1. **By key.** The Stop hook keys a reply by `sha1(strip_markdown(reply
   without [[visual:]] markers))`, before any spoken summary rewrites it,
   and the line carries that key. The same hash of the assistant message's
   final text (the last text part; also the text parts after its last tool,
   joined) is an exact match. A `[[reveal:]]` reply is spoken in two halves,
   each keyed on its own; the first half's line wins.
2. **By words**, for what the key misses (old rows, a reply handed to the
   hook differently than it is reconstructed): the unclaimed agent line
   most similar (≥ 0.6, `difflib` on normalised words) to the final text
   among those said no earlier than 5 s before the message began.
3. A listener line joins the user message with the same words said within
   two minutes of it.

Measured on red5's 8 most recent agent-media sessions (22 Sep 2026,
read-only): 204 of 209 spoken agent lines joined a message, **all of them by
key**. Of the 5 left over, 4 were questions read on the alert lane (their
`ask` part carries no `spoken` yet) and 1 a reply the join did not
recognise. 165 of 178 listener lines joined a user message.

Each line joins one message. **Failure mode:** a message never spoken, or
whose speech is not recognised, keeps `spoken: null` — the join never
guesses by time alone, so a wrong replay is impossible and a missing one is
the cost. A line that joins nothing (a notification, a question read on the
alert lane) stays in `lines` and is not a message. Speech whose audio the
cache has swept is not in the lines either (session_feed drops it), so old
messages lose `spoken` as their clips are swept.

**Codex and pi have readers of their own** (23 Sep 2026). `READERS` in
`transcript.py` holds one per harness: the fold from that harness's records
into the messages above, and the two cheap tests the backwards scan makes on
a raw line before parsing it. What each contributes:

| | Codex (`rollout-*.jsonl`) | pi (`<stamp>_<id>.jsonl`) |
| --- | --- | --- |
| a prompt | `response_item` `message`, role `user` — its `developer` messages and the tag-wrapped preamble are not the conversation | `message` record, role `user` |
| the words | role `assistant`, `output_text` | role `assistant`, `text` blocks |
| thinking | `reasoning` — a `summary` when there is one, else encrypted: a redacted part, as a signature-only Claude block is | `thinking` blocks, **with their words** |
| a step | `custom_tool_call` (the sandboxed `exec`, whose command is pulled out of its script) and `function_call`, answered by the matching `*_output` | `toolCall` blocks, answered by a `toolResult` record naming the call (`isError` marks a failure) |
| the turn ends | `event_msg` `task_complete` | the next prompt, or a `compaction` |

Tool names are canonicalised (`activity.canonical_tool`: `bash`, `exec`,
`exec_command` → `Bash`), so one table of summaries and titles serves every
harness. `turn.running` on these is "a tool call is still waiting on its
result" — neither harness writes a stop reason, and whether a session is
working now is §6.2's question, not the transcript's.

**Hermes** keeps its conversations in a SQLite database rather than a file,
so it has no reader: its messages are still its lines reshaped (one text or
ask part each, `id` `"line:<at>"`, `spoken` when the line was spoken).

**Cost** (measured on red5, 22 Sep 2026, the 8 largest transcripts, 7.5–
11.1 MB, page cache warm): a first read, from the end, 40–210 ms; the
whole file 40–100 ms; an unchanged file 0.03 ms (a stat); an append of a
few records 0.1–0.25 ms (only the new bytes are read). A page of 60
messages is 106–315 KB of JSON, 32–87 KB gzipped — tool summaries are two
thirds of it. Per file, the fold is cached with the inode and the offset
read to; a file that shrank, was replaced or rewritten in place (the bytes
before the old offset differ) is read again from the end.

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

Request: `{"session": "<session>" | "item": "<item>", "text": "…", "quote"?: "…", "mode"?: "continue" | "branch", "refs"?: {"<title>": "<session>"}}`

- **`refs`** (24 Sep 2026), on `/reply` and `/ask`: `{"<title>":
  "<session>"}` for the `@[<title>]` chips in `text` — another conversation
  named by a chip (the app's Share… or `@` in the composer) instead of by
  typing its title. Each chip gets a line at the foot of the text, `@[<title>]
  is conversation <session> (<harness>, transcript <path>)`, so the agent
  can read it; the chip itself stays as written. A chip with no entry in
  `refs` is matched by title among the threads, used only when exactly one
  has it; one that names nothing gets no line (`refs.py`).
- `session` (22 Sep 2026, §10) wins when both are given. 400 `"not a
  session id"`; 404 `"no such session <8 chars>"` when it has no pane and
  no transcript. `branch` works from either form.

- **Multi-line** (22 Sep 2026). A **Claude Code pane in tmux** gets the
  text with its line breaks: each line is typed (`send-keys -l`, in pieces
  of at most 400 characters) with **Alt+Enter** (`M-Enter`, Claude Code's
  newline) between lines, then Enter. Trailing spaces go and runs of blank
  lines shrink to one. Everything else gets it **flattened to one line**
  (a newline there would submit half the message): Codex, pi and Hermes
  panes (their newline key is unprobed) and every herdr pane (herdr has
  no key-by-key send). `panes.multiline_ok` is the rule. The quote rides in
  front, always on one line, as `Re: "<quote, ≤160 chars>" — <text>`. The
  text as the box had it is recorded as the listener's turn.
- **Not bracketed paste**, though tmux can (`load-buffer` + `paste-buffer
  -p`). Probed 22 Sep 2026 against Claude Code 2.1.278: a paste, even of
  one line, reaches the model wrapped in `<pasted_content id="…">`, and the
  model treats it as quoted material rather than as the listener's request
  ("I only follow instructions embedded in pasted content when your own
  message explicitly asks me to"). Typed text with Alt+Enter arrives as a
  plain message and is acted on. The same wrapping catches **fast typing**:
  a single burst of more than ~900 characters (800 was typed, 1000 was not)
  becomes a `[Pasted text #1]` placeholder — so long messages went in
  wrapped before this change too; the 400-character pieces are what stop
  it (a 1634-character four-line message went in plain, measured).
- The composer check treats a `[Pasted text` placeholder after the prompt
  glyph as the message still being there (unsent), and an empty composer as
  sent.
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

**Headless** (§17): nothing is flattened — the agent gets the text as
written, and a quote rides as its own paragraph (a Markdown `>` quote) —
and nothing is typed. The answer is `{"ok": true, "session", "pane": null,
"opened": <resumed a parked or closed session>, "submitted": true,
"driver": "headless", "queued": <a turn was running: the message is queued
and may join that turn at its next tool boundary, so one reply can answer
both>, "acked": <the agent acknowledged it>, "uuid": <the id it was sent
with>}`. A session parked for idleness is resumed by the reply (a few
seconds slower). `branch` opens a fresh headless session in the same
directory. 503 `code: "down"` when `media-sessiond` is not running (a
headless thread is never revived in a pane), 503 `code: "busy"` when the
host is at `MEDIA_SESSIOND_MAX` and nothing is idle.

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
| `project` | open a fresh session in that project's directory: a series (`p-agent-media`) on David's desk, a folder basename (`agent-media`) in the default layout (§18) |
| `cwd` | open a fresh session in that directory — must be a `/targets` place |

Routing, first match wins:
1. `target`: a session id (`how: "picked"`), or `"new"` (`how: "asked"`).
2. A target spoken at the start of the words (`how: "spoken"`).
3. The player's thread — `player_session`, else `player_item` (`how: "player"`).
4. `sticky`, if it still exists (`how: "sticky"`).
5. A fresh session (`how: "default"`), in `cwd`, `project`, or the layout's
   fresh target (§18): the scratch amux registration (`MEDIA_ASK_SESSION`)
   on David's desk, home in the `sasonica` tmux session otherwise.

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

**Headless** (§17): with `MEDIA_HEADLESS` on, every fresh Claude session
`/ask` starts — `target: "new"`, a spoken "new chat", and the default route —
is headless: `{"ok": true, "mode": "new", "how", "session": "<never null:
chosen up front>", "pane": null, "opened": true, "fresh": true, "tmux":
null, "agent": "claude", "submitted": true, "driver": "headless", "acked",
"title": "", "text"}`. It runs in the same directory a pane would have (the
`cwd`, `project`, or the scratch registration's), with the amux flags
dropped (the permission profile is sessiond's, §17). Codex, pi and Hermes
still open panes. `/ask` flattens its words as before (the parse needs one
line); only `/reply` passes newlines through.

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

**Auto** (22 Sep 2026): `{"session", "auto": true}` with no `title` has the
server name it — the conversation's text (its opening and its latest, 8k
chars) to the summary gateway on `MEDIA_TITLE_MODEL`, else the follow-up's
model — and then renames exactly as above; the answer's `title` is the name
it chose. A `title` sent alongside wins. 502 `"could not think of a name"`
when the gateway gives nothing usable. Takes a few seconds.

**After the first turn** (23 Sep 2026): a headless thread nobody has named
names itself. When a session's first turn ends without an error, sessiond
runs the same naming off a thread (`threads.name_unnamed` → `auto_title` →
`book_tracks.rename`) and types `/rename` into the live session, so the
shelf, the library item and Claude Code's own copy agree. It is skipped for
a thread that already has a name — the manifest's `title` (a `/rename`) or
Claude Code's name file, which `/rename` at the terminal writes too; the
folder does not count, being the question it opened with. Nothing waits on
it and every failure is silent. `MEDIA_AUTO_TITLE=0` switches it off. Only
headless: a session at the terminal is named by Claude Code itself (an
`ai-title` record), which `-p` does not write.

**Headless sessions** (§17, 22 Sep 2026): the same `/rename <title>` goes
to a live one as a stream-json message through sessiond — Claude Code takes
it under `-p` as a local command (no model call, a zero-cost `result`, a
`custom-title` record; measured on 2.1.278) and it is not counted as a turn
or shelved as the listener's words; behind a running turn it queues. A
parked one is not woken for it: `terminal: false`, `why: "headless sessions
pick up the name on their next resume"` (the name is already in its
transcript). Their `/targets` rows show the shelf's name — the manifest's
`title` (the rename), then the folder's.

Clients: S (`ReplyBox.vue`).

#### `POST /session/resume` · `POST /session/close` — gated

`{"session"}`.

- resume, already live: `{"ok": true, "session", "pane", "live": true, "opened": false}`
- resume, revived: same with `"opened": true`. 404 when there is no transcript.
- close, not live: `{"ok": true, "session", "live": false, "closed": false}`
- close, closed: `{"ok": true, "session", "pane", "live": false, "closed": true}`.
  Only the pane hosting that very session is killed; the transcript stays,
  so close is undone by resume.
- (22 Sep 2026) Both go through `send.close_pane`, the path the idle reaper
  also uses — but a close from here is the person's decision, so it is never
  marked rested, and drops a `rested` mark left from an earlier reaper close.
  A resume that opens the session drops the mark too
  ([Resting](#resting)).
- **Headless** (§17): resume respawns the agent with `--resume` (no modal
  under `-p`) and answers `pane: null`, `"driver": "headless"`; close ends
  its stdin, then SIGTERM after `MEDIA_SESSIOND_CLOSE_GRACE` (10 s), and
  answers at once. A closed headless session stays headless: the next reply
  resumes it with sessiond, never in a pane. The idle reaper does not touch
  headless sessions — sessiond parks its own (§17).

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

Old archive marks — the `archived` tag on the conversation's ABS item, which
is what archiving was before this flag — are carried over once by `media
session-archive-import` (dry run; `--apply` to set the flags). It maps each
tagged item to its session by the folder's `<project>/<title>` tail against
the book-tracks manifests, and only reads from ABS.

#### `POST /session/pin` — gated (22 Sep 2026)

`{"session", "pinned": true | false}` → `{"ok": true, "session", "pinned"}`.

- Keep a session open against the idle reaper ([Resting](#resting)): a
  pinned session is never closed by it. The pin does nothing else — you can
  still close the session yourself — and outlives the session (pin, close,
  resume next week: still pinned).
- Kept here, per session, in `<state_dir>/pinned.json` (`{"<session>":
  <pinned at>}`, the same atomic write-and-lock as the archive flag).
- `pinned` defaults to `true`; anything but a JSON boolean is 400 `"pinned
  must be true or false"`. Setting what is already set is a 200 that writes
  nothing.
- Rows carry it as `pinned` on `/targets` and `/conversations`.
- 400 `"not a session id"`; 404 `"no such session <first 8>"`, as for
  `/session/archive`.
- In `CORS_PATHS`.

Pinned by `packages/server/tests/test_reap.py`.

#### `POST /session/priority` — gated (24 Sep 2026)

`{"session", "level"}` → `{"ok": true, "session", "level", "priority"}`.

What happens to the thread's replies (a Claude Code Stop read-out; its
questions are never touched):

| level | the reply |
|---|---|
| `interrupt` | plays at once, at HIGH priority: another thread's reply steps aside at its next sentence and resumes after |
| `auto` | plays at once: the desk toast does not hold it and a pane or tmux-session mute does not silence it |
| `normal` | the usual rules (the default; nothing stored) |
| `quiet` | rendered and archived unheard (`extras.held`, `unheard: true` on the transcript) and never played by itself |

- `priority` in the answer, and on the rows, is true for interrupt and auto.
  The older body `{"session", "priority": bool}` still works: true is auto,
  false normal; with neither, auto.
- Kept in core (`agent_media_core/speak_priority.py`), per session, in
  `<state_dir>/speak-priority.json` (`{"<session>": {"level", "at"}}`; a
  bare number is the older flag, read as auto), so the hooks read it without
  the server; `media priority [interrupt|auto|normal|quiet|status]` sets the
  same thing from a pane. Outlives the session, like a pin.
- 400 `"level must be interrupt, auto, normal or quiet"`; the other
  refusals and CORS exactly as `/session/pin`.

Pinned by `packages/server/tests/test_reap.py` and
`packages/core/tests/test_speak_priority.py`.

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

**A question** (22 Sep 2026: AskUserQuestion — multi-select, free text,
several questions) takes structured answers, the same shape for a pane and
a headless session:

```json
{"session": "…", "key": "<approval.key>",
 "answers": [{"question_index": 0, "selected": [2]},
             {"question_index": 1, "selected": [1, 3], "other_text": "a newt"}]}
```

`selected` holds option numbers (`questions[i].options[].n`), `other_text`
the "Other" words; every question needs an answer, and a single-select one
exactly one (an option or words). The headless dict form (below, by
question text and label) is accepted on a pane too. A pane is given it key
by key (`asks.drive`), the screen read back after every step: it goes back to
the first tab, picks or ticks (and unticks what is ticked and not wanted),
types words only once the cursor is on the free-text row, moves tab by tab,
and sends from the review page — then waits, as the numbered form does, for
the dialog to go.

- 200 `{"ok": true, "session", "pane", "answers": {"Which colour?": "Blue",
  "Which pets?": "Cat, Fish, a newt"}, "waiting", "approval"}` — `answers` is
  what the agent is handed.
- 400 `"no answer for …"`, `"no option n in …"`, `"… takes one answer"`,
  `"no such question …"` (with `approval`) — nothing was pressed.
- 409 `"the question has changed"` (a `key` no longer on screen) with the
  current `approval`; 409 `"only part of this question is on screen: answer
  it at the desk"` for a pane question with `partial: true` (several tabs and
  no hook copy, or a list scrolled off a short pane).
- 504 `"the question did not take the answer: <step>"` with the `approval`
  now on screen — a step the screen did not follow; whatever keys went in
  stay in (the dialog is not cancelled).
- A number (`choice`) on a question: one single-select question, as before
  (the digit, then Enter); one multi-select question, as `selected: [n]`;
  several questions, 400 `"this question takes answers, not a number"`.
  `answers` on a permission prompt: 400.

**Headless sessions** (22 Sep 2026, §17) take the same numbered form (1 =
Allow, 2 = Deny; a single single-select question by its option's number),
and a structured one:

```json
{"session": "…", "request_id": "<approval.id>", "decision": "allow" | "deny",
 "answers": {"Which fruits do you like?": ["apple", "pear"],
             "What should I call you?": "Something else entirely"},
 "message": "not today"}
```

- `decision` is required unless `answers` is given (then it is `allow`).
  `answers` is for a question: every question answered, by label, a list of
  labels (multi-select, sent to the agent joined with ", "), or free text.
  `message` is what a deny tells the agent (default: declined from the
  phone, do not retry).
- 200 `{"ok": true, "session", "pane": null, "request_id", "decision",
  "waiting": bool, "approval": <the next one> | null, "driver": "headless"}`,
  plus `answers` (as sent to the agent) for a question, and `answered` /
  `label` when answered by number.
- 400 `"decision must be allow or deny"`, `"no such question …"`, `"no answer
  for …"`, `"no option n"`. 404 not live. 409 `"that session is not waiting
  on a question"`; 409 `"the question has changed"` (an `id` no longer
  pending) with the current `approval`; 409 `code: "lost"` when the request
  died with its process — `"that request was lost when the session host
  restarted; send a message to carry on"` (§17). 503 `code: "down"` when
  `media-sessiond` is not running.
- The list form above works here too, with `key` and no `request_id` (the
  request pending now).
- A pane session refuses allow/deny by `request_id`: 400 `"this session
  answers by number (choice and key)"`. Its questions take `answers` (above).

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
 "pos": 12.0, "dur": 40.0, "speed": 1.6, "muted": false, "target": "app" | null,
 "replay": false, "turn": {"at": 1790031449.7, "id": 91},
 "queued": [{"session": "5f8c…" | null, "title": "…", "urgent": false, "at": 1790031449.7}]}
```

- It describes what is **heard**. A reply waiting for the voice (another
  reply or a replay is playing) is not `live`, does not change `session`,
  `sentence` or `pos`, and is listed in `queued` instead.
- `live` = speaking or paused.
- `replay` is true while what is heard is a recorded reply played again (the
  bar's `replay`/`prev`, `replay-id` from a bubble, the desk popup). Its
  `session`, `title` and `sentence` are the replayed reply's. False when not
  live.
- `queued` lists replies said but not yet heard, in the order they are likely
  to play (urgent first, then oldest): `session` (null for a reply with no
  session, e.g. `media say`), `title` (as for the live one; `""` if unknown),
  `urgent` (a question, a permission prompt or `media say --urgent`: it will
  interrupt what is playing) and `at` (when it was submitted, epoch seconds).
  Always present; `[]` when nothing waits. A reply that stepped aside for an
  urgent one is listed while it waits to resume. Replies still rendering and
  replays are not listed. Read from the playback lock's waiter registry
  (`intake.submit.speech_queue`).
- Who waits for whom: an ordinary reply never interrupts — not another
  session's reply, not its own session's earlier one, and not a replay. A
  replay interrupts an ordinary reply, which resumes after it (paused, if it
  was). An urgent reply interrupts either; the reply resumes, the replay
  does not. (`_SpeechPlaybackLock`'s docstring is the full rule.)
- `target` is where the voice is: while live, the reply's own target (from
  its now-playing row — a reply moved mid-way by §6.9 still names where it
  started); when quiet, where the next reply will play. `null` only if the
  host could not say. Names are §6.9's.
- `session`, `title` and `item` are filled only while live and only for a
  valid session id. Title and item are cached per session for 60 s.
- `turn` (23 Sep 2026) names which turn is being spoken, keyed the way the
  log's lines are: `at` is the line's `at`, and `id` the history row on a
  replay. Present only while live; absent when the host could not say.
  It is what makes `pos` usable — a position is worth nothing until you know
  what it is a position *into*. A reader holding a line's `sentences` and
  `offsets` (§6.2, the newest turn carries them with no `live`) matches
  `turn.at` against its line and bolds `pos` against the offsets, on the
  player's own clock. That is the path that survives losing the live row.
- `pos`, `dur` and `speed` may be `null`.

Clients: S (`SpeechBar.vue`). **Polled 1.5 s while speaking, 5 s idle, 15 s
after failures.** The server logs one line per device on the first answer
and on every refusal.

#### `POST /speech/ctl` — gated

`{"action", "arg"?, "sentence"?, "session"?}` → `{"ok": true, "out": "<what media printed>"}`.

Actions (`_APP_SPEECH_ACTIONS`): `toggle`, `skip-`, `skip+`, `para-`,
`para+`, `jump-end`, `prev`, `replay`, `replay-id`, `speed-`, `speed+`,
`speed0`, `vol-`, `vol+`, `mute`, `goto-sentence`.
- `arg` is the turn index for `prev` and `replay` (1 = latest, clamped
  1–999), or a history row id for `replay-id` (not clamped).
- Anything else is 400 `"unknown action"`.

**Read from here** (built 22 Sep 2026). Sentence indices are always the
server's own list, 0-based, never a client's re-split of the text:

- `goto-sentence` jumps the reply being said to sentence `arg` of the live
  message's `sentences` (§6.2 live fields / `spoken.live.sentences`, i.e.
  the now-playing row's `clip_sentences`) and carries on from there
  (`media skip --unit sentence --to N`). `arg` must be a JSON integer
  0–9999, else 400 `"arg must be a sentence index"` (`true`, `"3"` and
  `-1` are refused, not coerced). An index past the end is clamped to the
  last sentence. With nothing speaking or paused it is 409 `"nothing is
  being said"` — it never brings a finished reply back. `session`
  (optional) names the thread the tap was in; if a different session's
  reply is being heard it is 409 `"that reply is no longer being said"`.
  The live fields then follow the new position (the bold moves on the next
  answer; the app moves it at once and lets the next answer confirm it).
- `replay-id` with `sentence` plays history row `arg` starting at that
  sentence of the list `GET /speech/sentences` returns for it — one
  command (`media replay --id ID --from-sentence N`), so there is no
  replay-then-seek race and the sentences before it are never heard. Same
  validation as above (400 `"sentence must be a sentence index"`); clamped
  to the row's last sentence; a row with no sentence map (its clip arrays
  were swept, or the lane kept no timeline) plays from the top. The live
  fields of the replay count from the start of the reply, as if the earlier
  sentences had played, so `sentence`/`offsets`/`elapsed` read as usual.

#### `GET /speech/sentences?id=<history id>` — gated

`{"ok": true, "id": 48213, "sentences": ["First.", "Second.", …]}` — the
sentences a replay of that spoken reply can start at, in the order
`replay-id` + `sentence` counts them (`cli.replay_sentence_map`). `[]` when
it can only be replayed from the top. 400 for an id that is not a number,
404 `"no such spoken reply"` for an id that is not a speech-history row.
Same gate as `/speech/ctl`. Fetched on demand (the app asks when the
listener picks "Read from here"), so the message log does not carry a
second copy of every reply's words.
- `ok: true` means the command ran, not that it did anything — read `out`.
- `error` is added (and `out` starts `error: `) when the verb ran and could
  not do it — a replay whose audio was cleared from the cache, or was
  rendered for another player. Show it; nothing else played instead.
- `replay`, `replay-id` and `prev` can take up to ~8 s longer than before
  (`MEDIA_REPLAY_WAIT_S`): a replay waits for a speaking reply to step aside
  before it plays, rather than playing over it.

Clients: S (`SpeechBar.vue`; `replay-id` from `ConversationLog.vue`);
the chat app (speech bar; `replay-id` from a message's ▶; `goto-sentence`
on a tap in the message being said; `replay-id` + `sentence` from "Read from
here" on an older one).

### 6.6 Harness setup — gated

Getting an agent onto the host and signed in, from the phone.

| Route | Request | Response |
| --- | --- | --- |
| `GET /harnesses` | – | `{"ok", "agents": [{"name", "present", "path", "version", "auth": "in"\|"out"\|"unknown", "account", "actions": ["install", "login", "logout"], "installed_action": "install"\|"update"}]}` |
| `POST /harnesses/run` | `{"agent", "action": "install"\|"login"}` | `{"ok", "pane", "agent", "action", "cmd"}` · 400 unknown agent/action, 409 already running, 503 no window |
| `GET /harnesses/screen?pane=` | – | `{"ok", "pane", "agent", "action", "cmd", "lines": [...], "done": bool, "exit": int\|null}` · 404 not ours, 410 gone |
| `POST /harnesses/keys` | `{"pane", "text"?, "key"?}` | `{"ok", "pane"}` · 400 bad key / nothing to type, 404, 410 |
| `POST /harnesses/close` | `{"pane"}` | `{"ok", "pane"}` · 404 |
| `POST /harnesses/logout` (23 Sep 2026) | `{"agent"}` | `{"ok", "agent", "cmd", "exit", "lines", "auth"}` · 400 unknown agent, 409 no sign-out, 503/504 it would not run |
| `GET /harnesses/updates[?refresh=1]` (23 Sep 2026) | – | `{"ok", "updates": [{"name", "installed", "latest", "behind": bool\|null, "line", "checked_at"}]}` |

Only windows `/harnesses/run` opened can be read or typed into.

`logout` is in `actions` only where `auth` is already `in`: on `out` it would
do nothing, and on the two that answer `unknown` its effect would be
invisible. It opens no window — `claude auth logout` and `codex logout`
delete the stored credentials and exit — so it answers with what the command
said and the state read back afterwards. It is the one call here that takes
something away, including the credential every session the app starts runs
on, so the client asks before making it.

`/harnesses/updates` is the slow half of `/harnesses`, and its own route
because it is the only thing here that goes to the network: the page draws
its rows from `/harnesses` and fills the update state in when this lands.
Installed harnesses only, asked two ways — the three npm packages by
`npm view <pkg> version` against what `--version` says here, and Hermes by
`hermes update --check`, which fetches from its remotes and answers
behind-or-not with no version at all (`line` is what it said). `behind: null`
is "nobody could say" and must never render as up to date. Answers are cached
an hour on the server; `?refresh=1` asks again (the page's ↻, and after an
install window closes).

The client hides the Update button on a row that is `behind: false` — there
is nothing for it to do — and labels it `Update to <latest>` when there is.

Signed out is also enforced where it bites: `POST /ask` refuses a fresh chat
with a harness that is missing or signed out (409, `{"agent", "fix":
"harnesses"}`) rather than opening a window that sits on the harness's own
sign-in screen and never answers. A *resumed* session is not checked — it is
already running. `unknown` is let through.

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

### 6.9 Audio destinations — gated (built 22 Sep 2026)

Where speech and music play, chosen from the app. The choice is kept in
core (`agent_media_core/audio_targets.py`) and is the same one
`media speech-target [NAME | --clear]` shows and sets at the desk.

#### `GET /audio/targets` — gated (`auth.gate`)

```json
{"ok": true, "channels": {
  "speech": {"current": "app", "default": "app", "overridden": false,
             "options": [{"name": "app",   "label": "Phone (Sasonica)",      "available": true,  "why": null},
                         {"name": "phone", "label": "Phone (Termux player)", "available": true,  "why": "was slow or unreachable a moment ago"},
                         {"name": "rooms", "label": "House speakers",        "available": true,  "why": null},
                         {"name": "local", "label": "red5",                  "available": false, "why": "no speech player running on red5"}]},
  "music":  {"current": "phone" | null, "next": "default", "overridden": false,
             "options": [{"name": "auto",  "label": "Automatic", …},
                         {"name": "rooms", "label": "House speakers", …},
                         {"name": "phone", "label": "Phone (Termux player)", …},
                         {"name": "app",   "label": "Phone (Sasonica)", …}]}}}
```

Speech:
- `current` is where the **next** reply plays: the listener's choice if one
  is set and still playable, else `default` (`MEDIA_SPEECH_DEFAULT_TARGET`,
  which `media-lane` may be switching). `overridden` says which. While a
  choice is set, `media-lane`'s switching no longer moves speech; clearing
  it hands speech back to the lane. A choice that stops being playable (its
  config removed) is ignored, never followed.
- `options` lists the targets this host is configured for: `app`, `phone`,
  `rooms`, `local`, plus any name the env configures per target
  (`MEDIA_SPEECH_SOCKET_<T>`, `MEDIA_SPEECH_DEVICE_<T>`,
  `MEDIA_REMOTE_SAY_CMD_<T>`). A target is configured when a reply sent there
  has a route — a remote-say lane, or a device the speech sink can bind.
  The env default and the current choice are always listed.
- `available` / `why` come from local checks only, never a connection: a
  local broker's socket must exist; a tcp bridge is `available` but carries a
  `why` while the mpv breaker has it marked slow. Cached 5 s.

Music:
- `current` is where music is playing **if the host knows it without asking
  a player**: the track the channel is playing is the one `media music play`
  last routed. Anything else (the MCP tool, a play started elsewhere) is
  `null`. `next` is the stored `--where` for the next untargeted play, or
  `"default"`.

#### `POST /audio/target` — gated (`auth.may_control_speech`, like `/speech/ctl`)

`{"channel": "speech" | "music", "target": "<name>" | null}` → `{"ok": true,
"channel", …that channel's block…}`. `null` (or `""`) clears the choice.
400 for an unknown channel, a name that is not a string, or a target this
host cannot play (`error` says which); nothing is stored on a 400.

**When it takes effect.** Speech: from the next reply. A reply resolves its
target once, when it starts, and keeps it — pause, skip and replay follow
its now-playing row to the end — so a reply already speaking finishes where
it is. There is no "move it now": nothing in core can hand a playing reply to
another player cleanly, and restarting it elsewhere is a replay the listener
can already ask for. Replays (`/speech/ctl replay`) play on the new choice;
a reply the phone rendered itself (`clips_remote`) has no audio on this host
to send anywhere else. Music: the next `media music play` given no
`--where` (and shares, which call it). Music that is playing is not moved,
and the MCP `music_play` tool keeps its own default.

The choice is per host, in that host's state dir (`speech-target`,
`music-where`). It matters on the host that starts replies (`origin`); set
on a host that never originates speech, it changes nothing audible.

Clients: S (planned — the speech bar's picker, §14).

### 6.10 Notes — gated (built 22 Sep 2026)

The Org tree (`~/org`, `MEDIA_NOTES_DIR` to move it), browsed, searched and
captured into with no Emacs involved (`notes.py`). The layout is paragtd's:
the GTD files at the top, org-roam notes under `roam/`. The file list and the
capture template are copied from paragtd by hand for now. Commits are not
the server's job: `org-autosync` commits and pushes the tree from every host.
All five routes use `auth.gate`. GETs are gzipped when the client accepts it.

#### `GET /notes`

`{"ok", "root", "views": [{"name", "label", "kind": "agenda"|"file"|"folder",
"path"?, "count"?}]}`. `agenda` comes first. There is one `file` view per
paragtd core file that exists (`count` = open TODO-like headings), and one
`folder` view per roam folder (`count` = notes). `roam-sessions` holds the
agents' own notes, close to a thousand of them.

#### `GET /notes/view?name=<view>[&done=1]`

`{"ok", "view", "items": [...]}`.
- A file view returns its headings as `{path, at, level, state, priority,
  title, tags, scheduled?, deadline?}`. `at` is the 1-based line of the
  heading. DONE/CANCELLED headings are left out unless `done=1`.
- `agenda` returns the headings scheduled or due within 7 days, plus
  overdue ones, each with `date` and `overdue`. An astro alert more than 2
  days past is dropped, matching `paragtd-astro-skip-stale`. Repeaters are
  not expanded, so a routine not yet marked done shows as overdue from its
  first date.
- A folder view returns `{path, title, modified}`, newest first, at most 300.
- An unknown view is a 404.

#### `GET /notes/read?path=<rel>[&at=<line>]`

`{"ok", "path", "at", "title", "text", "links": [{label, path}], "chats":
[{session, title, at}]}`. `text` is raw Org, capped at 256 KB. With `at`,
only the subtree under that heading is returned. `links` resolves the text's
`[[id:…]]` links to paths. `chats` lists the chats started about this item
with `POST /notes/ask`, newest first (at most 20).
- 404 for anything outside the tree, in a dot-dir, a directory, or a file that
  is not `.org`/`.md`/`.txt`.
- 409 when the line at `at` is no longer a heading, meaning the file changed
  under the client. Refetch the view.

#### `GET /notes/search?q=<text>[&all=1][&memory=0]`

`{"ok", "q", "notes": [{path, line, text}], "memories": [{id, user, score,
text}]}`.
- `notes` is a case-insensitive fixed-string ripgrep: at most 3 hits per file
  and 30 in total. It skips `roam/sessions/`, `astro.org` and backups unless
  `all=1`.
- `memories` comes from the memory store (Hippocampus, the `ryer` and `sam`
  namespaces, as `agent-memory-search` uses), searched in parallel. When the
  store is down, the list comes back empty and the notes half still answers.
  `memory=0` skips the store.

#### `POST /notes/capture`

`{"text", "kind": "todo"|"note", "memory": bool}` → `{"ok", "path":
"inbox.org", "at", "kind", "remembered"}`.
- The capture is appended to `inbox.org` under an flock, using paragtd's "t"
  template: `* TODO <first line>`, a `:CREATED:` drawer, then the remaining
  lines as the body. `note` writes a plain heading instead of a TODO.
- A body line starting with `*` is indented one space, so one capture is
  always one entry.
- Unless `memory` is false, the text is also written to the memory store in
  the background, as best effort.
- 400 when the text is empty, 413 when it is over 8 KB.

#### `POST /notes/say` — `auth.may_control_speech`, like `/speech/ctl`

`{"path", "at"?}` → `{"ok", "path", "at", "title", "chars"}`.
- The note, or with `at` its subtree, is spoken through `media say`, the same
  way as a reply.
- The text is cleaned for listening first (`notes.spoken`): drawers,
  keywords and dates are dropped, links become their labels, and each
  heading becomes a sentence ("Todo: Call the bank.").
- It is capped at 6,000 characters.
- 404 and 409 as for `/notes/read`; 422 when nothing readable is left.

#### `POST /notes/ask` — gated (`auth.gate`), like `/ask`

`{"path", "at"?, "text", "agent"?}` → `/ask`'s answer for a new session
(§6.3: `session`, `pane`, `opened`, `fresh`, …) plus `path` and `at`.
- The Organiser's chat box (`notes_chat.py`). A fresh session opens in the
  notes tree itself, so the agent can read and edit the file. No session need
  have run there before: the server chose the directory, not the client.
- Its first message names the item, then the words:
  `About "<title>" in my Org notes (~/org/<path>, line <at>, <STATE>): <text>`.
  A whole note (no `at`) leaves out the line and the state.
- The session is recorded against the item's file and title (not its line,
  which moves), for `/notes/read`'s `chats`. A refile to another file starts
  that list afresh.
- 400 when the text is empty; 404 and 409 as for `/notes/read`.

#### `POST /notes/state` · `POST /notes/refile` · `POST /notes/date` · `POST /notes/priority` — gated (`auth.gate`)

These change a heading in one of the GTD files at the top of the tree:
inbox, next-actions, waiting-for, tickler, someday, projects, areas and
routines. Roam notes cannot be changed here (400). Every file touched is
flocked while it is read and rewritten, the same lock capture takes.

**Finding the heading.** All three routes take the heading as `at` (its line as
the app last saw it) plus `title` (its text).
- If that line still holds that title, it is used.
- If not, the single heading with that title is used.
- If there are none, or more than one, the answer is 409. The app should
  refresh and ask again; the server never guesses.

`/notes/state {"path", "at", "title", "state"}` → `{"ok", "path", "at",
"state", "repeated", "next"?}`
- `state` is one of TODO, NEXT, WAITING, SOMEDAY, DONE, CANCELLED, or `""`
  (no keyword).
- Closing a heading puts `CLOSED: [stamp]` on its planning line, creating
  the line if there is none. Reopening takes the stamp off again.
- A heading whose planning line has a repeater (`+1d`, `++1w`, `.+1m`) is
  not closed. Its timestamps move to the next occurrence, as Org does:
  - `+` moves one interval;
  - `++` moves to the next occurrence after today;
  - `.+` moves one interval from today.

  The heading keeps its state, and the answer says `repeated: true` with
  `next`, the new date.
- `at` in the answer is where the heading is now.

`/notes/refile {"path", "at", "title", "to", "date"?}` → `{"ok", "path",
"at", "to"}`
- `to` is one of next, waiting, tickler, someday, projects or inbox. The
  whole subtree moves, with its heading levels shifted to fit.
- Where it lands follows paragtd's capture templates:
  - `next`: under `* Inbox` in next-actions.org, as NEXT.
  - `waiting`: under `* Waiting` in waiting-for.org, as WAITING.
  - `tickler`: under `* Tickler` in tickler.org, with `SCHEDULED:` set to
    `date` (YYYY-MM-DD, required; replaces any earlier SCHEDULED).
  - `someday`, `projects`, `inbox`: at the top level of their files.
- A missing file or headline is created.
- 400 when the heading is already in the target file.

`/notes/date {"path", "at", "title", "kind", "date", "time"?}` → `{"ok",
"path", "at", "kind", "date", "time"}`
- `kind` is `scheduled` (the default) or `deadline`.
- `date` (YYYY-MM-DD) replaces that stamp's date in place. A repeater on it
  stays. Its time of day stays too, unless `time` is given: `"HH:MM"` sets
  it and `""` drops it.
- With no stamp of that kind, one is added: to the end of the planning
  line, or on a new planning line under the heading.
- An empty `date` takes the stamp off, and the planning line with it if
  nothing is left on it.
- `time` in the answer is the time the stamp now has (`""` for none).
- 400 for a malformed date, time or kind.

`/notes/priority {"path", "at", "title", "priority"}` → `{"ok", "path",
"at", "priority"}` (24 Sep 2026)
- `priority` is `"A"`, `"B"`, `"C"` or `""` (take the cookie off); it is
  written as Org's `[#A]` after the state. 400 for anything else.
- An `[#A]` TODO whose SCHEDULED or DEADLINE has a clock time is read aloud
  at that time (`media agenda-alarm`, agent_media_core `agenda_alarm.py`).

#### `GET /notes/setup` · `POST /notes/setup` — `auth.may_control_speech`, like `/harnesses`

This is the Notes tab's checklist for a host that has no notes yet
(`notes_setup.py`). GET returns `{"ok", "components": [{name, label, state:
"ok"|"missing"|"off"|"down", detail, why, actions, optional}], "search":
"ripgrep"|"built-in"}`. It lists these components:

| name | what it checks | actions |
|---|---|---|
| `org` | the tree, with paragtd's files | `clone` (only when `MEDIA_NOTES_REPO` is set and the folder is empty); `create` (writes any missing paragtd files and roam folders, never overwrites, runs `git init` if the folder is not a repo) |
| `sync` | that `org-autosync.timer` is enabled; needs a git repo with a remote | `enable` |
| `memory` | that the store answers `/health` | none. It runs on the hub and is optional. |
| `paragtd` | the Emacs package and astro generator (`MEDIA_PARAGTD_DIR`, default `~/projects/paragtd`) | `install` (clone plus `bin/bootstrap`), `update`; optional |

POST `{"component", "action"}` runs one action.
- A quick action (`create`, `enable`) is done in place and answers `{"done":
  true, …}`.
- A long one (`clone`, `install`, `update`) opens a background tmux window
  and answers `{"pane", "cmd"}`. The window is registered with the harness
  installs, so the app watches it with the same `/harnesses/screen`,
  `/harnesses/keys` and `/harnesses/close`.
- 400 for an unknown component.
- 409 for an action the checklist does not currently offer; `error` carries
  the reason.

Search works without ripgrep: `search: "built-in"` means a slower Python
scan that follows the same rules.

Clients: none yet (a Notes tab in S is next).

---

### 6.11 Dashboard — gated (built 22 Sep 2026)

#### `GET /dashboard` — gated (`auth.may_control_speech`, like `/targets`)

The app's Home screen in one answer: what needs you, what is working, what
is being said, where each thread was, where a new chat can start, and how the
machines are. Code: `agent_media_server/dashboard.py`. Pinned by
`packages/server/tests/test_dashboard.py`. Gzipped when the caller accepts it.

```json
{"ok": true, "at": 1790053383.513,
 "needs_you": [{"session": "0f1e…", "title": "Sasonica web", "kind": "question",
                "approval": {…the §6.2 approval object…}}],
 "working": [{"session": "01a0…", "title": "Set up websites on red4",
              "current": "cloudflare api execute", "since": 1790045840.684, "count": 15}],
 "speech": {"now": {"live": false, "speaking": false, "paused": false, "session": null,
                    "title": "", "sentence": "", "target": "app", "replay": false},
            "queued": [{"session": "5f8c…", "title": "…", "urgent": false, "at": 1790031449.7}]},
 "recent": [{"session": "5f8c…", "title": "…", "live": true, "at": 1790053201.164, "rested": null,
             "recap": {"text": "…", "at": 1790052694.779, "source": "claude"}}],
 "places": [{"name": "agent-media", "path": "/home/ryer/projects/agent-media", "at": 1790053383.1}],
 "agents": [{"name": "claude", "present": true}, {"name": "codex", "present": true},
            {"name": "pi", "present": true}, {"name": "hermes", "present": true}],
 "hosts": [{"name": "red5", "role": "origin,render", "local": true, "online": true, "last_seen": null,
            "sessions": 3, "mem_used_mb": 5269, "mem_total_mb": 7758, "mem_available_mb": 2489,
            "sessions_mem_mb": 943, "tight": false,
            "reaper": {"mode": "apply", "last_run_at": 1790053206.0, "closed_last_run": 0},
            "shell": {"service": "sasonica-shell", "active": true},
            "sessiond": {"service": "agent-media-sessiond", "active": true}},
           {"name": "hpo", "role": "peer", "local": false, "online": true, "last_seen": 1790000400.1,
            "sessions": null, "mem_used_mb": null, "mem_total_mb": null, "mem_available_mb": null,
            "sessions_mem_mb": null, "tight": null, "reaper": null, "shell": null, "sessiond": null}]}
```

- `needs_you`: every live session `/sessions/state` has in `approval` whose
  dialog could still be read — the approval object exactly as
  `/conversation/log` carries it (§6.2 `approval`: a permission prompt, a
  pane's question form, or a headless request), so the card is answered in
  place with `POST /session/answer`. `kind` is `"question"` when the
  approval's `kind` is, else `"approval"`. A headless row adds `"driver":
  "headless"`. A row the sweep says is on a dialog that is gone by the time
  its pane is read is left out.
- `working`: every live session in `working`, with what its running turn is
  doing from the activity file its hooks append to (the same
  `activity.attach` the log's `working` uses): `current` — the step in
  progress, `""` before the first; `since` — when the turn began (null with
  no turn on file); `count` — steps so far.
- `speech`: `/speech/now` (§6.5) cut to `now` (`live`, `speaking`, `paused`,
  `session`, `title`, `sentence`, `target`, `replay`) and its `queued` rows
  verbatim. It is built over the last speech snapshot this route read, not
  a fresh one: the canvas's snapshot runs `media popup-status`, 1–3 s on red5
  (which is most of `/speech/now`'s own time). Younger than 1.5 s it is used
  as is; older (up to 20 s) it is still used while one background read
  replaces it; past that, or before the first, the request waits for a read.
  So the line can lag the voice by a poll or two — the speech bar's own
  `/speech/now` poll is the live one.
- `recent`: up to 8 `/targets` rows, archived ones left out, newest first by
  `at` — a shelved row's own `at`, else the transcript's mtime (a live row),
  else the recap's. `recap` and `rested` as on `/targets`.
- `project`, `cwd` (22 Sep 2026) on every `recent`, `working` and
  `needs_you` row: as on `/targets` (§6.1), `null` when unknown.
- `places`: `/targets.places`. `agents`: every harness (five since opencode, 24 Sep 2026), `present` =
  installed on this host (a PATH lookup only — `/harnesses` has versions and
  sign-in).
- `hosts`: this host first, then `MEDIA_DASHBOARD_PEERS` (default `hpo`;
  empty for none). Local: `role` is its roles (`config.host_roles`), comma
  joined, `""` when none are declared; memory from `/proc/meminfo`
  (`mem_used_mb` = total − available), `sessions` and `sessions_mem_mb` from
  the `/sessions/state` sweep, `tight` the idle reaper's rule (§6.1
  Resting); `reaper` is the last run in `session-reap.log` (the lines sharing
  its newest stamp: its `mode`, when, how many it closed; all null/0 with no
  log); `shell` and `sessiond` are `systemctl --user is-active` of
  `sasonica-shell` and `agent-media-sessiond` (`MEDIA_DASHBOARD_SHELL_UNIT`,
  `…_SESSIOND_UNIT`), `active` null when systemctl cannot say. A peer is only
  what `tailscale status --json` knows — `online`, `last_seen` (null when
  tailscale never saw it or cannot be asked) — and null everywhere else.
  Nothing here reaches another machine: no ssh, no peer's own answer.

**Cost.** Built from sweeps something else keeps warm: the `/sessions/state`
sweep (3 s), `sessions_index()` (cached here 4 s), `places()`, one pane
capture per session on a dialog, one activity file per working session, the
speech snapshot. The machine part (two `systemctl`, one `tailscale`, the
reaper log's last 16 KB, the harness PATH lookup) is cached
`MEDIA_DASHBOARD_HOSTS_TTL` (15 s). Measured in-process on red5 (22 Sep
2026, 3 live sessions, 6 places, auth stubbed): first call 0.74 s (cold
recaps and transcript lookups), warm 0.084 s, 0.28 s when the state sweep's
3 s cache has lapsed.

Clients: the chat app's Home, polled every ~5 s while visible.

### 6.12 Background agents — gated (built 22 Sep 2026)

The subagents a thread has spawned (Claude Code's Agent / Task tool), and
each one's own turns. Code: `agent_media_server/agents.py`; pinned by
`packages/server/tests/test_agents.py`. Read-only: nothing here writes to a
transcript. Claude Code sessions only (other harnesses answer `[]`).

Claude Code keeps each subagent beside the thread's transcript:
`~/.claude/projects/<proj>/<session>/subagents/agent-<id>.jsonl` (the
agent's own turns, every record a sidechain one) and `agent-<id>.meta.json`
(`{agentType, isFork, description, toolUseId, parentAgentId, spawnDepth,
requestShape, model}`).

#### `GET /threads/{session}/agents` — gated (`auth.gate`, like the log)

```json
{"ok": true, "session": "5f8c…",
 "counts": {"running": 1, "total": 3},
 "agents": [
   {"id": "a788bec427a1f3f12", "description": "Home dashboard + FAB fix, server and app",
    "agent_type": "general-purpose", "is_fork": false, "parent_id": null, "depth": 1,
    "started_at": 1790053121.745, "ended_at": 1790056110.385, "status": "done",
    "current_step": null, "steps": 134, "last_at": 1790056110.224},
   {"id": "a49bff707ac5eb98b", "description": "Build and ship GET /dashboard server",
    "agent_type": "fork", "is_fork": true, "parent_id": "a788bec427a1f3f12", "depth": 2,
    "started_at": 1790053301.492, "ended_at": 1790054032.094, "status": "done",
    "current_step": null, "steps": 34, "last_at": 1790054031.972},
   {"id": "a16b8d9cc8415b2f9", "description": "App: agents strip, sorting, menus",
    "agent_type": "fork", "is_fork": true, "parent_id": "a8f25a5199a6eea1a", "depth": 2,
    "started_at": 1790056789.373, "ended_at": null, "status": "running",
    "current_step": "Add agents to mock server", "steps": 33, "last_at": 1790057005.051}]}
```

- Rows in start order (`started_at`, then `id`); the app sorts them itself.
  `[]` (and zero counts) for a thread with no subagents.
- `id`: the file's `<id>` (`[0-9a-z]{6,64}`). `parent_id`: another row's
  `id` (`parentAgentId`: an agent spawned by an agent), `null` when the
  thread spawned it. `depth`: `spawnDepth` (1 = the thread's own).
  `agent_type`: `agentType`; `is_fork`: `isFork`, or type `"fork"`.
- `started_at`: the Agent call's own record in the parent's transcript (the
  thread's, or the parent agent's), else the agent file's first record —
  not the file's first record by default: **a fork's file opens with a copy
  of its parent's conversation, at the original times**, and those copied
  turns are neither its steps nor its log.
- `steps`: its tool calls since `started_at`. `current_step`: the latest
  one's title (`activity.describe`, as `working.current` words it) while
  `running`, else `null`. `last_at`: its file's newest record.
- `ended_at`: when its terminal record was written; for a `stopped` agent
  with none, its `last_at`; `null` while running.
- `status`, **conservatively** — "done"/"failed" only on the harness's own
  word, "running" only while something says it could be:
  1. **A terminal record**: the newest `<task-notification>` naming it (by
     `<task-id>` = its id, or `<tool-use-id>`), found in the thread's
     transcript or any subagent's — as a user prompt, a `queued_command`
     attachment (a child's lands in its parent agent's file) or a
     `queue-operation` enqueue (the thread's). Only a record whose text *is*
     a notification counts, not one quoted inside other words, and only its
     head (before `<summary>`) is read. `completed` → `done`, `failed` →
     `failed`, `killed` → `stopped`. Failing that, the Agent call's own
     `tool_result`, unless it is the background launch answer (a foreground
     agent's result is its end): `is_error` → `failed` (an interrupt →
     `stopped`), else `done`.
  2. **Resumed after it**: a background agent can be sent another message,
     so one id can notify more than once. When the agent's file has records
     more than `GRACE_S` (5 s) newer than its terminal record, that record
     is old news and rule 3 decides.
  3. **Otherwise** `running` while the thread's session is live (a pane or a
     headless session: a background agent dies with its process) **and** the
     agent's file changed in the last 30 min (`MEDIA_AGENTS_STALE_S`); else
     the terminal status it had, or `stopped` when it never had one (cut off:
     the session ended or was resumed, or it went quiet longer than any tool
     call runs).
- 400 `"not a session id"`; 404 `"no such session"` (no transcript, not
  live, unknown to every harness).

#### `GET /threads/{session}/agents/{id}/log?limit=&before=` — gated

One agent's own turns, as §6.2.2 messages from the same parser (reading
sidechain records): `{"ok": true, "session", "agent": <row>, "messages",
"older"}`. `limit` (default 60, at most 500) and `before` page back exactly
as on `/conversation/log`; `?messages=1` is accepted and implied. The first
message is the prompt it was given; a fork's copied turns are left out
(`older` is then false). `spoken` is always `null`; when the agent is not
`running`, no message's `turn.running` is true. Gzipped when accepted. 404
`"no such agent"`.

**Cost.** Every file (the thread's and each agent's) is scanned forwards
once and then only for its new bytes, cached with (inode, offset, seam) as
in §6.2.2; a line is parsed only when a byte test says it could matter (a
notification, an Agent/Task call or its result, a tool call in an agent's
own file); an unchanged file is a stat. Measured on red5 (22 Sep 2026, a
34-agent thread, 11 MB transcript + 55 MB of agent files, page cache warm):
first call 0.77 s, warm 0.027 s; a 60-message agent log 0.05 s. The thread
stream (§11) counts them on each 1 s / 3 s re-read (a glob and a stat per
file once warm).

Clients: the chat app's thread header strip ("3 running · 12 done") and its
read-only agent view.

### 6.13 The session list as a stream — gated (built 22 Sep 2026)

#### `GET /sessions/events[?ping=<s>]` — gated (`auth.may_control_speech`, like `/sessions/state`)

What a phone's background notifier holds open while the app is closed
(Sasonica Next's `NotifyService`): one connection that says when any live
session changes state, so it can post "New reply · <title>" (`working` →
`waiting`) and "Needs you · <title>" (→ `approval`) without polling. Code:
`agent_media_server/session_events.py`. Pinned by
`packages/server/tests/test_session_events.py`.

```
GET /sessions/events?ping=120
Authorization: Bearer <device token>
Accept: text/event-stream
```

```
retry: 5000

id: 1
event: sessions
data: {"sessions":[{"session":"0f1e…","title":"Sasonica web","state":"working"}],"at":1790053383.513}

id: 2
event: ping
data: {}
```

- `sessions`: the first frame on every connection, then again whenever any
  row's `session`, `title` or `state` changes — the whole list each time
  (it is a few rows), sorted by `session`. `state` is `/sessions/state`'s
  (§6.1): `working` | `waiting` | `approval`; absent means not live. The
  client diffs against what it held; the server keeps nothing per client, so
  a reconnect's first frame is the catch-up (a client that remembers the
  last list across a reconnect sees what changed while it was away).
- `ping`: `{}` after `?ping=` seconds of silence — 15 to 300, default 15,
  clamped. A phone asks for a long one so an idle connection wakes the radio
  only for real changes.
- Auth like `/sessions/state`; `?access_token=` is accepted for a plain
  `EventSource` and never logged (the canvas's request log redacts it). The
  bearer is checked again every 300 s and the stream ends if it is refused
  (a revoked device), so the reconnect gets the 401.
- **Server side.** One watcher for every connection, started by the first
  and stopped by the last, reading `sessions.cached_states()` — the same
  3 s-cached sweep `/sessions/state` uses — every 3 s. So while the app is
  polling the list the stream costs no extra sweep, and while it is closed
  it costs one per 3 s. At most 16 streams (503 `"too many open streams"`);
  each holds one handler thread, and a failed write (at the latest the next
  ping) ends it. Not gzipped: frames are a few hundred bytes.
- Errors before the stream opens: the §4.1 / §9 auth answers. After, none
  in-stream: the connection closes.

### 6.14 Search — gated (built 23 Sep 2026)

#### `GET /search?q=<words>[&limit=][&before=<at>][&tools=1][&memory=0]` — gated (`auth.gate`)

Every thread's messages — both sides; live, closed, headless and archived
threads alike — plus thread titles, recaps and projects, plus long-term
memory as its own section when this host has agent-memory. Code:
`agent_media_server/search.py`; pinned by
`packages/server/tests/test_search.py`. Gzipped when accepted.

```json
{"ok": true, "q": "follow along", "terms": ["follow", "along"], "tools": false,
 "threads": [
   {"session": "5f8c…", "title": "Sasonica rebuild", "project": "p-agent-media",
    "harness": "claude", "live": true, "archived": false,
    "recap": "Wired the follow-along clock…", "at": 1790088100.2,
    "match": {"recap": [[10, 22]]}}],
 "messages": [
   {"session": "5f8c…", "message": "8a1f…-uuid", "role": "assistant",
    "at": 1789971302.844, "kind": "text",
    "snippet": {"text": "…1. My follow-along formula was wrong. It compares…",
                "match": [[9, 21]]},
    "thread": {"title": "Sasonica rebuild", "project": "p-agent-media",
               "harness": "claude", "live": true, "archived": false}}],
 "next": 1789955853.211,
 "indexing": false,
 "memory": {"available": true,
            "items": [{"id": "…", "user": "ryer", "score": 0.61, "text": "…"}]}}
```

- **The query.** `q` is words; a `"quoted phrase"` stays whole. Every term
  must match; the last one matches as a prefix, so results come as you type.
  Case and accents are ignored (FTS5 `unicode61`). `terms` is how `q` was
  read — the app highlights them in the thread after a jump. No words
  (`q` empty or all punctuation): 400 `"nothing to search for"`.
- **`messages`**, newest first, at most `limit` (default 20, at most 100).
  `message` is the §6.2.2 message id (the transcript `uuid` of its first
  record) — the id `/conversation/log?around=` takes. `kind` is `text`, or
  `tool` for a tool step. `snippet.text` is ~14 words around the match with
  `…` where it was cut; `snippet.match` is `[start, end]` character offsets
  of each matched word in it. One hit per message per kind. `next` is the
  last hit's `at` when the page is full (ask again with `before=<next>`),
  else `null`; a `before` that is not a number is 400. With `before`,
  `threads` is `[]` and `memory` is left out: they belong to the first page.
- **`tools=1`** (the app's Advanced setting) adds tool steps: a tool's name,
  its title, its input summary and its result summary (§6.2.2 — summaries,
  never file contents). Without it they are never searched.
- **`threads`**: at most 20, newest first — threads whose title, latest
  recap or project hold every term (case-insensitive substring). Names are
  the thread list's (`/targets`, a 10 s cache) where it has the thread,
  else the index's: a `/rename`, then Claude's `ai-title`, then the first
  prompt. `match` gives offsets per field that matched.
- **`memory`**: only on the first page and unless `memory=0`.
  `{"available": false}` when agent-memory is not installed here or does
  not answer; otherwise `{"available": true, "items": [...]}`, the same
  items as `/notes/search`'s `memories` (Hippocampus, the `ryer` and `sam`
  namespaces). Installed means `agent-memory-search` on PATH or in
  `~/.local/bin`, `~/.config/hippocampus.env` (or `sacred-brain.env`), or
  `HIPPOCAMPUS_URL` / `AGENT_MEMORY_HIPPOCAMPUS_URL` set; answering means
  its `GET /health` within 1.5 s. Checked once a minute. A store that fails
  mid-query gives `items: []` and an `error`.
- **`indexing`**: `true` while some changed transcript was not yet read
  (the first build, or a query that ran out of its 0.6 s catch-up) —
  results may be missing the newest words. Ask again shortly.
- **What is searched.** Claude Code transcripts (every §6.2.2 message:
  text, narration, an ask's question and answer); pi session files (each
  `message` record); Codex rollouts (each `message` item, Codex's preamble
  skipped, and its tool calls); Hermes stores (every profile's `messages`).
  Subagents' own turns are not. **Left out:** threads in an excluded folder
  (`MEDIA_SESSIONS_EXCLUDE_CWD`, default `~/.meridian`: Meridian's pool) are
  never read; machinery — a `claude -p` run whose transcript says
  `entrypoint: sdk-*` (a pipeline's calls, the slash-menu probe) — is
  indexed but never answered, unless sessiond holds it as a headless
  thread (§17).
- **Jumping.** For Claude Code hits, open
  `/conversation/log?session=<session>&around=<message>` (§6.2). Codex and
  pi threads are read from their transcripts now (§6.2.2), but the index
  still names *records* rather than messages (`rec:<offset>`, the pi record
  id), so only a pi hit on the record that opened a message matches; every
  other Codex, pi or Hermes hit says `found: false` and the app places the
  thread by `at` instead.

**The index.** SQLite FTS5 at `$XDG_STATE_HOME/agent-media/search.db`
(`~/.local/state/…`): a `docs` table (one row per message and kind: session,
message id, role, at, byte position, words) behind two external-content FTS
tables (text, tool steps), a `threads` table (cwd, project, names, latest
recap, entrypoint, first and last message) and a `files` table (per file:
inode, size, mtime, the offset read to, the 256 bytes before it, and
`resume` — the offset of the last prompt). A file that grew is re-read from
`resume`, its rows from there on replaced (the turn that was open has
grown); a file that shrank, was replaced or rewritten in place is read
again; an unchanged one is a stat. Hermes resumes from its highest message
id. The canvas builds it on a background thread at nice 15 after start,
then looks for changes every 60 s (`MEDIA_SEARCH_INTERVAL_S`;
`MEDIA_SEARCH_INDEX=0` turns the thread off and leaves it to queries); each
query first catches up changed files, newest first, for up to 0.6 s. A
schema change (`SCHEMA_VERSION`) rebuilds it. By hand:
`python -m agent_media_server.search rebuild | refresh | status | query <words>`.

**Cost** (measured on red5, 23 Sep 2026, into a scratch state dir): the
first build over 4,083 files (611 MB of Claude transcripts — 2,854 of them
Meridian's, skipped — plus 244 pi, 34 Codex and 3 Hermes stores) took
**25 s** at nice 10, peak RSS **56 MB**, and made a **70 MB** index: 1,255
threads, 15,772 message rows, 22,547 tool-step rows. A catch-up with a few
changed files: ~0.14 s. Nothing is held in memory between queries but
SQLite's 4 MB page cache per connection.

Clients: the chat app's search screen (`/find`, the ⌕ beside Show / Sort on
the thread list — search looks through threads, so it lives with the thread
list's own controls; a message hit opens `/t/<session>?at=<message>`). The
screen carries a Show of its own — Show, project and agent, seeded from the
list's filter (Active becomes Everything, since the list only folds archived
rows) and shown on screen, `?show=&project=&agent=`. The server is asked for
everything regardless: the narrowing is applied to the hits, which already
carry `project`, `harness`, `live` and `archived`.

### 6.15 Move a thread to another project — gated (built 23 Sep 2026)

#### `POST /session/move` — gated

`{"session", "project"?, "cwd"?}` → `{"ok": true, "session", "project",
"cwd", "restarted": bool, "pane": null | "%23", "live": bool, "folder":
null | "…", "folder_error"?: "…"}`.

A thread has no project field to set: `project` on a `/targets` row is read
off the directory the session ran in and the shelf folder it is filed under
(§6.1). So a move is three things, in order (`moves.py`):

1. **Filed here** — `<state_dir>/moved.json`, `{session: {project, cwd,
   at}}`. Every later `project` and `cwd` for that thread comes from this
   first, so the thread list, the By-project headings and search follow at
   once.
2. **Its transcript moves** — `claude --resume <id>` only finds a session
   from the directory it ran in (transcripts live under
   `~/.claude/projects/<encoded cwd>/`, every non-alphanumeric character a
   dash), so the file and its `<session>/` sidecar are refiled under the new
   directory. Claude Code only: Codex, pi and Hermes keep their own stores
   and move by (1) alone.
3. **Its folder moves** — the conversation's own files are
   `<project>/<title>` under the Conversations root, so the folder goes
   under the new project and the manifest points at it there (`folder` in
   the answer). The files on disk and the app then agree, and `project_of`
   derives the new project from the folder even without (1). A folder that
   could not move (one of that name is already there) is reported as
   `folder_error` beside a move that otherwise happened — the thread moved
   either way.
4. **A live session restarts there** — the pane is closed and reopened with
   `--resume` in the new directory, in the tmux session that project uses
   (`layout.project_host`). Claude Code cannot change its own cwd mid-
   session, so the conversation continues and the process does not; the
   answer carries the new `pane`. A closed thread opens nothing:
   `restarted: false`, and the next resume lands in the new directory.

Name the destination either way: `project` (its directory is the one that
project's newest conversation ran in, `sessions.project_target`) or `cwd`
(any directory on this host, whose project is whatever the layout calls it).

**409** `"that session is working — stop it first"`: a move restarts the
session, and an interrupted turn loses whatever it had not written. Stop it
(`/session/stop`) and move it then. 400 for an unknown project or a
directory that is not there, 404 when no harness still holds the session,
401 unpaired.

**What the folder move costs.** Audiobookshelf reads a folder that moved as
a *new* item — new id, no progress, the old one left behind (the same reason
`book_tracks.folder_for` keeps its first folder for ever), so a moved
conversation loses its place in the old ABS app. David decided on 23 Sep
2026 that the files and the app agreeing is worth more than progress in an
app on its way out. The title part of the folder is kept exactly as it was:
only the project above it changes.

Clients: the chat app's thread ⋯ menu ("Move to project…") and a long press
on a row in the thread list.

### 6.16 Every harness's conversations — gated (built 23 Sep 2026)

A thread used to be one of three things: a pane with an agent in it, a
session sessiond drives, or a conversation that spoke and so reached the
library. A Codex thread from this morning that said nothing was none of
them, and the app never heard of it. `/targets` now reads **each harness's
own store** as well:

| Harness | Where its conversations are |
| --- | --- |
| claude | `~/.claude/projects/<encoded cwd>/<id>.jsonl` |
| codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<id>.jsonl` |
| pi | `~/.pi/agent/sessions/--<cwd>--/<stamp>_<id>.jsonl` |
| hermes | `~/.hermes/state.db` and each profile's (a table, not a file) |

`harnesses.stored()` (core) is that sweep, and it is **stat-only** — one
`scandir` per directory, no transcript opened, ~4,000 conversations in
0.06 s — so the list can be built on every poll.

- **Every row carries `harness`**: `"claude" | "codex" | "pi" | "hermes"`.
  Live rows and shelved rows gained it too; the app shows it as a chip and
  filters by it.
- **Store-only rows** are shaped like any other closed row, plus `source:
  "store"`. `at` is when the conversation was last written to.
- **The window**: `MEDIA_SESSIONS_STORE_DAYS` (30) back,
  `MEDIA_SESSIONS_STORE_ROWS` (40) per harness — capped *after* the cut, so
  a busy agent cannot crowd out a quiet one. `GET /targets?history=all`
  lifts the window, which is what the app's "Everything" filter asks for.
- **Excluded directories** are the ones the live sweep and the reaper
  already leave alone (`MEDIA_SESSIONS_EXCLUDE_CWD`, default `~/.meridian`):
  a gateway's scratch folder holds thousands of one-shot sessions nobody
  had. `MEDIA_SESSIONS_STORE_EXCLUDE_CWD` (comma-separated, empty by
  default) drops a directory from **the list only** — for a folder whose
  sessions are written by a schedule rather than by a person; one opened
  there by hand is still live, still listed, still reaped. Both are matched
  against each store's own name for the directory, so nothing is opened to
  decide. Codex files by date and says nothing about the directory, so
  neither exclusion reaches it.
- **A row needs a name**: the shelf's, else the agent's own (a `/rename`,
  Claude's `ai-title`, Codex's and pi's thread names), else the first thing
  asked. A transcript with nothing to name it by is left out rather than
  listed as "Conversation 0f3a…". Cached by `(size, mtime)`, so an unchanged
  conversation costs a stat.
- A conversation that is running, or on the shelf, is listed **once** — as
  the live or shelved row it already was.

Not VS Code: it keeps no conversation store of its own on this host.

### 6.17 Alerts — reported by the host, read gated (built 24 Sep 2026)

The watchers on a host (disk, hosts, logins, the memory store, …) report
**what is true now** on every run; the store decides what changed. Code:
`agent_media_server/alerts.py`; producer helper `agent-alert` in agent-config;
proposal `docs/proposals/2026-09-24-alerts-and-digests.md`. Pinned by
`packages/server/tests/test_alerts.py`.

#### `POST /alerts` — the host's own token (`X-Auth-Token`, the amux token) or a paired device

```
{"id": "disk.red5.root", "level": "warn", "title": "red5: / at 93% (7G free)",
 "detail": "…", "fix": "…", "host": "red5", "step": 90, "confirm": 1,
 "every_s": 900, "kind": "status"}
→ {"ok": true, "alert": {…}, "change": "raised", "notify": true}
```

- `id`: `[a-z0-9][a-z0-9._:-]{0,119}`, stable per thing watched.
- `level`: `ok` | `info` | `warn` | `needs`. `kind`: `status` (default; raises
  and clears) or `digest` (a one-off body, kept as the latest per id).
- `change`: `raised` (to warn+), `escalated` (a higher level, or a higher
  `step` at the same one), `eased`, `cleared` (back below warn), `digest`, or
  `null`. `notify` is true for raised and escalated, and for a digest at warn+.
- `confirm: N` holds a raise until N reports in a row; a clear is never held.
- `every_s`: a status alert unheard from for 3× that raises `<id>.silent`.
- A raise files one TODO in `~/org/inbox.org` carrying `:ALERT_ID:`; a clear
  appends `Cleared` and marks a routine one (never acked, never `needs`) DONE.
  `MEDIA_ALERTS_INBOX` points elsewhere, `0` turns it off.

#### `GET /alerts[?open=1]` — gated

`{"alerts": [...], "at"}`: open ones first (worst, then newest); without
`open=1`, then digests and clears from the last 14 days. Each row: `id, kind,
level, peak, step, title, detail, fix, host, every_s, first_seen, last_seen,
changed_at, cleared_at, acked_at, open`.

#### `POST /alerts/ack {"id"}` — gated

Seen it: kept open, not re-notified; a clear leaves its TODO open. 404 for an
unknown id. The next raise forgets the ack.

Not yet: the `alerts` event on `/sessions/events` and Next's Home section
(proposal step 2).

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
| Live updates | poll `/conversation/log` 1–15 s | `GET /threads/{session}/events` (§11) | **built 22 Sep 2026** |
| Stop | none | `POST /session/stop` (§12) | **built 22 Sep 2026**, speech marker included |
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

   `--host` and `--port` set the base, as they do for the amux link.
   Default host (22 Sep 2026): `MEDIA_VISUAL_PAIR_HOST`, else this
   machine's **tailnet IP** (`tailscale ip -4`), else its hostname — a bare
   MagicDNS name (`red5`) is not reachable from Sasonica Next, whose
   network security config allows cleartext only to tailnet addresses
   ("Could not reach http://red5:8781"). Default port `MEDIA_VISUAL_PORT`/8781.
   The app link is printed first, on a line of its own, then the QR; the
   http form last, marked as reference. (The app's pairing screen also
   takes the http form: `server` and `c` from it.)
2. The app redeems it:

   `POST /pair {"code": "…", "device": "Pixel 8a"}` — no auth.

   ```json
   {"ok": true, "token": "<43 chars: secrets.token_urlsafe(32)>",
    "device_id": "d_7f3a09c1b2e4", "name": "Pixel 8a",
    "server": {"name": "red5", "base": "http://red5:8781"}}
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
- A device token carries the rights `may_reply` grants the owner, plus one
  bit of its own: `enrol` (below). Otherwise a single scope in v1. Scopes
  arrive with the hosted tier, if it needs them.

### Enrolling from the app — BUILT 23 Sep 2026

Pairing began as a thing you could only do at the desk, which is the one
place you are not when you want to pair a tablet. A device row carries
`enrol` (default **false**, including for every row paired before the bit
existed), and a device that has it may do from the app exactly what the
shell does: list, mint, revoke.

- `media-visual-canvas pair --device NAME --enrol` mints a code that grants
  it. Without `--enrol` nothing changes, so the shell stays the only way in
  until a device is deliberately given the bit. `devices` marks such a row
  `[enrols]`.
- `POST /pair` now answers with `"enrol": true|false` — what this device
  may do, told to it once, so the app knows whether to offer the screen.

Three routes, all gated by `auth.may_enrol` (the §9 gate, then the bit).
A bearer that is nobody gets **401**; a good token without the bit gets
**403** `{"code": "not_enrolled"}` — the token is fine, the right is not
there, and an ABS login is not a way round it.

```
GET  /devices
  → {"ok": true, "self": "d_7f3a…", "devices": [ {id, name, created,
      last_seen, last_ip, enrol}, … ]}          (never the sha256)

POST /devices/code {"device": "Pixel Tablet", "enrol": false}
  → {"ok": true, "code": "7f3a09c1", "expires": <ts>, "name", "enrol",
     "links": {"app": "sasonica://pair?…", "browser": "http://…/pair?c=…&device=1"},
     "server": {"name", "base"}}
  → 400 {"code": "no_device_name"} for a blank name
POST /devices/revoke {"id": "d_…"}
  → {"ok": true, "id", "devices": [ … ]}   → 404 {"code": "no_such_device"}
```

The minted code is the same code in the same store with the same TTL as the
desk's — `POST /devices/code` is `pair --device` with a device's token where
the shell would be. `links.app` is built from the address the request
arrived on, so the tablet is told where to find the server the phone is
actually talking to.

The name comes from the body here, where at the desk the body's name is
ignored: it is the same rule both times, that whoever is enrolling decides
what the new device is called, and on the couch that is the phone.

A device may revoke itself, and the last enrolled device may throw itself
out. That is not a lockout: `media-visual-canvas devices --revoke` is the
floor under all of this, and the desk can always mint again.

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

## 11. v1: the per-thread stream — BUILT 22 Sep 2026

Code: `packages/server/src/agent_media_server/thread_events.py`, routed in
`app.py`. Pinned by `packages/server/tests/test_thread_events.py`.

```
GET /threads/{session}/events
Authorization: Bearer <device token>
Accept: text/event-stream
```

**Auth over SSE.** The browser's `EventSource` cannot set headers, so
clients use a fetch-based reader that can (e.g.
`@microsoft/fetch-event-source`; Capacitor's WebView supports streamed
fetch). `?access_token=` is accepted as a fallback for a plain
`EventSource`. It is never logged: nothing prints the query, and the
canvas's request log (`MEDIA_VISUAL_DEBUG=1`) redacts it. Gated by
`auth.gate` like every app route (a device token, or an ABS bearer).

**Frames.** Named events (`event: <type>`), JSON `data`, a per-connection
increasing `id`, and `retry: 2000` first.

| Event | Data | When |
| --- | --- | --- |
| `snapshot` | `{"ok": true}`, the `/conversation/log?session=` envelope — `messages` (the newest **30**, or `?limit=`), `older`, `lines` (deprecated; only those of the page, below), `pending`, `working`, `approval`, `suggestion`, `recap` — plus `{"state", "session_live": bool, "live": bool (deprecated alias of `session_live`), "pane", "resumable"}`, and (22 Sep 2026) `"agents": {"running", "total"}` (§6.12), `"project"`, `"cwd"` (§6.1) | first frame on every connection |
| `message` | `{"op": "append" \| "replace", "message": <message>}` (§6.2.2) | a message appears, or one (matched by `id`) changes: grows a part, a tool finishes, its turn ends, it gains or loses `spoken` |
| `live` | `{"id", "at", "sentences", "sentence", "offsets", "elapsed", "server_time", "delay", "paused"}` — the message being spoken, by `id` — or `null` when nothing is | a new reply starts, the sentence changes, pause or resume, or the clock jumps (more than 1 s from where it should be: a skip) — **not** at 1 Hz; the client runs the clock between frames |
| `working` | the `working` object, or `null` | a step starts, or the turn ends |
| `approval` | the `approval` object, or `null` | a dialog appears, changes or goes |
| `suggestion` | `{"text": "…"}` | the ghost or follow-up arrives or clears |
| `state` | `{"state": "working" \| "waiting" \| "approval" \| "ended", "session_live": bool, "live": bool (deprecated alias), "pane"}` | the session changes state; `ended` when its pane goes |
| `pending` | `{"pending": bool}` | it changes — the log's rule: a live session whose last message is the listener's, the turn working (a step running, or a headless session `working`), or the last line the listener's. Set from the transcript as soon as a prompt lands; cleared by the 1 s / 3 s re-read |
| `recap` | `{"text", "at", "source"}` or `null` | a newer recap is written |
| `agents` | `{"running", "total"}` — the thread's background agents (§6.12) | an agent starts, ends or resumes (checked on the 1 s / 3 s re-read) |
| `ping` | `{}` | 15 s of silence |

`isRunning` is `state == "working"`, `pending`, or the last message's
`turn.running` (§14).

**`live` means two things — fixed by a rename.** In the snapshot and the
`state` event, `live` was "the session is running"; the `live` *event* is
the follow-along clock. The first is now **`session_live`**. `live` stays on
both, with the same value, **for one release** (deprecated 22 Sep 2026) and
then goes; read `session_live`.

**The page.** `GET /threads/{session}/events?limit=N` (1–500, default 30)
sets how many messages the snapshot carries. Older ones come from
`/conversation/log?session=&messages=1&before=<the first id held>`. The
snapshot's `lines` are cut to the same page (those said no earlier than 5 s
before its first message, plus a live one) whenever `older` is true.
Measured on red5's busiest thread (22 Sep 2026): `lines` were 184 KB of a
268 KB snapshot.

**Compression.** With `Accept-Encoding: gzip` (not `q=0`) the stream is
gzipped: `Content-Encoding: gzip`, `Vary: Accept-Encoding`, still
`Cache-Control: no-store` and `X-Accel-Buffering: no`, no Content-Length.
One gzip member per connection, sync-flushed after every frame (`retry`,
each event, each ping), so every event reaches the client whole and at once
— a fetch-based reader sees plain text (the browser/WebView inflates it).
Without the header, plain as before.

**Size**, measured against red5's live canvas, read-only (22 Sep 2026): the
first frame on the connection (wire bytes up to the end of the snapshot).

| Thread | before (60 messages, all lines) | 30, plain | 30, gzip | `?limit=60`, gzip |
| --- | --- | --- | --- | --- |
| busiest, 10 MB transcript (lines 184 KB of it) | 268.6 KB | 49.8 KB | **13.0 KB** | 28.7 KB |
| tool-heavy, 14 MB transcript (47 messages in the tail) | 238.7 KB | 166.2 KB | **48.1 KB** | 70.4 KB |

The rest of a tool-heavy page is its tool summaries (§6.2.2), which gzip
takes to about a third.

**Applying events.** Keep messages by `id`. `append` adds at the end —
unless the `id` is already held, which can happen in the moment between a
snapshot and the first event: then it replaces. `replace` replaces in place;
**a `replace` for an `id` not held is ignored** — the watcher watches the
newest 60, the snapshot is 30, so it can name a message the client has not
paged back to (and when it has, it holds it, and the replace applies). A
replace never arrives for a message older than the newest 60.

**Reconnection.** The server keeps no per-client history. On every
(re)connect it sends a fresh `snapshot`, and `Last-Event-ID` is accepted
but ignored. A client replaces its thread state with each snapshot. A
client that falls 256 events behind is disconnected, and its reconnect's
snapshot is the catch-up.

**Server side.** One watcher per *subscribed session*, fanning out to that
session's subscribers, started by the first and stopped when the last
leaves. Its baseline is read before the first subscriber's snapshot, so a
change that lands between the two is sent (at worst twice), never lost.

- **The transcript** is stat-polled every 0.3 s (stdlib only, no inotify).
  When it grows, the watcher waits for the writes to settle (0.2 s quiet,
  at most 1 s from the first), reads the new bytes only (§6.2.2's cache)
  and sends the messages that appeared or changed. Measured on red5 with a
  copy of an 11 MB transcript, default intervals, in-process server: **append
  → `message` event median 0.33 s, max 0.5 s** (20 appends). What it
  replaces was a 1–15 s poll of speech history, which had the reply only
  once it was being spoken.
- **Everything else** — speech (which message is spoken, and the live
  one), working, the dialog, the suggestion, the state, the recap — is
  re-read every 1 s while a turn is working or speech is live, and every 3 s
  otherwise, and each is sent only when it changed (a clock field alone,
  `server_time`, is not a change).
- **Held threads.** Each connection holds one of the canvas's handler
  threads (ThreadingHTTPServer). Every write that fails ends it, and the
  15 s ping means a vanished client is noticed within one; its watcher stops
  when it was the last.
- **Caps:** 8 subscribers per session, 32 streams in total; over either,
  503 `{"ok": false, "error": "too many open threads"}`.
- The canvas's own `/events` (§7) is separate and unchanged.
- **Headless sessions** (§17): the messages come from the transcript
  exactly as above (Claude Code writes it under `-p` too). `state` and
  `approval` come from `media-sessiond` rather than a screen, and the
  watcher also polls sessiond's event counter every 0.3 s: a new event (a
  turn starting, a permission request, a result) triggers the full re-read
  at once instead of at the next 1 s / 3 s tick. `state` is `ended` while
  the session is parked or closed.

**Errors before the stream opens:** 401/403 (auth, the §4.1/§9 answers), 400
`"not a session id"`, 404 `"no such session"` (not live, no transcript, no
manifest). After it opens, errors are not reported in-stream — the
connection closes, and the reconnect's `snapshot` (or its 404) says what
happened. However the stream ends — the client going, falling 256 behind,
a failed snapshot, a bug — the request counts as answered: nothing further
(no 404) is ever written onto that socket.

**The thread list** stays polled (`/sessions/state` at 5 s, `/targets` on
open) in the app. `/sessions/events` (§6.13, 22 Sep 2026) streams the
list's states, for the phone's background notifier; it reads the same
cached sweep rather than watching every pane itself.

### Deviations from the 21 Sep draft

- `line` events are **`message`** events carrying §6.2.2 messages, matched
  by `id` rather than `at`. Lines still ride in the snapshot, deprecated,
  and are not streamed.
- `live` names its message by `id` (and still carries `at`), and is `null`
  when speech stops.
- A `recap` event was added.
- The transcript is watched at 0.3 s (plus a 0.2 s settle) rather than
  re-read at 1 s / 3 s; the 1 s / 3 s re-read covers the rest.
- 404's words are `"no such session"`, and a session with a manifest but no
  transcript and no pane still streams (its lines).

---

## 12. v1: stop — BUILT 22 Sep 2026

Code: `agent_media_server/stop.py`, the drivers' `interrupt`
(`driver/pane.py`, `driver/headless.py`), `speech.stop_speech`, and the
per-session marker in core (`intake/submit.py`:
`request_session_speech_cut`, `end_session_speech_cut`,
`session_speech_cut`). Pinned by `packages/server/tests/test_stop.py` and
`packages/core/tests/test_session_speech_cut.py`. As built:

- **Interrupt** goes through the driver that owns the session: a headless
  session gets the `interrupt` control request (its receipt names queued
  messages, which then run as their own turn); a pane gets Escape, only
  while it is `working`, watched for up to 3 s (504 `"still working after
  Escape"`). Codex panes are Escaped too; pi and Hermes answer
  `interrupted: false, "why": "not supported for pi"`.
- **The clip** is stopped only when what is heard is this thread's (the
  canvas's speech snapshot names the session): `SinkSpeech().stop` on the
  player it is playing on, as `media stop` does.
- **The marker.** One file per Claude session
  (`$XDG_STATE_HOME/agent-media/speech-cut/<sha1 of the session id>`, JSON)
  with up to two stamps, both the press time:
  - `after` — set just **before** the interrupt, so a reply the turn
    finishes while the Escape lands is already behind it; taken back if
    nothing was interrupted (504, or `interrupted: false`), since that
    turn's reply is still worth hearing. A second press in the same exchange
    keeps the first stamp. It ends when the session next submits a listener
    turn: the `UserPromptSubmit` hook (the desk, and headless sessions,
    whose hooks run under `-p`) and every message the server sends
    (`send._record_turn`, all drivers and harnesses). Backstop:
    `MEDIA_SPEECH_CUT_TTL_S` (1800 s) after it was set, for a harness whose
    desk-typed turns nothing reports (Codex, pi, Hermes).
  - `all` — every reply of this session submitted up to the stamp and not
    yet heard. One-shot by construction: later replies have later stamps.
- **The checkpoint** is the global flush's: after the reply has the playback
  token and any hold is over, just before its first clip. A reply stopped
  there — queued behind another session, still rendering, or waiting out a
  hold — is not played, and still writes its history row with
  `extras.flushed: true`. `all` is also checked **between clips** of the
  reply playing (both the per-clip loop and the phone playlist's), so the
  rest of the reply the stopped clip belonged to does not start up again.
  `after` never cuts a reply mid-play. Keyed by the Claude session id
  (`source_session`), not the pane, so another thread — even in the same
  tmux session — is never touched. `media say --supersede`'s marker is the
  model and is unchanged; `request_speech_flush()` (global) is unchanged.
- **`flushed`** in the response is the number of this thread's replies
  waiting for the voice at the press (`speech_queue()`) that the `all` cut
  dropped; replies still rendering are dropped too but not counted.
- `speech: "silence"` sets `all` even when another thread is the one
  heard (its clip is left alone), and `after` too when the turn was working.
- `state` afterwards is the driver's (`ended` for a session not running).
- In `CORS_PATHS`.

Response as built: the specification's below, plus `"flushed": <int>`;
`cutoff` is the `after` stamp when an interrupt landed, else `null`;
`speech: "stopped"` whenever this thread's speech was stopped or its queue
dropped.

The specification, as it was written:

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

`convertMessage(message) → ThreadMessageLike` (22 Sep 2026: from §6.2.2
messages; lines are deprecated):

| ThreadMessageLike | From the message |
| --- | --- |
| `id` | `message.id` — stable as the message grows |
| `role` | `message.role` |
| `createdAt` | `new Date(message.at * 1000)` |
| `content` | the parts, in order: `text` → `{type: "text", text}` (Markdown — render with `MarkdownText`; canvas markers already removed); `reasoning` → `{type: "reasoning", text}` (a redacted one → a collapsed "Thought" with no text); `tool` → `{type: "tool-call", toolCallId: tool_use_id, toolName: name, args: {summary: input_summary, title}, result: result_summary}` (no `result` while `status == "running"`; `isError` when `error`); `ask` → `{type: "tool-call", toolName: "AskUserQuestion", toolCallId, args: {questions: ask}, result: answer}`; the pictures (below) |
| `status` (assistant) | `{type: "running"}` while `turn.running`, else `{type: "complete"}` |
| `metadata.custom` | `{command, spoken: {id, key, figure, live}}` for the replay button, follow-along and slash-command chip components |

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
| `messages` | `/conversation/log` poll (`?session=` and `messages` since 22 Sep 2026) | `snapshot` + `message` events (§11), **built 22 Sep 2026** |
| `isRunning` | `pending` | `state == "working"`, `pending` (the snapshot's, then `pending` events), or the last message's `turn.running`. Note that assistant-ui disables the composer while running, but a Claude Code session takes messages mid-turn (they queue), so the app passes sends through while running |
| `onNew` in a thread | `POST /reply {item, text}`; a thread not on the shelf yet has no item, so it goes through `POST /ask {text, target: session}` | `POST /reply {session, text}` — **available since 22 Sep 2026**, for every thread, shelved or not |
| `onNew` in a new thread | `POST /ask {text, target: "new", cwd?, agent?}` | same; the returned `session` becomes the thread id |
| `onCancel` | — (gap) | `POST /session/stop`; a second cancel within 5 s sends `speech: "silence"` |
| `onEdit` | not supported — a transcript cannot be truncated. Nearest: `POST /reply {mode: "branch", quote}` as a "branch from here" action | same |
| `onReload` | not supported | not supported |
| `suggestions` | `suggestion` → one suggestion | `suggestion` event |
| `adapters.attachments` | — (gap) | — (gap) |
| `adapters.speech` | not used — speech is agent-media's, rendered by a custom speech bar on `/speech/now` and `/speech/ctl` | same |

**The speech bar's destination picker.** The bar shows `/speech/now.target`
by its §6.9 label ("Phone (Sasonica)", "House speakers"); tapping it opens a
sheet from `GET /audio/targets` — speech options, with unavailable rows
greyed and their `why` beneath — and a pick is `POST /audio/target`. While a
reply is live the sheet should say the change applies from the next reply.
This picker chooses among agent-media's players; it is not the phone's own
output route (see §16).

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

An approval with `kind: "question"` (a pane's AskUserQuestion, or a
headless one) is a card built from `questions`: tap an option for a
single-select, checkboxes (starting from `checked`) and a Send for a
multi-select, an "Other" field per question, one Send for several
questions; answer with `POST /session/answer {session, key, answers:
[{question_index, selected, other_text?}]}` (plus `request_id` for a
headless one, optional). On 409, re-render from the returned `approval`;
`partial: true` still means "answer at the desk" (`/focus`). A pane's
pending ask has no `ask` part yet (it reaches the transcript when answered),
so the card stands at the end of the thread; a headless one attaches to its
running `ask` part by `tool_use_id`.

A **headless** session's approval (§6.2, headless form) is structured: render
`kind: "tool"` as the tool call (`tool`, `input_summary`, `input`) with
Allow/Deny, and `kind: "question"` from `questions` with multi-select and an
"Other" free-text field; answer with `POST /session/answer {session,
request_id: approval.id, decision, answers?, message?}`. No desk needed.

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
| Live thread updates | **built 22 Sep 2026** (§11), with messages from the transcript (§6.2.2). Left: the app side, and transcript parsers for Codex, pi and Hermes |
| Stop | **built 22 Sep 2026** (§12), with the per-session speech marker in core (`after` / `all`): the cutoff, and this thread's queued replies dropped |
| Machine-readable error codes | specified (§13), not built |
| Archive / unarchive a thread | **built 22 Sep 2026**: `POST /session/archive` and `archived` on `/targets` rows (§6.1, §6.4), a server-side flag in `<state_dir>/archived.json`. Left: the app side, and moving any existing ABS `archived` tags over (not done — the tag and the flag are independent until then) |
| Session memory on the phone | **built 22 Sep 2026**: `mem_mb` per `/sessions/state` row and its `host` block (§6.1). Left: the app side |
| Delete a thread | none, and deliberately not proposed: transcripts are the harness's. Needs a decision |
| Attachments (a photo, a file) | none. `/reply` is text only. Needs an upload route and a way to hand a file path to the harness |
| Answer a multi-select or free-text ask from the phone | **built 22 Sep 2026** for both: headless (§17) and panes (§6.2 question form, §6.4 — `answers` given key by key, the screen checked after each). Left: a list scrolled off a short pane, and several tabs when the hook did not keep the question, still go to the desk |
| Edit / regenerate | not possible with the harnesses; `branch` is the substitute |
| A thread-list stream | deliberately deferred (§11) |
| Offline reading and downloads | none. ABS provided them for audio; the new app needs its own cache of the log (and of speech clips, if listening offline matters) |
| Thread search | none. ABS search did it. `/targets` covers the latest 40 only |
| Push notifications (a session waiting on you) | none. Matrix or FCM; out of scope here |
| Choose where agent-media plays (phone / house speakers / host) | **built 22 Sep 2026** (§6.9). Left: the app's picker (§14), and moving music that is already playing |
| The phone's own output (earbuds / speaker / Cast) | none here, and not the server's: that is Android's route for whatever the app plays, so it is an app-side feature for the Capacitor build (an output switcher / `MediaRouter`), separate from §6.9 |

---

## 17. Drivers and headless sessions — BUILT 22 Sep 2026, behind `MEDIA_HEADLESS` (off)

Code: `agent_media_server/driver/` (the seam, `pane.py`, `headless.py`),
`sessiond.py`, `permissions.py`, `stop.py`. Proposal:
`proposals/2026-09-22-headless-sessions.md`; measurements:
`notes/2026-09-22-headless-spike.md`. Pinned by `test_driver.py`,
`test_headless.py` (a real in-process sessiond against
`tests/fixtures/fake_claude.py`, which speaks the spike's recorded
envelopes) and `test_stop.py`.

**The Driver seam.** The routes that act on a session — `/reply`, `/ask`
(fresh sessions), `/session/answer`, `/session/resume`, `/session/close`,
`/session/stop` — gate as before, then ask which driver owns the session:
`start`, `send`, `resume`, `interrupt`, `answer`, `close`, `state`,
`approval`. The **pane** driver is the code these routes always ran, called
unchanged. The **headless** driver talks to `media-sessiond`. A session is
headless when sessiond has a record of it (`<state>/sessiond/<session>.json`,
read from disk, so a headless thread stays headless — and answers 503 —
while sessiond is down); anything else is a pane session. With the flag
off every lookup is the pane driver and nothing reads sessiond's records.

**Which chats are headless.** With `MEDIA_HEADLESS=1` on the canvas's host:
every fresh Claude session `/ask` starts (§6.3). Replies, resumes and
branches of a headless thread stay headless. Desk sessions, Codex, pi and
Hermes, and every pane session stay in panes. There is no handoff between
the two yet (proposal §7).

**media-sessiond** (`media sessiond`; unit template
`packages/core/services/media-sessiond/`, installed only where
`MEDIA_HEADLESS` is set) owns the `claude -p --input-format stream-json
--output-format stream-json --verbose --permission-prompt-tool stdio
--session-id <id>` processes, so they survive canvas restarts. It speaks
JSON lines on `$XDG_RUNTIME_DIR/agent-media/sessiond.sock` (0600, in a 0700
dir; `MEDIA_SESSIOND_SOCKET`). Children get the subscription login
(`ANTHROPIC_*` stripped), no `TMUX*`/`HERDR*`/`CLAUDE*` (bar
`CLAUDE_CONFIG_DIR`), and `MEDIA_SOURCE_KIND=headless` +
`MEDIA_SOURCE_WORKSPACE` (the tmux session a pane would have opened in). It
writes nothing to tmux.

- **State from events**, never from sends: `system/init` or
  `command_lifecycle started` → working; `can_use_tool` → approval;
  `result` → waiting. Every message carries a client uuid (the interrupt
  receipt and the lifecycle name it). Events are kept per session (in memory
  and `<id>.events.jsonl`, trimmed) with a counter the thread stream polls.
- **Parking.** Waiting for `MEDIA_SESSIOND_IDLE` (1800 s) → stdin closed,
  process gone, record `parked`; the next message resumes it with
  `--resume`. Never parked while working, on an approval, or with queued
  messages. At most `MEDIA_SESSIOND_MAX` (4) live; a fifth parks the least
  recently used idle one, else 503 `busy`.
- **Restarts.** A sessiond restart ends its children. A permission request
  pending then is not re-sent by the CLI on `--resume`, so it is recorded
  under `lost`: the thread shows no approval, `/session/answer` for it is
  409 `code: "lost"`, and the next message resumes the session (the model
  sees its tool call interrupted and asks again if it still wants it). At
  start-up any child a crashed instance left running is terminated.
- **Permissions** (`MEDIA_HEADLESS_PERMISSIONS`, per host): `strict`
  (default) keeps every settings source — hooks, speech — and adds a
  `--settings` overlay whose `ask` rules name every tool but a read-only list
  (`Read`, `Glob`, `Grep`, `LS`, `NotebookRead`, `TodoWrite`, `WebSearch`,
  `ToolSearch`, `Skill`, `BashOutput`) **and mirror each user and project
  `allow` rule** (`Bash(*)`, `Write(*)`, …), plus `--permission-mode default`.
  Ask beats allow in Claude Code, so everything else becomes an approval on
  the phone. `normal` drops the overlay: the user's own settings decide, and
  whatever still asks comes to the phone. Verified end to end on 22 Sep 2026:
  a project `allow: Bash(*)` did not pre-approve a Bash call under strict.
- **Speech**: the hooks run under `-p`, so a headless reply is spoken by the
  Stop hook like a pane's, with `source_session` set, `source_pane` empty,
  and (since the hook honours `MEDIA_SOURCE_WORKSPACE`) the same
  `source_tmux_session`, voice and mute as a pane in that workspace.
- **Other settings**: `MEDIA_HEADLESS_MODEL` (`--model`),
  `MEDIA_SESSIOND_CLOSE_GRACE` (10 s), `MEDIA_SESSIOND_CLAUDE` (the binary),
  `MEDIA_HEADLESS_EXTRA_ARGS` (more flags; debugging and smoke runs).

`claude agents --json` lists headless sessions too (seen in the smoke run);
nothing here reads it.

## 18. Layouts — BUILT 22 Sep 2026

Where a session started or revived from the app opens depends on the host's
desk, and `agent_media_core/layout.py` is the one place that answers it:

```toml
# ~/.config/agent-media/config.toml (top level)
layout = "default"          # or "projects-per-tmux-session"
```

Precedence: `MEDIA_LAYOUT` → the file → detected. Detection says
`projects-per-tmux-session` only when `~/.amux/sessions/` exists **and** the
hook that files panes into project sessions is installed (a SessionStart
`tmux-organise-panes` entry in Claude Code's settings, or tmux-claude-resume
in the tmux config); anything else is `default`. `media-setup init` writes
what it detected (`--layout` to choose), `media-setup layout [--set …]` says
which is active and why, and `media selfcheck` / `media doctor` report it
(`layout=`, `layout_why=`).

| Question | `projects-per-tmux-session` (David's) | `default` |
| --- | --- | --- |
| Fresh chat, no place (`/ask`) | amux `scratch` registration: its dir and flags, tmux `amux-scratch` | home, tmux `sasonica`, no flags |
| Fresh chat in a `cwd` place | tmux session = the folder's basename | tmux `sasonica` |
| What `project` names | a series = the tmux session it ran in (`p-<name>`) | a `/targets` place, by folder basename |
| Revived / branched window | the tmux session with a client attached (`MEDIA_REPLY_TMUX`) | `sasonica` (`MEDIA_REPLY_TMUX`), client held |
| Client held on the target | yes, for a named target | yes, always `sasonica` |
| New window moved by a SessionStart hook | yes (expected, harmless) | no |
| Headless workspace (filing, voice) | the tmux session a pane would have used | the folder's basename (`sasonica` for home) |
| Series for a chat filed under `sasonica` | the tmux name | the transcript's folder |
| Series from an encoded `~/projects/<x>` path | `p-<x>` | `<x>` |

With `MEDIA_HEADLESS` on, `default` needs no tmux at all for app chats; a
pane is opened only for a harness without a headless driver (Codex, pi,
Hermes) or a revive of a pane session. `MEDIA_ASK_TMUX` / `MEDIA_ASK_CWD` /
`MEDIA_ASK_FLAGS` override the fresh target in both layouts. Nothing on the
wire changes: `tmux` in an `/ask` answer names whichever session was used.

---

## 19. Reaching the server — the three shapes (23 Sep 2026)

The phone always dials out; the server is the only side that needs an
address. How it gets one is a **deployment choice, not a code path**: the
server already takes its public identity from the request it answered
(`Host` as sent, `https` when `X-Forwarded-Proto` says so — §9), and the
pairing link carries whatever base URL the desk hands it. All three shapes
below run the same binary with the same auth.

David, 23 Sep 2026: the tailnet is the development path, not the product —
"asking a user to stand up a mesh VPN before they can chat is a dead
product". But it is also not the only alternative to a tunnel: red5 happens
to be a Hetzner box with a public IPv4, which most people's server is not.
So all three are supported and documented; none is assumed.

| | **A. Reverse proxy** | **B. Outbound tunnel** | **C. Tailnet only** |
| --- | --- | --- | --- |
| Needs | a public address or a forwarded port, a DNS name, a proxy you already run | an account with a tunnel provider (`cloudflared`, `tailscale funnel`) | every client on the mesh |
| Inbound firewall hole | yes (443) | **no** | no |
| TLS | the proxy's cert | terminated at the provider's edge | none — plain http on the mesh |
| Server's IP visible | yes | no | mesh only |
| Extra daemon | no (the proxy exists) | yes | no |
| Who it fits | a VPS (red5) | a laptop or NUC behind a router — **most people** | anyone already on a mesh — offer it first, assume it never |

**From the app, all three are one thing: a base URL.** The app has no
transport setting and must never grow one — it stores the address the
pairing link carried (§9) and talks https or http to it. So "configurable
from the app" is already the design; what is missing is a way to *enter* an
address without a pairing command run at a desk. That gap is the setup flow,
not the transport.

### A. Reverse proxy (red5 today)

red5 has a public IPv4 and `caddy.service` already terminating TLS for the
Matrix vhosts, so a tunnel would add a hop and a dependency for nothing.
One vhost is the whole change:

```caddy
app.ryer.org {
	reverse_proxy 100.103.43.93:8781
}
```

**The upstream is the tailnet IP, not loopback.** The canvas binds the
Tailscale address on red5 (§2) — `reverse_proxy 127.0.0.1:8781` fails with
a connection refused. Caddy sets `X-Forwarded-Proto: https` itself, which is
what makes `POST /pair` hand the app an `https://` base (§9).

### B. Outbound tunnel

`cloudflared tunnel --url http://100.103.43.93:8781`, or a named tunnel with
a config file, gives a public hostname with no inbound hole and no IP
disclosure. This is the shape to document first for other people: it is the
only one that works unchanged behind CGNAT or a router nobody can configure.
The tunnel must terminate TLS and forward `X-Forwarded-Proto` (both
Cloudflare and `tailscale funnel` do). Device tokens travel over https only.

### C. Tailnet only

What ships today, and the shape to offer **first** to anyone who already has
a mesh (David, 23 Sep 2026): it is the least work and the least exposure.
It is simply not one a stranger can adopt, so it cannot be the only one.

**Correction to §9 (23 Sep 2026).** §9 says Sasonica Next's network security
config "allows cleartext only to tailnet addresses". It does not:
`android/app/src/main/res/xml/network_security_config.xml` is
`<base-config cleartextTrafficPermitted="true">` with no domain rules — in
Sasonica Next, sasonica-chat and sasonica alike. So the
"Could not reach http://red5:8781" that produced that note was **name
resolution** (a bare MagicDNS name needs the tailnet's DNS), not a cleartext
refusal. Consequence, and it is the useful half: a plain-http server on a
home LAN (`http://192.168.1.10:8781`) is **already allowed**. There is no
app-side blocker to shape A or B, and none to a LAN-only install either.

### Before anything is exposed: the amux token

Opening the canvas port publicly puts §4.2's desk routes there too, and
`/input` types keystrokes into live agent panes. What holds today:

- `MEDIA_VISUAL_TRUST_TAILNET` is **not set on red5** (checked 23 Sep 2026),
  so the token is enforced. That flag drops the check *entirely*, including
  `/input` — it must never be set on a host reachable from outside the mesh.
  A `media doctor` check for "trust-tailnet on + a proxy in front" is owed.
- The token is `~/.amux/auth_token`, 43 chars of `token_urlsafe(32)`, so
  entropy is not the problem.

What is owed before exposure:

- **Constant-time compare.** `_authorized()` in `canvas.py` uses `got ==
  token`; `devices.py` already uses `hmac.compare_digest`. Same fix, same
  file.
- **A rate limit on the desk routes.** `POST /pair` has one (10 failures per
  source per 10 min, §9); `/input` and `/ctl` have none.
- **Revocability.** One static string shared by every browser that ever
  paired, with no listing and no revoke — unlike device tokens (§9), which
  have both. The end state is desk routes on device tokens with a scope, and
  the amux token retired.

Until those land, the defensible posture is A or B with the app routes
proxied and the desk routes left on the mesh; with them landed, the whole
port can go behind one vhost, canvas page included.

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
- `test_transcript.py` pins messages (§6.2.2) against small synthetic
  transcripts: every record type, redacted and visible thinking, tool
  pairing and summaries (no file contents), turns, incremental reads,
  shrink/replace/rewrite, reading from the end, paging, and the speech
  join. `test_thread_events.py` pins the stream (§11) over real HTTP with
  shrunk intervals: snapshot then appends as the file grows, the speech
  join and `live`, state and suggestion, reconnect, one watcher per session,
  pings, caps, auth, and that `/events` is untouched.
- `test_recaps.py` pins recaps ([Recaps](#recaps)): parsing, the backwards
  read, the cache, and the `recap` field on both routes. The server conftest
  points `CLAUDE_CONFIG_DIR` at a throwaway dir, so no test reads a real
  transcript.
- `test_archive_and_memory.py` pins `POST /session/archive`, `archived` on
  the rows, un-archive on send, and `mem_mb` / `host` on `/sessions/state`
  against a fake `/proc` tree. The conftest points `procmem.PROC` at an empty
  throwaway dir, so no test reads the machine's real processes for memory.
- `test_reap.py` pins the idle reaper, resting, recap `source` and the
  fallback, `POST /session/pin`, and the archive import: the thresholds
  (tight memory included), every never-reap rule, dry run against apply, the
  rested mark and each way it is cleared, and the tag → session mapping. The
  live sweep, the panes, speech, the gateway and ABS are all fakes; closing
  is a recorder.
- `test_agents.py` pins the background agents (§6.12) against synthetic
  thread and subagent transcripts: every status rule (notification in each
  of its three record shapes, a quoted one ignored, a foreground result,
  resumed, stale, the session gone), forks nested with their copied turns
  left out, incremental reads, the log and its paging, both routes' shapes
  and refusals, the snapshot's counts and the `agents` event, and
  `project_of` in both layouts.
- `test_dashboard.py` pins `GET /dashboard` (§6.11) over real HTTP: every
  key set, needs-you from a permission prompt and a question, working from an
  activity file, the reaper log's last run, systemctl and tailscale faked
  (and missing), the machine cache, archived rows left out of `recent`.
- `test_driver.py`, `test_headless.py` and `test_stop.py` pin the Driver
  seam, headless sessions and stop (§12, §17): the pane driver's Escape
  rules; a real in-process sessiond against a fake `claude` for start,
  send, a message queued mid-turn, interrupt, approvals allowed and denied,
  structured and numbered answers, questions with multi-select and free
  text, park and resume, a crash, close, a restart that loses a pending
  request, orphans, the permission overlay; the routes over HTTP; and that
  with the flag off `/ask` opens a pane and nothing headless is listed.
- Run all three packages' tests together (`packages/server/tests
  packages/visual/tests packages/core/tests` in one pytest run): basename
  collisions and cross-suite isolation faults only show up that way.
- When a shape changes, change this file and the test in the same commit.
- When a v1 section is built, move it into §6 and pin it the same way.
