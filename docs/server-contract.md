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
  (§12) is BUILT (22 Sep 2026)** except the per-session speech marker.
- **Headless sessions (§17), behind `MEDIA_HEADLESS` (off by default).**
  With the flag on, a new chat from the app runs as a `claude -p` process
  held by `media-sessiond` instead of a TUI in a pane. Every shape below
  holds for it; the few additions are marked "headless" where they occur.
  With the flag off nothing in this file changes.

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
`/harnesses/screen`, `/harnesses/keys`, `/harnesses/close`, `/share`,
and (22 Sep 2026) `/threads/{session}/events` — matched as a pattern, not
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
  first, titled by their folder name, with `at` = the manifest's mtime.
  **`at` is only on shelved rows.** A session appears once, live if it
  is live.
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
               "mem_mb": 364}],
 "host": {"mem_total_mb": 7758, "mem_available_mb": 1568, "sessions_mem_mb": 3009}}
```

- `state`: `working` | `waiting` (has answered, waiting on you) |
  `approval` (stopped on a dialog). Read from the pane's screen, not a hook.
  A headless row (§17) has it from the agent's own events instead, and
  carries `"driver": "headless"` (a fifth key only it has); `mem_mb` is its
  process tree, measured the same way.
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
  `questions`: `[{"question", "header", "options": [{"label",
  "description"}], "multiSelect"}]`, as asked.
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
                     "server_time": 1790000134.4, "delay": 0.0, "paused": false}},
 "turn": {"running": false}}
```

| Field | Meaning |
| --- | --- |
| `id` | the transcript `uuid` of the message's first record. Stable: a message keeps it as it grows. Codex/pi/Hermes (below): `"line:<at>"` |
| `role` | `"user"` or `"assistant"` |
| `at` | epoch seconds, 3 dp, of the first record |
| `parts` | in order, as the terminal draws them (below) |
| `spoken` | the speech of this message, or `null` if it was not spoken (or cannot be recognised). `id` is the history row for `/speech/ctl replay-id` (`null` while it is still playing for the first time); `key` the reply's dedup key; `images`/`figure` as on lines, only when drawn; `live` only while it plays — the §6.2 live-line fields, moved here |
| `turn.running` | the turn is still going: the last record asked for a tool, or a tool has no result yet. Always `false` when the session is not live |
| `command` | user messages that are a slash command only: `{name, args, text}`, the line's chip (`slash.py`); settings commands are never messages |

**Parts.**
- `text` — the words. Cut at 32 KB.
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
  (sidechain records) are not messages.
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

**Codex, pi and Hermes** have no parser yet: their messages are their lines
reshaped (one text or ask part each, `id` `"line:<at>"`, `spoken` when the
line was spoken). The Builder in `transcript.py` is fed one record at a
time — the shape a headless session's stream-json events also have — so
each harness gets its own reader in front of it.

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
- A pane session refuses the structured form: 400 `"this session answers by
  number (choice and key)"`.

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
 "replay": false,
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
- `error` is added (and `out` starts `error: `) when the verb ran and could
  not do it — a replay whose audio was cleared from the cache, or was
  rendered for another player. Show it; nothing else played instead.
- `replay`, `replay-id` and `prev` can take up to ~8 s longer than before
  (`MEDIA_REPLAY_WAIT_S`): a replay waits for a speaking reply to step aside
  before it plays, rather than playing over it.

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

`{"ok", "path", "at", "title", "text", "links": [{label, path}]}`. `text` is
raw Org, capped at 256 KB. With `at`, only the subtree under that heading is
returned. `links` resolves the text's `[[id:…]]` links to paths.
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
| Stop | none | `POST /session/stop` (§12) | **built 22 Sep 2026**, less the speech marker |
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
| `snapshot` | the whole `/conversation/log?session=` envelope (default page: `messages`, `older`, `lines`, `pending`, `working`, `approval`, `suggestion`, `recap`) plus `{"state", "live": bool, "pane", "resumable"}` | first frame on every connection |
| `message` | `{"op": "append" \| "replace", "message": <message>}` (§6.2.2) | a message appears, or one (matched by `id`) changes: grows a part, a tool finishes, its turn ends, it gains or loses `spoken` |
| `live` | `{"id", "at", "sentences", "sentence", "offsets", "elapsed", "server_time", "delay", "paused"}` — the message being spoken, by `id` — or `null` when nothing is | a new reply starts, the sentence changes, pause or resume, or the clock jumps (more than 1 s from where it should be: a skip) — **not** at 1 Hz; the client runs the clock between frames |
| `working` | the `working` object, or `null` | a step starts, or the turn ends |
| `approval` | the `approval` object, or `null` | a dialog appears, changes or goes |
| `suggestion` | `{"text": "…"}` | the ghost or follow-up arrives or clears |
| `state` | `{"state": "working" \| "waiting" \| "approval" \| "ended", "live": bool, "pane"}` | the session changes state; `ended` when its pane goes |
| `recap` | `{"text", "at", "source"}` or `null` | a newer recap is written |
| `ping` | `{}` | 15 s of silence |

`isRunning` is `state == "working"`, `pending`, or the last message's
`turn.running` (§14).

**Applying events.** Keep messages by `id`. `append` adds at the end —
unless the `id` is already held, which can happen in the moment between a
snapshot and the first event: then it replaces. `replace` replaces in place.
Only the newest page (60) is watched; a replace never arrives for a message
older than that.

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
happened.

**The thread list** stays polled (`/sessions/state` at 5 s, `/targets` on
open). A list stream is left for later: it would be a second watcher over
every pane, for a list that changes slowly.

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

## 12. v1: stop — BUILT 22 Sep 2026 (less the speech marker)

Code: `agent_media_server/stop.py`, the drivers' `interrupt`
(`driver/pane.py`, `driver/headless.py`), `speech.stop_speech`. Pinned by
`packages/server/tests/test_stop.py`. As built:

- **Interrupt** goes through the driver that owns the session: a headless
  session gets the `interrupt` control request (its receipt names queued
  messages, which then run as their own turn); a pane gets Escape, only
  while it is `working`, watched for up to 3 s (504 `"still working after
  Escape"`). Codex panes are Escaped too; pi and Hermes answer
  `interrupted: false, "why": "not supported for pi"`.
- **Speech** is stopped only when what is heard is this thread's (the
  canvas's speech snapshot names the session): `SinkSpeech().stop` on the
  player it is playing on, as `media stop` does.
- **Not built — the per-session marker.** Core has no per-session speech
  marker yet (`after` / `all` below). So `cutoff` is always `null`; a reply
  the interrupted turn had already handed to the Stop hook still speaks;
  and "this thread's queue" is not dropped — only the clip playing now is
  stopped. Needs the marker and its checkpoint in `intake/submit.py`.
- `state` afterwards is the driver's (`ended` for a session not running).
- In `CORS_PATHS`.

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
| `content` | the parts, in order: `text` → `{type: "text", text}`; `reasoning` → `{type: "reasoning", text}` (a redacted one → a collapsed "Thought" with no text); `tool` → `{type: "tool-call", toolCallId: tool_use_id, toolName: name, args: {summary: input_summary, title}, result: result_summary}` (no `result` while `status == "running"`; `isError` when `error`); `ask` → `{type: "tool-call", toolName: "AskUserQuestion", toolCallId, args: {questions: ask}, result: answer}`; the pictures (below) |
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
| `isRunning` | `pending` | `state == "working"`, `pending`, or the last message's `turn.running`. Note that assistant-ui disables the composer while running, but a Claude Code session takes messages mid-turn (they queue), so the app passes sends through while running |
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

AskUserQuestion's multi-select and free-text "Other" cannot be answered by
number. For those, the tool UI offers "answer at the desk" (`/focus`) —
that is the v0 behaviour, and a gap (§16) for pane sessions.

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
| Stop | **built 22 Sep 2026** (§12), except the per-session speech marker in core (`after` / `all`): no cutoff, and this thread's queued replies are not dropped |
| Machine-readable error codes | specified (§13), not built |
| Archive / unarchive a thread | **built 22 Sep 2026**: `POST /session/archive` and `archived` on `/targets` rows (§6.1, §6.4), a server-side flag in `<state_dir>/archived.json`. Left: the app side, and moving any existing ABS `archived` tags over (not done — the tag and the flag are independent until then) |
| Session memory on the phone | **built 22 Sep 2026**: `mem_mb` per `/sessions/state` row and its `host` block (§6.1). Left: the app side |
| Delete a thread | none, and deliberately not proposed: transcripts are the harness's. Needs a decision |
| Attachments (a photo, a file) | none. `/reply` is text only. Needs an upload route and a way to hand a file path to the harness |
| Answer a multi-select or free-text ask from the phone | **headless sessions: built 22 Sep 2026** (§6.4, §17 — structured `answers`). Pane sessions: none; `/session/answer` presses one number |
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
