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
`/harnesses/screen`, `/harnesses/keys`, `/harnesses/apikey` (28 Sep 2026), `/harnesses/close`,
`/harnesses/logout`, `/harnesses/updates` (23 Sep 2026), `/share`, `/upload` (25 Sep 2026; its GET 29 Sep 2026),
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
| **session** | a uuid (Claude Code, Codex, pi, Gemini CLI, Grok), or Hermes's `YYYYMMDD_HHMMSS_hex` | `reply._SESSION`. The thread id. Every session-taking route rejects anything else with 400 `"not a session id"` |
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
 "places": [{"name": "agent-media", "path": "/home/ryer/projects/agent-media", "at": 1790000000.1, "mail": 2}]}
```

- **`restart_pending`** (19 Oct 2026; additive, only while there is one):
  a restart of the session host is waiting for its busy sessions to finish
  (§17 "Draining"): `{"since", "deadline": <epoch s>, "state": "waiting" |
  "firing", "waiting_on": [{"session", "kind": "turn" | "agents" | "own" |
  "speech", "label"}]}`. Each headless row, and a headless thread's
  snapshot (§11), carries the same object plus `"held": <how many of this
  thread's messages wait for it>`, so a held reply (§6.2.2 `held`) can say
  "held until the server restarts". It goes when the restart is done or
  given up, and the held messages are then delivered.
- **`quota`** (20 Oct 2026; additive, only while there is one): the
  account's usage limit (§17 "The usage limit"): `{"resets_at": <epoch s>,
  "since", "kind": "five_hour" | "seven_day" | …, "state": "waiting" |
  "resuming", "waiting": <threads in line>}` — `waiting` until the reset,
  `resuming` while the threads it stopped are picked up one by one. Each
  row (headless or desk) and a thread's snapshot (§11) that is in line
  carries `"quota": {"resets_at", "state"}`, so the app can say "Paused by
  the usage limit — resumes at 3:00"; it goes once the thread is resumed.
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
- `tags` (every row, 24 Oct 2026): the thread's own tags, in file order,
  from its org-roam node's `#+filetags` (`POST /session/tags`, §6.4); `[]`
  when it has none or no node, and on a sealed row.
- `speech` (every row, 24 Sep 2026): the thread's speech level —
  `"interrupt"`, `"auto"`, `"normal"` or `"quiet"` — set with
  `POST /session/priority` (§6.4) or `media priority`; a thread with none
  of its own has the default (`/speech/default`), and `speech_own` (25 Sep
  2026) says whether the level is its own. `priority` (same
  day) is true for interrupt and auto: its replies are never held by the
  desk toast and never silenced by a pane mute. **Where it comes from**
  (20 Oct 2026, one scale for threads and people): a Matrix room with no
  level of its own has the level of the person it is with, set on their
  card (§6.25 "Speech level"), else quiet; any other thread the default.
  `speech_from` says which: `"own"`, `"person"`, `"room"` (quiet, nobody's
  level) or `"default"`. `speech_fallback: {"level", "from"}` is what "Use
  the default" gives it: the app labels that choice "Default (follows …)",
  naming the person's level or Quiet for a room, the server default
  otherwise.
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
  for a directory in a project root (a worktree inside one counts; one in
  a group is `p-<folder>` too, see `project_groups`, and a link left at an
  old path names the project it points at; `spl-p-<folder>` in another
  root), else
  the directory's own name (`org`, `scratch`; 24 Sep 2026), `null` for home.
  The shelf's catch-all folder `conversation` (a thread with no workspace)
  is never a series. Default layout: the folder's basename (home is `sasonica`). Both
  `null` when unknown. The app draws `project` as a small line under the
  title and groups its By-project order by it. The same two keys are on
  `/conversations` rows, `/dashboard`'s `recent`, `working` and
  `needs_you` rows, and the thread stream's snapshot (§11).
- `places`: up to 6 directories sessions have run in, newest first
  (running sessions count as "now"). These are the only directories a new
  chat may be opened in — `/ask` checks against this list (with no limit).
  `mail` (16 Oct 2026): how many agent-mail messages are **pending** in
  that place's mailbox — not claimed, not acknowledged. The mailbox is
  agent-config's `agent-handoff` `mailbox_of`: a git repo's name (a
  worktree counts as its main checkout), else the folder's name; the
  shared `sam` mailbox is never a place's (always 0). Read from this host's
  agent-mail SQLite (`AGENT_MAIL_DB`, else
  `$XDG_STATE_HOME/agent-mail/mail.sqlite3`) in one query, cached 5 s
  (`mailbox.py`). **Absent** on every place when the mailbox can't be read,
  and from older servers: treat absent as 0. The app badges the place's
  chip when it is above 0. `/dashboard`'s `places` carry it too.
- `project_mail` (16 Oct 2026): `{project: n}` for every project with
  pending agent-mail, keyed like the rows' `project` — the Threads page's
  project strip names every project its rows do, not only the six newest
  places. A project's mailboxes are those of the folders its rows (`cwd`)
  and places ran in, each counted once, by the same rule as `mail`.
  Projects with none are left out; the key is **absent** when the mailbox
  can't be read, and from older servers. About 50 ms cold, 10 ms warm.
- `project_groups` (18 Oct 2026; every root 19 Oct): `{project: {"group",
  "name"}}` for every project that sits in a group, keyed like the rows'
  `project` (`"spl-p-sasonica-app": {"group": "sasonica", "name":
  "sasonica-app"}`). Projects live in the project roots (§6.24: the stored
  list, else `MEDIA_PROJECTS_DIR`, else `~/projects`). A group is a folder
  in a root with no `.git` of its own holding at least one folder that has
  one; every folder in it is then a project. Detected from the folders,
  nothing configured (`layout._index`). On David's desk a project is
  `p-<folder>` in `~/projects` and `<owner>-p-<folder>` in another root, the
  owner being the folder above the root (`~/spl/projects` → `spl-p-`),
  grouped or not (`<prefix><group>-<folder>` only when two projects share a
  folder name); its tmux session has the same name; a link
  left at an old path, and a name a project went by before a move, list as
  the project they point at (`layout.canonical_project`). The app draws the
  group as a heading with its projects nested under it (By-project list,
  project strip, move picker) and titles a thread `group / name`; a project
  not in the map stands alone. Default layout keys are `group/name`. `{}`
  when there are none; **absent** from older servers.
- **Headless rows** (`MEDIA_HEADLESS`, §17): the sessions `media-sessiond`
  holds are listed after the live pane sessions — the running ones, then up
  to 20 ended ones (parked or closed), newest first — with the same keys
  plus four that only they carry: `"driver": "headless"`, `"drivable": true`
  (the phone can send to it, answer it and stop it), `"harness": "claude"`
  and `"source": "sessiond"`. `pane` is always `null`; `live` is whether its
  process is running; an ended one has `at` (its last event) like a shelved
  row. Title: the shelf's name, else Claude's (`/rename`, then `ai-title`),
  else the first message. Pane rows never carry the four keys.
- **Matrix rows** (8 Oct 2026, `matrix.py`; the Matrix proposal's step 1):
  one per room on `MATRIX_ROOM_ALLOW`, last in the list, with
  `"source": "matrix"`, `"harness": "matrix"`, `"room"` (the room id) and
  `"drivable": false`, and `"network"` — the service a bridge carries the
  room for, `{"id", "name"}` from its `m.bridge` state with the variants
  folded (`gmessages-sms`, "Google Messages (SMS)" → `gmessages`, "Google
  Messages"), or null (11 Oct 2026; the app's Messages tab chips). `session` is a uuid made from the room id (stable,
  so every session route takes it); `title` the room's name, else its other
  members; `recap` the last message, `{"text": "<name>: <words>", "at",
  "source": "matrix"}`; never live, rested or pinned. `/conversation/log`
  and the thread stream (§11) serve the room's timeline as §6.2.2
  messages: the owner (`MATRIX_OWNER_ID`, else the first of
  `MATRIX_CONTROL_IDS` that is not `MATRIX_SAM_ID`) is `user`, everyone
  else `assistant` with `peer: {name}` and `sender: {id, name}`; photos,
  voice notes and files are a one-line text part. The envelope adds
  `room: {id, name, members}`; `around=<event id>` (a search hit, §6.14;
  17 Oct 2026) answers the window holding it as for a session, paging
  back up to 10 pages for one older than the room holds. Rooms are
  searched (§6.14, 17 Oct 2026). **Sending** (9 Oct 2026): `/reply` to a
  room sends `text` as the owner, with the owner's own token
  (`MATRIX_OWNER_TOKEN`, made by `matrix-owner-login`; never the agent's),
  a `quote` in front as a Markdown quote, and answers `{"session",
  "pane": null, "submitted": true, "event_id"}`; the message is in the
  thread at once. With no owner token it is 409, `drivable` is false and
  `/conversation?session=` says `resumable: false`; `mode: "branch"` is
  always 409. The app lists these rows only under its Matrix filter.
  **Speech** (9 Oct 2026): a room has a speech level like any thread
  (`POST /session/priority` takes its id) and **starts quiet**, once per
  room. Anyone else's text (read as "<name>: <words>") and voice notes
  follow it as an agent's reply does: quiet keeps them unplayed with a
  Play, normal plays only while the room is open. A message with speech
  has `spoken: {id, key: "", at}` (plus `unheard: true` while held), and
  the app's Play replays it by id. **Contact** (17 Oct 2026, §6.25):
  `"contact": {"id", "name"}`, the one person the room is with, from the
  phone's contacts — so Messages can group a person's rooms without asking
  again. **Absent** when no contact matches (a group, a stranger, a server
  that has never read the phone): treat absent as "a room, as before". `id` is the one
  `GET /contacts/<id>` takes. **Pictures** (20 Oct 2026): a row whose room
  has its own picture (`m.room.avatar`) carries `"avatar": {"url":
  "/rooms/<session>/avatar", "etag"}` (absent when none; a person's row
  shows the contact's picture first, §6.25 "Pictures"). In the timeline,
  an incoming message's `sender` adds `avatar: {url, etag}` — the member's
  own picture on the homeserver, `/rooms/<session>/avatar?user=<mxid>` —
  and `contact: {id, name}`, the person that member is (matched as a room
  with them alone is: their bridged account, number, address, or a
  trusted network's name; the owner's book only), so a group chat shows
  each sender's picture and a tap opens their card; either is absent when
  there is none. `GET /rooms/<session>/avatar[?user=<mxid>]` — gated, the
  owner's (403-free: anyone else gets 404) — answers that picture as a 256
  px thumbnail from the homeserver's authenticated media, fetched once and
  kept by its mxc's hash (0600; a failure is not asked again for an hour),
  with `ETag`, 304 and `?access_token=` for an `<img>`; none, an unknown
  room or member, 404.

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
- `background` (20 Oct 2026): `{"agents": N, "commands": M}` on a
  `waiting` row whose background work is still running — subagents started
  in the background, background commands — and absent otherwise (and on
  every `working` or `approval` row). `state` stays honest: `waiting`
  still means the turn is over, so everything keyed on a turn ending (the
  "New reply" notice, speech, follow-along, `pending`, §6.13's stream)
  fires when the main turn ends, as before. The flag only tells a client
  the thread will report back by itself: the app labels it working
  ("Working · 2 agents"), not "your turn", until the counts reach zero. A
  headless row's counts are sessiond's tasks (`local_bash` and other shell
  kinds are commands, the rest agents); a pane row's are its transcript's
  `running` subagents (§6.12) and `commands` is always 0 (a pane session's
  background Bash cannot be seen from the server). The same test a move
  waits on (`why_waiting: "agents"`, §6.15). Code:
  `agent_media_server/background.py`; older servers leave it out.
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
 "recap": null,
 "context": {"used": 66597, "window": 200000}}
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
- `context` (7 Oct 2026): how full the session's context is, in tokens,
  `{"used", "window"}`, or `null` (no reply yet, or not a Claude Code
  session). `used` is the newest reply's input (`input_tokens` +
  `cache_creation_input_tokens` + `cache_read_input_tokens`), so it is as
  of the last reply and drops after a /compact. `window` is what Claude
  Code's status line last reported for the session (`media-statusline`,
  saved as `<state>/agent-media/context/<session>.window`), else by the
  reply's model (Opus 5.5: 1 000 000), else 200 000, or 1 000 000 once
  `used` is past 200 000.

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
| `unheard` | held replies | `true` for a reply held for the listener and never played (the desk toast, `intake/toast.py`: history `extras.held` without `extras.heard`); absent once a replay has played it to its last sentence (not when its clips are pushed; a replay stopped part-way leaves it, with `stopped_at`, 14 Oct 2026), and on the line being heard (24 Sep 2026) |
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

Codex's `request_user_input` uses the same question form below (21 Oct
2026), with single-select options and free text. Its PreToolUse event uses
shared question storage and speech, including reading later parts only when
the earlier part is answered. `codex_asks.py` reads the `Question i/n` overlay
and checks the stored request against it; an unfinished rollout is a fallback
when no hook copy exists (`source: "rollout"`). With neither copy available,
the visible question is offered one part at a time (`source: "screen"`).
Digits commit and advance in Codex, so its answer driver never follows a
choice with Enter. Free text selects "None of the above" and opens the notes
composer before typing. Stale cards are rejected by the same `key` check.
When launching or resuming Codex, the server passes the chosen working root
explicitly with `--cd`, including after a menu move; tmux's cwd alone leaves
Codex asking whether to restore the session's previous directory.

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
| `spoken` | the speech of this message, or `null` if it was not spoken (or cannot be recognised). `id` is the history row for `/speech/ctl replay-id` (`null` while it is still playing for the first time); `key` the reply's dedup key; `images`/`figure` as on lines, only when drawn; `live` only while it plays — the §6.2 live-line fields, moved here; `timeline` instead of `live` on the newest spoken turn when it is not playing (`{sentences, offsets, measured}`, §6.2); `unheard: true` on a reply held and never played (as on lines) — the app makes its play key the big one; `resume: {sentence, at_s, dur_s}` on a reply the listener interrupted (a Stop, or a reply that ended the reading) and has not heard again — `sentence` is the first not heard (clip index, what `replay-id` `sentence` takes), `at_s` the seconds into the reply it starts at, `dur_s` the reply's length; history `extras.stopped_at`, cleared when the reply is replayed; also written when a paused reply outlasts the speech lock's hold (`stopped_at.paused`, the sentence it was paused on, §`/speech/now` `left_paused`, 14 Oct 2026); the bar's Replay resumes it too (25 Sep 2026) |
| `turn.running` | the turn is still going: the last record asked for a tool, or a tool has no result yet. Always `false` when the session is not live |
| `command` | user messages that are a slash command only: `{name, args, text}`, the line's chip (`slash.py`); settings commands are never messages |
| `peer` | user messages another session delivered into this one (a `<cross-session-message>`): `{name}`; the text is only the message body. Not the listener's words — the app shows a small "From <name>" and never counts it as a send of its own |
| `shell` | user messages that are a `!` command (29 Sep 2026, `shell.py`): `{command, stdout?, stderr?}`, each stream at most 8 KB; the text is `!<command>`. The output keys come once the command has finished (a pane writes it as a record of its own) |
| `compacted` | `true` on the marker where the context was compacted (16 Oct 2026): `/compact`, or the harness's own when the context fills (Claude Code's `compact_boundary`, pi's `compaction`). Role `assistant`, never spoken, its one part the text "Conversation compacted" for a client that does not know the field. The `/compact` that asked for it is a settings command and not a message, so this is what tells a client it was done: a client that sent `/compact` retires that send on the first marker after it |

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
  chars; `description` (Claude only, 23 Oct 2026; absent when empty) — the
  call's own plain-words step name, one line, cut at 120 chars, for Bash,
  PowerShell, Agent/Task and Monitor only (another tool's `description`,
  such as a calendar event's, is content and is never copied here); `status` `running` | `done` | `error` (an interrupted turn's
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

**Gemini CLI** (17 Oct 2026) writes `~/.gemini/tmp/<slug>/chats/
session-<stamp>-<id[:8]>.jsonl`, but its lines replace one another — a
message is written again whole each time it changes, `$rewindTo` drops a
turn — so it has no Reader: the file is folded whole as Gemini folds it
(`harnesses.gemini_fold`) and `GeminiBuilder` is fed what survives, the
result kept until the file's inode, size or mtime moves. A `user` message
is a prompt (its `<hook_context>` left out), or a tool's answer, which is no
message; a `gemini` message is one model step — `thoughts` (subject and
line, with their words), the words, and its `toolCalls`, each written once
it has finished, with its status. A turn's steps are one message until the
next prompt. Tool names are canonicalised as above (`run_shell_command` →
`Bash`, `read_file` → `Read`, `replace` → `Edit`, …).

**Grok** (17 Oct 2026) keeps a folder per conversation,
`~/.grok/sessions/<cwd, url-encoded>/<id>/` (`GROK_HOME` moves it), and its
log there, `updates.jsonl`, is ACP `session/update` events: words in
chunks (`user_message_chunk`, `agent_message_chunk`, `agent_thought_chunk`)
and tool calls (`tool_call`) brought up to date by later lines
(`tool_call_update`). So it has no Reader either: the log is folded whole
(`harnesses.grok_fold` — chunks in a row joined, a new `messageId` a new
message) and `GrokBuilder` is fed the result, kept until the file moves,
as Gemini's is. A user message is a prompt (Grok's `<system-reminder>`s
and `<user_query>` wrapper left out); everything after it until the next
prompt is one assistant message — thoughts, words, and tool calls with
their status (`completed`, `failed`; `pending` and `in_progress` running).
Tool names are canonicalised as above (`run_terminal_command` → `Bash`,
`search_replace` → `Edit`, `write` → `Write`, `list_dir` → `LS`, …). The
shapes are from Grok 1.0.62's docs, not yet read off a real session: how a
line wraps its update is not known (all three plausible wrappings are
taken), and xAI's rewind marker is not understood yet, so a rewound turn
still reads as said.

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

Request: `{"session": "<session>" | "item": "<item>", "text": "…", "quote"?: "…", "mode"?: "continue" | "branch", "refs"?: {"<title>": "<session>"}, "keep_reading"?: bool, "voice"?: bool, "nudge"?: bool}`

`voice` is the app's hands-free loop (David, 29 Sep 2026): the session is
marked for 15 minutes (`agent_media_core.voice_mode`) and the prompt hook the
words set off (`intake/heard.py`) adds a note asking for one to three short
spoken sentences, a decision as one closing question, and a spoken yes before
anything hard to undo. It is context, not text: the message and transcript
are unchanged. A reply without `voice` clears the mark; a `!` command leaves
it as it was.

While marked, a Claude thread talks on a faster model (David, 22 Oct 2026;
`agent_media_server.voice_model`): the first `voice` reply that finds it
between turns switches it to `sonnet` before the words go in (a pane is typed
`/model sonnet`, a headless thread gets sessiond's `set_model`), and the
note adds a line asking it to pass `model: "opus"` to the background agents
it starts. A reply without `voice`, or the reaper's pass once the mark has
gone stale, puts back the model it had, again only between turns; a model
changed by hand meanwhile stands. The user's default for new sessions
(Claude Code saves a typed `/model` as one) is put back as it was. Nothing
in the request or the answer changes. `MEDIA_VOICE_MODEL=0` turns it off;
`MEDIA_VOICE_MODEL_CLAUDE` / `MEDIA_VOICE_SUBAGENT_MODEL_CLAUDE` name other
models. Other harnesses keep their model for now.

`nudge` (David, 22 Oct 2026) is the loop hearing nothing after a reply: the
app sends `{"session", "text": "", "voice": true, "nudge": true}` once per
reply, in place of ending the loop, and listens again for the answer. `text`
is ignored. The session gets a note in a `<voice-nudge>` tag
(`voice_mode.nudge_text`), which the shelf, the transcript and the prompt
hook drop like a system reminder, so no listener turn appears: only the
agent's answer. The note asks it, if its last reply ended with a question,
to ask it again in one short, simpler sentence (ideally yes/no), and
otherwise to offer the one most useful next step as a short yes/no question
— or to answer exactly `[[quiet]]` when there is none. A `[[quiet]]` reply
is never spoken (the Stop hooks) and never shown (`strip_markers` drops the
message). The answer is spoken through the normal path, so the speech gate
(`MEDIA_SPEECH_GATE_CMD`) and the thread's level apply. A nudge does not end
the read-out, keeps the voice mark on, takes no owed retract note, and is
refused with `mode: "branch"` (400) or for a Matrix room (409). Whether to
nudge, and only once, is the app's call.

- **A reply ends the read-out** (25 Sep 2026). Replying means the thread's
  last reply was read, so its speech is cut as `read`: queued replies of the
  thread are dropped (archived, marked flushed) and the one playing ends at
  the close of its sentence — on the phone, when the player moves past the
  sentence it was on. The reply the message starts plays as usual. The
  desk gets the same from Claude Code's UserPromptSubmit hook (it records
  nothing; it only cuts). **`keep_reading: true`** is the reply box's chip
  switched to Keep reading: nothing is cut, and the prompt hook that send
  sets off is told to leave it too (a `keep` mark, 30 s, spent by the hook).
  A thread at the Auto speak level is never cut by a reply. `branch` cuts
  nothing. Show the chip only while the thread is being read (`/speech/now`).

- **`refs`** (24 Sep 2026), on `/reply` and `/ask`: `{"<title>":
  "<session>"}` for the `@[<title>]` chips in `text` — another conversation
  named by a chip (the app's Share… or `@` in the composer) instead of by
  typing its title. Each chip gets a line at the foot of the text, `@[<title>]
  is conversation <session> (<harness>, transcript <path>)`, so the agent
  can read it; the chip itself stays as written. A chip with no entry in
  `refs` is matched by title among the threads, used only when exactly one
  has it; one that names nothing gets no line (`refs.py`).
- **`!` runs a shell command** (29 Sep 2026, `shell.py`), as Claude Code's
  bash mode: `text` starting with `!` is a command, run in the session's
  directory, and the command and its output go into the conversation for the
  agent, which answers them. A pane is typed it as is (its terminal runs it);
  a headless session's is run by the server (`bash -c`, 120 s, each stream
  cut at 30 000 characters) and sent in as the same tagged message, and the
  answer comes at once, with `shell: true`, before the command has finished.
  No quote goes in front and a taken-back note waits for the next message:
  the `!` has to stay first. The thread shows it as a `shell` message (§6.2.2).
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
host is at `MEDIA_SESSIOND_MAX`, or below `MEDIA_SESSIOND_MIN_FREE_MB` of
free memory with one of its sessions running, and nothing is idle.

**Held for a restart** (19 Oct 2026): while a restart of the session host
is pending (`restart_pending`, §6.1, §17 "Restarts"), a reply is not given
to the agent but kept: the answer adds `"held": true` (with `queued: true`,
`acked: false`), and the message is delivered, in order, once the host has
restarted (or the restart gives up). Show it as "held until the server
restarts", not as failed. A new chat is refused meanwhile: 503 `code:
"restart_pending"`, with a sentence.

**Held for the usage limit** (20 Oct 2026): while the account's usage limit
holds (§6.1 `quota`, §17 "The usage limit"), a reply to a Claude thread is
held the same way and the answer carries `"quota": {"resets_at", "state"}`
too: show "held until the limit resets at 3:00". It is delivered after the
note that resumes the thread. A new Claude chat is refused 503 `code:
"quota_limited"` with a sentence and `quota`.

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
| `agent` | `claude` \| `codex` \| `pi` \| `hermes` \| `opencode` for a fresh session (default `MEDIA_ASK_AGENT`, else claude). With `MEDIA_HEADLESS` on, `claude` and `opencode` start headless (sessiond; opencode as a session of its one shared `opencode serve`, 29 Sep 2026), the rest in a pane |
| `project` | open a fresh session in that project's directory: a series (`p-agent-media`) on David's desk, a folder basename (`agent-media`) in the default layout (§18) |
| `cwd` | open a fresh session in that directory — must be a `/targets` place |
| `new_project` | a name (15 Oct 2026, §6.24): make a project folder and open the fresh session in it — implies `target: "new"`. With `repo`, `agents_md`, `preset`, `root` |
| `root` | with `new_project` (18 Oct 2026, §6.24): which project root it goes in — one of `GET /projects/roots`, else 400; default the first; its folder gone (`missing`) → 409 |
| `repo` | with `new_project`: clone this git URL instead of `git init` (§6.24) |
| `agents_md` | with `new_project`: `true` writes a starter `AGENTS.md` and `CLAUDE.md` → it; `"draft"` has the first chat write it (§6.24) |
| `preset` | a preset's name (§6.24): its AGENTS.md text for `agents_md`. The app sends the preset's agent, profile, model and plan itself |
| `worktree` | `{"from": <a /targets place>, "branch"}`: open the fresh session in a new git worktree of that project (§6.24) — implies `target: "new"` |
| `persona` | a persona id from `/presets`' `personas` (16 Oct 2026, §6.24): the fresh session starts as its public render. Unknown → 400; ignored when the words land in an existing thread |
| `role` | a role id from `/presets`' `roles` (§6.24): with or without `persona`. Unknown → 400 |
| `model` | Claude (26 Sep 2026): `opus` \| `sonnet` \| `haiku` \| `fable` for a fresh session — `--model` in a pane, sessiond's `start` headless. opencode: a `provider/model` id from its sheet, sent with each prompt when headless. Anything else is dropped, not refused; ignored when the words land in an existing thread |
| `plan` | Claude only: `true` starts the fresh session in plan mode (`--permission-mode plan`; a pane's `--dangerously-skip-permissions` becomes `--allow-dangerously-skip-permissions`, which would otherwise win) |
| `level` | a fresh session's speech level (§6.4: `interrupt` \| `auto` \| `pocket` \| `normal` \| `quiet`; 14 Oct 2026), set as soon as its id is known so the first reply already speaks at it; answered as `level` (null when the id never surfaced — set it with `/session/priority` then). Anything else → 400; ignored when the words land in an existing thread |

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
 "submitted": true, "title": "", "text": "…",
 "persona"?: "zara", "role"?: "", "persona_via"?: "system_prompt" | "prepend"}  // with persona/role

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

#### `POST /voice/check` — gated (24 Oct 2026)

Hands-free, before heard words send: did the phone hear what was meant?
With the screen off and an earbud in, nobody reads the box during the
countdown (David, 24 Oct 2026). Request:
`{"text": "<heard words>", "session"?: "<session>", "context"?: "<what the
words answer: the last reply, or the question card>", "confidence"?: <the
recogniser's 0–1, omitted or ≤ 0 when unknown>, "say"?: "Did you mean:
{gist}?"}` → `{"ok": true, "sure": bool, "gist": "<one short sentence of what
they meant>", "spoken": bool, "why"?: "…"}`.

A fast model (`MEDIA_VOICE_CHECK_MODEL`, else `MEDIA_FOLLOWUP_MODEL`, on the
summary gateway, `MEDIA_SUMMARY_*`) reads the words with `context` (the last
1500 characters) and answers `sure: false` when they look garbled, misheard,
cut off or easily taken the wrong way; a recogniser `confidence` under
`MEDIA_VOICE_CHECK_MIN_CONFIDENCE` (0.5) makes it unsure on its own. `gist`
is in the words' language, phrased as the request itself. Anything that goes
wrong answers `sure: true, gist: ""` with a `why` — `no_answer` (no gateway,
an unusable answer, or none within `MEDIA_VOICE_CHECK_TIMEOUT`, 3 s, a
hard deadline), `no_gist`, `no_words`, `off` (`MEDIA_VOICE_CHECK=0`) — so the
loop sends as it would have; `low_confidence` marks an unsure answer the
model alone would have passed.

`say`, the app's own words in its language with `{gist}` in them, has an
unsure answer asked aloud in `session` (`spoken: true`) the way a question
card's next part is read: the thread's voice and place, the speech gate and
its level, and never kept in speech history (`private`). The app waits for
it to play, then listens for yes or no. Nothing is sent to the session;
which words go, and when, is the app's call. 400 without `text`, or for a
`session` that is not a session id. `voice_check.py`.

Since 24 Oct 2026 every dictation with a countdown asks too, not only
hands-free. The mic key with the screen on sends no `say` (nothing is
spoken; the app shows the gist with Send and Edit) and asks alongside its
countdown; the assistant and headset buttons, and a press in the dark, send
`say` and ask first. A dictation in a new chat has no `session` and no
`context` (no reply yet): it is checked on the words alone and never
spoken.

Clients: W (`hooks/useHandsFree.ts`, `hooks/useDictation.ts`).

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

#### `POST /session/tags`, `GET /tags` — gated (24 Oct 2026)

`{"session", "add"?: [tag], "remove"?: [tag], "tags"?: [tag]}` →
`{"ok": true, "session", "tags"}`, the thread's tags after the change.

- Kept in the thread's org-roam node, not here: the file agent-sessions
  exports for it under `MEDIA_THREAD_TAGS_DIR` (default
  `~/org/roam/sessions`), found by `:AGENT_SID:`. The edit changes its
  `#+filetags:` line and nothing else; the exporter keeps that line once the
  file exists, and moves the tags into the Denote file name on its next pass.
  A tag added in Emacs shows on the next `/targets`.
- The exporter's own tags in that line — the harness, `inbox`,
  `autonomous`, the project hub's key and name from the `- Project ::` link,
  the directory (whole and its last part) and any path tag `home_…` — are
  not the thread's: rows leave them out and an edit keeps them, first. So a
  tag cannot start with `home_` (400).
- `tags` replaces the set; `add` then `remove` change it (a string is a list
  of one). A tag is made an org tag: a leading `#` dropped, spaces and dashes
  `_`; then letters, digits, `_@#%`, at most 40 characters, else 400 `"not a
  tag: …"`. At most 20 per thread. None of the three: 400.
- 409 `"this thread is not in your org files yet; try again in a few
  minutes"` when no node has the session (newer than the last export); 423
  on a sealed thread; 400 `"not a session id"`.
- `GET /tags` → `{"ok": true, "tags": [{"tag", "count"}]}`: every thread tag
  in use, most used first, for a filter and for suggestions.
- Both in `CORS_PATHS`.

Pinned by `packages/server/tests/test_thread_tags.py`.

#### `POST /session/priority` — gated (24 Sep 2026)

`{"session", "level"}` → `{"ok": true, "session", "level", "own", "from", "priority"}`
(`from`: where the level now comes from, as a row's `speech_from`; 20 Oct
2026). `"level": "default"` clears the thread's own: a Matrix room then
follows its person's level, else quiet (§6.25 "Speech level").

What happens to the thread's replies (a Claude Code Stop read-out; its
questions are never touched):

| level | the reply |
|---|---|
| `interrupt` | plays at once, at HIGH priority: another thread's reply steps aside at its next sentence and resumes after |
| `auto` | plays at once: the desk toast does not hold it and a pane or tmux-session mute does not silence it |
| `pocket` | as `normal`, and the thread still counts as open for a while after the phone's screen went off on it (`POST /session/pocket`); the app's "When open or locked" (28 Sep 2026) |
| `normal` | the usual rules (the built-in default) |
| `quiet` | rendered and archived unheard (`extras.held`, `unheard: true` on the transcript) and never played by itself |

- `priority` in the answer, and on the rows, is true for interrupt and auto.
  The older body `{"session", "priority": bool}` still works: true is auto,
  false normal; with neither, auto.
- Kept in core (`agent_media_core/speak_priority.py`), per session, in
  `<state_dir>/speak-priority.json` (`{"<session>": {"level", "at"}}`; a
  bare number is the older flag, read as auto), so the hooks read it without
  the server; `media priority [interrupt|auto|pocket|normal|quiet|status]` sets the
  same thing from a pane. Outlives the session, like a pin.
- 400 `"level must be interrupt, auto, pocket, normal, quiet or default"`; the other
  refusals and CORS exactly as `/session/pin`.

- `"level": "default"` (25 Sep 2026) clears the thread's own level, so it
  follows the default. The answer's `level` is the one it now has, and
  `own` whether that is its own.
- A level equal to the default (`/speech/default`) clears the thread's own,
  so it follows the default from then on; any other is kept, normal
  included.

Pinned by `packages/server/tests/test_reap.py` and
`packages/core/tests/test_speak_priority.py`.

#### `GET|POST /session/voice` — gated (21 Oct 2026)

The Speech card's per-conversation voice override. GET takes `?session=`;
POST takes `{"session": "<id>", "voice": "edge:<ShortName>" | null}`.
Null clears the override (**Use default voice**). Both answer `{"ok": true,
"session", "voice", "default_voice", "effective_voice", "voices", "languages"}`.
`voice` is the stored override or null; `default_voice` is the current target's
voice, and `effective_voice` is the override or that default. `voices` and
`languages` are the named Microsoft voice catalog used by `/speech/voice`.
These voices can render on either phone or server; choosing one changes no
render-side or global voice setting. Gate/session checks match `/session/priority`.
Missing, empty, non-string or unknown voice is 400; a null voice is valid.
Save failure is 500.

Kept by session in `<state_dir>/session-voices.json`, shared across devices.
Applies to subsequent replies, questions and thinking; a reading already
submitted keeps its choice. An explicit event engine/voice still wins over
this setting; the hooks' automatically assigned workspace voices do not.
Server rendering selects Edge and its voice name; phone clips pin the choice
in a `.voice` sidecar, so replay retains it even after the override changes.
Removing the override makes subsequent speech follow its usual target defaults.

#### `GET|POST /session/thinking-speech` — gated (21 Oct 2026)

A conversation's **Read thinking aloud** switch, separate from its speech
level and shared across devices. Conversations follow the server's default
unless explicitly set. GET takes `?session=`; POST takes
`{"session": "<id>", "enabled": true|false|null}`; null restores inheritance.
Both answer `{"ok": true, "session": "<id>", "enabled": false,
"supported": true, "own": false, "default": false}`. `enabled` is effective;
`own` marks an explicit choice (including off), and `default` is the server's
choice. Unsupported agents remain disabled even when the default is on.
Gated and checked for session existence as `/session/priority`: 400 for a
bad session or an invalid/missing `enabled`, 404 for no session, 409 when
enabling a harness without a readable reasoning adapter, 500 on save failure.
`supported` means the harness has an adapter, not that its current model
necessarily exposes thinking. Empty and redacted reasoning remains silent.

The server keeps session overrides in `<state_dir>/thinking-speech.json`
(an enabled generation token, or false for explicit off) and
watches independently of app connections. Enabling, restarting the server,
replacing or truncating a transcript establishes a baseline: old thinking
is never replayed. New readable reasoning parts are spoken after their
writes settle, through the configured voice/player, each opened by the soft
`think` earcon (agent_media_core/earcons.py; `MEDIA_EARCON_THINK=0` turns it
off) so it is not taken for a reply. They follow the thread's
speech level, mute, gate and holds. Busy/unheard thinking is discarded;
questions and replies take precedence at a sentence boundary. Disabling
finishes the current sentence and drops the rest. The legacy remote-say
command bridge has no cancellable playback and skips thinking speech.

A Claude thread's **step names** are read as thinking too (23 Oct 2026),
because Claude Code stores almost all of its thinking signature-only: a new
`tool` part with a `description` (below) is spoken as a thinking readout,
with the same earcon, gates, tags and cancellation; `thinking_part` is the
tool part's index. Only the description is read, never the command, path,
input or result. Real thinking text goes first (a step waits while a
thought is settling or speaking); a burst of steps keeps only the newest;
a step is read at most once per 15 s per thread (since that thread's last
readout of either kind), is dropped after waiting 15 s, and is never
started while a reply is speaking or speech is held (the mic listening).
Other harnesses read only their reasoning and progress as before.

Thinking playback is tagged `thinking: true`, `thinking_message` (the source
transcript message id) and `thinking_part` (its zero-based part index) on the
live speech row. It adds no speech-history reply or shelf/feed entry; the
reasoning stays in its original transcript part. Temporary rendered clips
are removed after playback.

#### `GET|POST /speech/thinking` — gated (22 Oct 2026)

Settings' **Read thinking aloud by default** applies on this server across
devices to conversations without an override. Initially off. GET returns
`{"ok": true, "enabled": false}`; POST requires `{"enabled": true|false}` and
returns the saved value. Missing or non-boolean values are 400; failed saves
are 500. The enabled generation token is kept in
`<state_dir>/thinking-speech-default.json`. The watcher discovers both pane
and headless conversations, including newly started ones, without an app
connection. Enabling or re-enabling takes a fresh baseline and never replays
old reasoning. Disabling cancels inherited playback at the next sentence;
explicit thread settings still win. All ordinary speech rules still apply.

#### `POST /session/pocket` — gated (28 Sep 2026)

`{"session", "pocketed": true|false}` → `{"ok": true, "session", "until"}`.

A `normal` or `pocket` reply plays at once while its thread is open in the
app, which the server reads off the thread's event stream (§11). The page
closes that stream when it hides, and it hides when the screen goes off too,
but a locked phone is not a look away. So the app, when its page hides
with the screen off (not for another app), sends `true` for the open
thread, and `false` when the page shows again; it sends it whatever the
thread's level. While that lease lasts a `pocket` thread counts as open: its
replies play. A `normal` one ignores it (screen-on only).

- `until`: the lease's end, epoch seconds; 0 after `false`. A lease runs
  out after `MEDIA_POCKET_S` (default 1800; 0 turns leases off), so a thread
  left open overnight does not start talking hours later.
- Kept in core (`agent_media_core/watching.py`,
  `<state_dir>/thread-pocketed.json`), where the Stop hook reads it.
- The session need not exist. 400 `"pocketed must be true or false"`; the
  other refusals and CORS exactly as `/session/pin`.

Pinned by `packages/server/tests/test_reap.py` and
`packages/core/tests/test_toast.py`.

#### `GET` / `POST /session/settings` — gated (26 Sep 2026)

The reply box's model and plan chips. `GET ?session=` → `{"ok": true,
"session", "agent", "live", "model", "model_id", "plan", "can": {"model",
"plan"}, "models": [{"id", "label", "note"}]}` — the label and note are
Claude Code's own (`Haiku 4.5`, `Fastest for quick answers`, from its
`/model` list, cached per version; the bare name and no note when it cannot
be asked). `GET` with no `session` → `{"ok": true, "models", "default"}`, a new
chat's sheet (`default`: Claude Code's line for its default model, or "").
`GET ?agent=opencode` (no session, 28 Sep 2026) → opencode's sheet: its free
models that can call tools — its own `opencode/…` at no cost, and
OpenRouter's `…:free` when the host has a key — `note: "Free"`, opencode's
own first; `default` names what opencode's config starts on ("Big Pickle ·
Free" on a bare install). Paid models are not listed. Any other agent →
`{"models": []}`. `/ask`'s `model` for opencode is a `provider/model` id
(checked by pattern, then `-m <id>` on its command line); anything else is
dropped.
`POST {"session", "model"?, "plan"?}` → the same after the change, plus `told` (a live session was told
now; a parked headless one takes it at its resume).

- `model` is an id from `models` (opus, sonnet, haiku, fable), or `""` when
  not known yet; `model_id` is the full id the session last reported.
- Headless: sessiond's `configure` op sends the CLI's `set_model` /
  `set_permission_mode` control requests, at any time, and keeps the choice
  for `--resume`. `plan` reads the latest `system/init` while the process
  runs (an approved plan leaves plan mode by itself).
- Pane: `/model <id>` typed in, and shift+tab pressed until the footer
  says (or stops saying) "plan mode on"; only between turns — 409 `"wait
  for the turn to finish, then change it"` otherwise, 409 when the session
  is not running. The model is read from the transcript's last assistant
  message. herdr panes: `can.plan` false.
- opencode (28 Sep 2026): `model` and `model_id` are the `provider/model`
  its last message went to (or the one picked since); `models` is its free
  sheet, with the current one first when it is not free; `can.plan` false,
  `can.model` while it runs in a tmux pane. A POST's `model` must be on that
  sheet (400 otherwise): its picker is driven as at the desk — `/models`,
  the model's name typed into the search, Enter — and the composer's footer
  checked; 502 `"opencode did not take <name>"` (after Escape) when it does
  not say the pick. Between turns only, as Claude's. `plan` → 400.
- Codex, pi, Hermes: `can` both false; the app shows neither chip.
- 400 on a model not in the list, a non-boolean `plan`, or neither given.

Pinned by `packages/server/tests/test_session_settings.py` and
`packages/server/tests/test_headless.py`.

#### `GET` / `POST /speech/default` — gated (25 Sep 2026)

`GET` → `{"ok": true, "level", "set"}`; `POST {"level"}` → the same, after
setting it. The level (as `/session/priority`) of every thread with none of
its own: normal until set. `set` (23 Oct 2026) is whether one was ever
chosen, so the app's header chip names only a default picked on purpose. It is the server's, so it applies to every
device paired with it (the app's Settings says so). Kept in
`<state_dir>/speak-priority-default.json`. 400 as `/session/priority`; in
`CORS_PATHS`. With accounts, it would be the account's.

Pinned by `packages/server/tests/test_reap.py` and
`packages/core/tests/test_speak_priority.py`.

#### `GET` / `POST /speech/balance` — gated (19 Oct 2026)

`GET` → `{"ok": true, "balance", "speech", "music"}`; `POST {"balance"}` →
the same, after setting it. How loud speech is next to music in the app:
`balance` -100..100, 0 until set. Above 0 speech is louder (music plays at
`100 - balance`), below 0 music is (speech at `100 + balance`); `speech` and
`music` are the two resulting 0-100 gains. They are the app players' own
gains (Media3Speech, Media3Music), under Android's media volume, which the
phone's keys still move for both. Speech takes its gain as each reply starts
and after a duck, music as each track loads, and a POST sets both at once
(best effort; music ducked under a reply is left to its restore). Only the
`sasonica` targets: the rooms, Termux and the desk keep their own volumes.
The server's, so every device's. Kept in `<state_dir>/speech-balance.json`.
400 for anything but a number in range; in `CORS_PATHS` (David, 19 Oct
2026: "a different volume for speech and music").

Pinned by `packages/server/tests/test_reap.py` and
`packages/core/tests/test_speech_balance.py`.

#### `GET` / `POST /speech/voice` — gated (27 Sep 2026)

`GET` → `{"ok": true, "target", "mode": "phone"|"server", "voice": str|null,
"can_phone": bool, "voices": [<voice>], "languages": [<language>],
"server_voice"}`; `POST {"mode", "voice"?}` → the same, after setting it.
Whether the current speech target's (`target`, the speech default) replies
are rendered by the phone (`phone`) or by the server (`server`), and in
which `voice`. `can_phone` is true for a player that can be handed words:
`sasonica`, or a target with `MEDIA_SPEECH_RENDER_<T>` set or a choice
already made; the app hides the setting otherwise. A POST without `voice`
keeps the one in use.

A `<voice>` is `{"name", "label", "gender", "locale", "language", "accent",
"where": ["phone", "server"] | ["phone"]}`:

- Microsoft's, `name` `edge:<ShortName>` (`edge:en-AU-NatashaNeural`,
  label `Natasha`): its whole list, fetched with edge_tts and kept a day in
  `<state_dir>/edge-voices.json` (offline: the stale list, else a built-in
  twelve English ones). Either side: the phone asks Microsoft itself,
  falling back to a Google voice; the server renders with its edge engine.
- Google's Australian five (`en-au-x-aua-network`, label `Google A ·
  online`): the phone's Android TTS only.

`languages` groups them: `[{"code": "en", "name": "English", "accents":
[{"locale": "en-AU", "name": "Australia", "voices": [<voice>]}]}]` —
English first, then by name; Australia first within English, the rest by
name; Microsoft's voices (by name) before Google's. `voices` is the same,
flat.

`server_voice` is what the server renders in: the chosen voice when in
`server` mode with a Microsoft one — a reply to the target that renders
here then uses the edge engine in that voice, unless the event names its
own engine or voice — else the engine's configured voice (for display).

Kept per target in `<state_dir>/speech-voice.json` as `{"<target>":
{"mode", "voice"}}`, read before the env (`MEDIA_SPEECH_RENDER_<T>`,
`MEDIA_SPEECH_DEVICE_VOICE_<T>`) on every reply. The server's, so every
device's. 400 on another mode, a voice not in `voices`, or `server` with a
phone-only voice (`"<label> is only on the phone: …"`); 409 when the target
cannot render words; in `CORS_PATHS`.

Pinned by `packages/server/tests/test_reap.py` and
`packages/core/tests/test_device_voice.py`.

#### `GET` / `POST /settings/language` — gated (27 Sep 2026)

`GET` → `{"ok": true, "language": "en", "locale": "en-AU", "languages":
[{"code", "name"}], "locales": [{"code": "en-AU", "language": "en", "name":
"English (Australia)"}]}`; `POST {"locale": "en-GB"}` (or `{"language":
"fr"}`, which takes that language's usual country) → the same, after
setting it. The app's language, site-wide: every paired device's. The
**locale** is what Settings picks — a language with its country, since
English alone has voices from ~14 countries (David, 27 Sep 2026: "really
busy") — and Voice shows only that locale's voices (`/speech/voice`'s
`languages[].accents[]` whose `locale` matches); the **language** is what
replies are asked for in (`intake/heard.py`) and what the app's text is
shown in. `locales` is every language-and-country Microsoft has voices for;
`languages` adds those known by name (`agent_media_core/language.py`
`NAMES`). Kept in `<state_dir>/language.json` as `{"language", "locale"}`;
`"en"`/`"en-AU"` until set. 400 `"no such language"`; in `CORS_PATHS`.

**Several accents** (27 Sep 2026): the answer also carries `"accents":
["en-AU", "en-GB"]` — the countries whose voices Voice offers together, the
locale first — and `POST {"accents": [...]}` sets them (all one language,
at least one; 400 `"no such accent"` otherwise). Choosing a language or a
locale starts again with that one accent.

Pinned by `packages/server/tests/test_reap.py` and
`packages/core/tests/test_language.py`.

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

#### `POST /session/ask/part` — gated — BUILT 19 Oct 2026

A question card of several questions is read aloud one part at a time
(David, 19 Oct 2026): the PreToolUse hook speaks the first part ("Question 1
of 3. …"), and each next part is spoken once the one before it is answered.
At the terminal the server sees that itself — the hook starts a watcher on
the pane (`ask_follow.py`) that speaks a part when its tab becomes the
current one. The app draws every part at once and sends them together, so
it says when a part is picked:

`{"session"?, "part": <n, 0-based>, "tool_use_id"?}` (`tool_use_id` alone
finds the thread whose question it is) — every part before
`part` has an answer picked; speak `part`. Send it the first time the last
unanswered part before `part` gets a pick (`part` is the first part with no
answer yet); sending it again, or for a part the terminal already reached,
speaks nothing.

Speaking a part skips to it: the read-out of the parts before is cut as an
answered question's is (the one speaking stops, a queued one is dropped),
so the next part never waits behind the one just answered; the lead-in
before the card is left alone. The same holds when the terminal reaches a
tab.

- 200 `{"ok": true, "spoken": true}`, or `{"ok": true, "spoken": false,
  "why"}` with `why` one of `already_spoken`, `held` (nobody is looking at
  the thread now, so the part is held for the screen like a reply — decided
  afresh for each part, so a first part held and then played from the
  screen does not silence the next), `no_question`,
  `not_this_question` (`tool_use_id` names another), `no_such_part` (0 —
  the hook's — or past the end).
- 400 `"not a session id"`, `"part must be a number"`; 401 as §4.1.
- Where neither can tell — a pane on a host without the server, or
  `MEDIA_ASK_PARTS=0` — the whole card is read at once, as before.

Clients: S (the question card).

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
 "pos": 12.0, "dur": 40.0, "speed": 1.6, "muted": false, "target": "sasonica" | null,
 "replay": false, "turn": {"at": 1790031449.7, "id": 91},
 "queued": [{"session": "5f8c…" | null, "title": "…", "urgent": false, "at": 1790031449.7}],
 "left_paused": ["9a1e…"]}
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
- `left_paused` lists the sessions whose latest reply was paused and then
  let go: the reply's player stayed paused past the speech lock's hold
  (`_BLIND_PAUSE_CAP_S`, or a link that stopped answering while it was
  paused), so it is no longer live, but it was never finished. Its row keeps
  `extras.stopped_at` (with `paused: true`) at the sentence it was paused
  on, so the message has its `resume` (§6.2) and the thread list can keep
  showing it paused. A session leaves the list when the reply is replayed
  (the stop is cleared) or the session says something newer. Only the last
  24 hours; never the live session. Always present; `[]` when none
  (`StateStore.left_paused`, 14 Oct 2026).
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
`speed0`, `vol-`, `vol+`, `mute`, `goto-sentence`, `stop`.
- `stop` (20 Oct 2026) ends the reply being heard, as `media stop` does; the
  next one waiting still plays. It never touches the turn: that is
  `POST /session/stop` (§12). The app's full screen speech rail sends it.
- `arg` is the turn index for `prev` and `replay` (1 = latest, clamped
  1–999), or a history row id for `replay-id` (not clamped).
- `session` on `replay` (25 Sep 2026): the thread the Replay was pressed
  in — its own newest reply (and the question before it, read out from
  its text: the listener's turns have no audio since 18 Oct 2026), never another
  thread's. Without it, `replay` is the newest reply of all, as the desk
  popup's `r`. Not uuid-shaped: 400 `"not a session id"`. Send it from
  inside a thread; with replies held rather than spoken, "the newest of
  all" is often a thread the listener is not in.
- End of reply (`jump-end`) ticks like a Stop (the `cut` earcon), on the
  live reply and on a replay; on a replayed question it moves to the answer
  without one.
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
Same gate as `/speech/ctl`. Fetched on demand, so the message log does not
carry a second copy of every reply's words. (The app asked when the listener
picked "Read from here" under a selection; that chip was removed 25 Sep
2026 — a tap on the reply being said is the way in — and nothing in the app
asks now.)

#### `GET /speech/clip?text=…&voice=…` — gated

One sentence as `audio/mpeg`, for the app in a browser that speaks replies
itself (16 Oct 2026). Such a browser holds `/sessions/events?speech=frames`
as the phone does and plays each `load` op's entry; a `tts:<id>?text=…&voice=…`
entry is sent back here with its `text` and `voice`. An `edge:` voice is
rendered as is; any other (the phone's own voices) gets the server's engine
and voice. Rendered once and kept under `cache/agent-media/web-clips/` (three
days since written). The bearer travels as a header, so the app fetches a
Blob rather than handing the URL to `<audio>`. 400 no text, 413 over 2000
characters, 502 the render failed (`error` says why). Same gate as
`/speech/ctl`. The browser's reports go to `POST /speech/state` like the
phone's; the newest `speech=frames` stream is the one frames go to, so a
browser turned on takes speech from the phone until the phone's stream
reconnects.

#### `GET /speech/clips/<name>?sig=…` — signed link (24 Oct 2026)

A clip this server rendered (`<speech_audio_dir>/<name>`, `.mp3`/`.wav`/
`.ogg`), with no bearer: the phone's clip cache sends none, so the link is
the credential. `sig` is an HMAC of `<name>` under `<state_dir>/clip-sign.key`
(`agent_media_core/clip_sign.py`), good for that one clip. A target gets
signed links when `MEDIA_SPEECH_CLIP_SIGNED_<T>=1` and its
`MEDIA_SPEECH_CLIP_BASEURL_<T>` points here
(`https://<public host>/speech/clips`), which takes server-rendered speech
off the tailnet. 404 for a bad name, a bad signature or a missing file alike.

A phone paired with several servers plays one server's speech at a time
(sasonica roadmap item 7, 20 Oct 2026). Its `POST /speech/state` reports to
a server whose speech is not the one playing carry `elsewhere: {"owner":
"<the server playing>", "deadline": <epoch s>}` (the deadline pushed out
every heartbeat), and `elsewhere: null` once the player is free. While it
stands, this server's broker claim (`am-claim`, `am-claim-play` on
`user-data/am-owner`) is answered with `{"owner": "elsewhere:<name>",
"deadline"}`, so its reply waits as it would for another host and speaks
next once the phone is free. A claim marked `"interrupt": true` (an
interrupt-level reply, a prompt, an urgent say) takes the player anyway; its
frame starts with a `take` op, on which the phone stops the other server's
speech. The phone's word never replaces a live claim of this server's own.
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
| `POST /harnesses/apikey` (28 Sep 2026) | `{"agent": "opencode", "provider": "openrouter", "key"}` (`key: ""` removes it) | `{"ok", "provider", "set", "note", "keys": [{"id", "name", "signup", "set"}]}` · 400 not opencode / unknown provider / not key-shaped / the provider refused it. Checked with the provider first (a provider that cannot be reached: kept, the note says not checked); kept in opencode's `auth.json`, 0600; never echoed. opencode's default row in `GET /harnesses` carries the same `api_keys`. Its free models join the new-chat sheet at once. |
| `GET /harnesses/updates[?refresh=1]` (23 Sep 2026) | – | `{"ok", "updates": [{"name", "installed", "latest", "behind": bool\|null, "line", "checked_at"}]}` |

Only windows `/harnesses/run` opened can be read or typed into.

**Gemini CLI** (17 Oct 2026) is the sixth row. Install and update are
`npm install -g @google/gemini-cli[@latest]`. Its sign-in is `gemini`
itself, run as `env NO_BROWSER=true gemini --skip-trust` with `/auth` typed
in once its prompt is up: Google's sign-in then prints a URL and asks for
the code that page shows, so it works from the phone. `auth` is read from
its files, nothing run: `in` for `GEMINI_API_KEY` (the environment,
`~/.gemini/.env` or `~/.env`; `account` says which) or an
`oauth_creds.json` with a refresh token (`account` the active one in
`google_accounts.json`); `unknown` for Vertex AI and a cloud machine's own
credentials; `out` otherwise. No `logout`.

**Grok** (17 Oct 2026) is the seventh row. Install is xAI's script, run as
`sh -c "curl -fsSL https://x.ai/cli/install.sh | bash"` (a static binary
under `~/.grok`, linked as `~/.local/bin/grok`); update is `grok update`.
Its own `grok update --check --json` answers `behind` and the `line`
(`{currentVersion, latestVersion, updateAvailable}`), as Hermes's check
does — it is not on npm, so `latest` is empty. Sign-in is `grok login
--device-auth`, a URL and a code to type there, so it works from the
phone; `logout` is `grok logout`. `auth` is read from `~/.grok/auth.json`,
nothing run: `in` for an entry with a key or a refresh token (`account` its
email), or `XAI_API_KEY` in the environment; `out` otherwise.

**Harness profiles — more than one login per agent (27 Sep 2026).** A
profile is a harness plus the directory its sign-in, settings and sessions
live in (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `PI_CODING_AGENT_DIR`,
`XDG_DATA_HOME` for opencode; Hermes keeps its own). `GET /harnesses` has a
row per profile: the default one with `"profile": ""`, then each added
one, every row with its own `auth`/`account`/`actions` (`install` only on the
default row: it is the program's). `"profiles": true` on a harness that can
have more.

```
POST /harnesses/profiles {"agent", "name", "path"?}
  → {"ok", "agent", "profile", "dir", "adopted"}   400 bad name / Hermes / taken
POST /harnesses/profiles/remove {"agent", "name", "delete"?: bool}
  → {"ok", "agent", "profile", "deleted"}           404 no such profile
```

`name` is `[a-z0-9][a-z0-9-]{0,23}`, not `default`. Without `path` the
directory is made under agent-media's state dir (`harness-profiles/<agent>-<name>`,
mode 700), and `delete` may remove it; with `path` an existing directory is
adopted and never deleted. `/harnesses/run` (a `login`) and
`/harnesses/logout` take `"profile"`; `POST /ask` takes `"profile"` too (404
when there is none), and a chat in a profile always opens in a pane — sessiond
does not know profiles yet. Every session reader lists each profile's
directory as well as the default, and a resumed session runs in the profile
it was found in (`harnesses.profile_of`).

**pi (27 Sep 2026).** Its sign-in exists only inside its prompt: `login`
opens `pi --no-session` and the server types `/login` once the prompt is up
(`Recipe.login_keys`; pi refuses `/login` given on its command line). The
window is then pi's own picker (an account or an API key, per provider),
driven with `/keys` like any sign-in; it stays in pi afterwards, so the
client closes it. `auth` is `pi auth check --json --no-refresh` on the
provider its settings default to: `ready` is `in`; a provider only an
extension adds (Meridian on red5) is `unknown`, since the check loads no
extensions, never `out`, which would make `/ask` refuse a working chat.

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

**This machine's wiring — BUILT 27 Sep 2026** (roadmap item 3). The same
page, one level down: what makes the agents part of Sasonica on this host.

```
GET  /setup
  → {"ok": true, "rows": [{"name": "mail", "what": "agent mail announced at
     the top of a turn", "kind": "core"|"extra", "state": "ok"|"missing"|
     "absent"|"unknown", "detail": "…"}, …]}
  → 409 when media-setup is not installed here; 502 when it said nothing
POST /setup/run {"name"?: "mail"}
  → {"ok": true, "pane": "%23", "name": "mail", "cmd": "…/media-setup profile --only mail"}
```

`/setup` is `media-setup status --json`, verbatim; `/setup/run` opens
`media-setup profile [--only NAME]` in a window like an install, and
`/harnesses/screen`, `/keys` and `/close` take its pane. No name wires every
row; the profile is idempotent, so a row already right stays. The server
runs the installer that owns the rows and never reimplements one. `absent`
is an extra whose tool is not on this host: nothing to fix from here.

**Restarting this host's services — BUILT 18 Oct 2026.** `media
restart-services` on this host, for another host to ask without ssh
(`media restart-services --host NAME`, docs/reference/version-skew-monitoring.md).
Gated by `may_admin`.

```
POST /services/restart {"only"?: ["sessiond", "canvas"], "after"?: 10,
                        "force"?: false, "dry_run"?: false, "drain"?: true}
  → {"ok", "host", "rc", "lines": [...], "selected": [...], "busy": [...],
     "unknown": [...], "scheduled": {"how", "unit", "log"}|null,
     "own": [...], "after", "drain"?: {…the pending-restart record…}}
  → 400 an unknown name (`unknown`, and `lines` lists the known ones) or a
    bad body; 409 busy (`busy` holds the reasons); 500 a restart or its
    health check failed. A dry run answers 200, with `rc` and `busy` as the
    real run would have them.
```

The body is `restart_services.run()`'s, with the CLI's semantics: no `only`
is everything this checkout owns, short names work, the busy checks are
skipped by `force`, `after` schedules a detached run (a transient systemd
unit, or a detached child under runit and Windows) that checks again before
it acts. The requester is not one of this host's sessiond sessions, so no
session is left out of the busy check. When the restart includes the service
answering the request (`own`: its main process is the server or one of its
parents, or the server is in its systemd cgroup), it is always detached and
`after` becomes 10 s if it was 0: restarted now, the server would be killed
before it answered. This is how the phone's canvas (runit) restarts itself.

`drain` (19 Oct 2026, default true): with `after` and sessiond selected, a
busy host is not refused (409) but drained, as `media restart-services
--after` does: 200, `scheduled` (or `{"how": "joined"}` when a restart was
already waiting) and `drain`, the pending restart (`restart_pending`, §6.1;
docs/reference/version-skew-monitoring.md). `"drain": false` refuses as
before.

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

Target names changed on 27 Sep 2026: `sasonica` was `next` and `abs` was
`app`[^next-rename]. A name from before (a history row's `target`, a saved
choice, an env value) still resolves to the new one; the per-target env
keys moved with it (`MEDIA_SPEECH_SOCKET_SASONICA`, `…_ABS`).

#### `GET /audio/targets` — gated (`auth.gate`)

```json
{"ok": true, "channels": {
  "speech": {"current": "sasonica", "default": "sasonica", "overridden": false,
             "options": [{"name": "abs",   "label": "Phone (Sasonica ABS)",  "available": true,  "why": null},
                         {"name": "phone", "label": "Phone (Termux player)", "available": true,  "why": "was slow or unreachable a moment ago"},
                         {"name": "rooms", "label": "House speakers",        "available": true,  "why": null},
                         {"name": "local", "label": "red5",                  "available": false, "why": "no speech player running on red5"},
                         {"name": "sasonica", "label": "Phone (Sasonica)",   "available": true,  "why": null}]},
  "music":  {"current": "phone" | null, "next": "default", "overridden": false,
             "options": [{"name": "auto",  "label": "Automatic", …},
                         {"name": "rooms", "label": "House speakers", …},
                         {"name": "sasonica", "label": "Phone (Sasonica)", …},
                         {"name": "phone", "label": "Phone (Termux player)", …},
                         {"name": "abs",   "label": "Phone (Sasonica ABS)", …}]}}}
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
- `sasonica` (28 Sep 2026) is Sasonica's own media player, a second Media3
  player beside its speech one that answers mpv's IPC on the phone's port
  6615 (`MEDIA_MUSIC_SASONICA_ENDPOINT`, unavailable when unset). A YouTube
  track is fetched on the phone, copied to this host and handed to the app
  as a clip-server URL, as `abs` does. When the app declines it, the Termux
  mpv plays it. docs/proposals/2026-09-28-music-tab.md.

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
`--where` (and shares, which call it). Music that is playing is not moved
by this call — `POST /music {"action": "move"}` (§6.9a) moves it — and the
MCP `music_play` tool keeps its own default.

The choice is per host, in that host's state dir (`speech-target`,
`music-where`). It matters on the host that starts replies (`origin`); set
on a host that never originates speech, it changes nothing audible.

Clients: S (planned — the speech bar's picker, §14).

### 6.9a What is playing: `/music` (28 Sep 2026)

The app's Media tab (docs/proposals/2026-09-28-music-tab.md, step 1).

#### `GET /music` — gated (like `/speech/now`)

`{"ok": true, "now": {…}, "art": url | null, "chapters": [{"title", "start_ms"}], "where": {…}}`.

- `art` (28 Sep 2026): a picture for the track — a YouTube one's thumbnail
  on `i.ytimg.com` — or null. Sasonica's player is handed it too
  (`user-data/agent-media/art`) for the lock screen.

- `now` is `media music status --json` for whichever player is live
  (Sasonica's own, the Termux mpv, Mopidy), so the tab and the desk popup
  agree: `{"backend", "uri", "media_id", "path", "title", "chapter",
  "pos_ms", "dur_ms", "paused", "speed", "volume", "held"}`, every field
  nullable. A phone player is `"backend": "phone"`; nothing playing is
  `"backend": "mopidy"` with the rest null.
- `chapters` are a mix's tracks (one file, many tracks) when the live file
  is played by Sasonica from the phone's cache, read on the phone with
  ffprobe and kept per file; `[]` otherwise. `now.chapter` names the one
  playing. For such a file `next`/`prev` move by chapter (`prev` restarts
  the chapter when past its first 3 s), and a tap on a track is
  `seek` to its `start_ms`.
- `where` is the `music` block of `GET /audio/targets` (§6.9), for the
  tab's picker; a choice is `POST /audio/target` as before. It adds
  `rooms` (20 Oct 2026): the house speakers one by one, `[{"id", "label",
  "connected", "on", "volume"}]` by label — each a snapclient on the music
  stream (`MEDIA_DUCK_ROOMS_STREAM`) other than the phone's
  (`MEDIA_SNAP_PHONE_PREFIX`); `on` is not muted, `volume` 0-100 (a ducked
  room's own level, not the duck's). `[]` when the host has no music
  stream, `null` when the Snapcast server did not answer. Kept 2.5 s.
- Reading asks the phone, so an answer is kept 1.5 s.

#### `POST /music` — gated (like `/speech/ctl`)

`{"action": "pause" | "resume" | "toggle" | "next" | "prev" | "stop" |
"like" | "seek" | "seek-by", "to"?: seconds, "by"?: seconds}` → the `GET`
answer, read afresh after the action. The `media music` verbs, run as the
desk runs them: `prev` restarts the track when it is past its start, `seek`
takes `to` (absolute), `seek-by` takes `by` (±). 400 for an unknown action
or a missing/non-number `to`/`by` (nothing is run); 502 with `error` (and
the state) when the verb failed.

`"play"` and `"add"` take a `uri` (`yt:`, `http://` or `https://`) and put
it on now or after what is playing (`media music play [--add]`, to the
default place): Recently played's tap.

`{"action": "room", "id", "on"?, "volume"?}` (20 Oct 2026) switches one
house speaker on or off (Snapcast mute) and/or sets its volume (0-100);
at least one of the two. 400 for an id that is not a room in `rooms`, a
non-boolean `on` or a volume outside 0-100; 502 when Snapcast did not
answer. Under a reply's duck the change goes into the duck's restore
baseline and the room sits at the duck level until it lifts, so the
restore never undoes it (agent_media_core.music_rooms).

`{"action": "move"}` (20 Oct 2026) takes what is playing to where the next
untargeted play goes (`POST /audio/target`'s music choice), at the same
spot: it pauses it, plays the same track there (`media music play --where`),
waits up to 90 s for it to start, stops the old place and seeks to where it
was (paused again if it was paused); on a failure the old place resumes.
It runs in the background: the answer comes at once, and every answer for
a minute after carries `"move": {"to", "state": "moving" | "done" |
"failed", "error"}`. Nothing happens (no `move`) when nothing plays or it
already plays there — the phone's players count as one place. With a
radio station on, the station moves instead (`radio.move`): the song is put
on in the new player at the same spot, the old one stopped once it plays,
and what was queued behind it goes back to the station's list, so the
radio plays on there; the station's loop waits while it moves. 409 when
the station cannot play there (`abs`) or plays in the hand-off app, when
what plays is not a YouTube track or web address (a book, a file), or while
a move is under way. The app sends it after a choice in the picker (David,
20 Oct 2026).

#### `GET /music/recent` — gated (like `/speech/now`) (28 Sep 2026)

`{"ok": true, "items": [{"id", "uri", "title", "art", "at", "session", "inferred"}]}`:
what was played, newest first, one entry per track (a `yt:` and a link to
the same video are one), at most 40. `session` is the conversation that
put it on — recorded with the play since 28 Sep 2026, and for older plays
the conversation that spoke in the five minutes before (`inferred: true`),
else null. `title` is the name the play history learned, else the one
YouTube gives for the video (looked up once and kept), else null. Only
things someone asked for: a player loading a file is not listed.

`?kind=music|book|podcast` (29 Sep 2026; default `music`, anything else
400) picks the list, and the answer says which (`"kind"`). Every item has
`kind`. A music item adds `artist`, `album`, `song`, `genre`; a book or
podcast item adds `author`, `series`, `narrator`, `genre` — any of them null
until known. Music's come from the YouTube channel and title and the iTunes
catalogue; books' and podcasts' from the Audiobookshelf item the file is,
a book's genre from iTunes' audiobooks. Lookups run in the background and
are kept (`media-meta.json`), so a later visit knows more. Books and
podcasts are the book player's history less what Threads already holds (a
conversation read back, the agenda, a note sent to the phone); a link to an
Audiobookshelf item is never handed on — its file on the host stands in.

`POST /music {"action": "book", "uri"}` (29 Sep 2026) plays a book or
podcast again (`media book play`, resumed where it was left). Only a `uri`
the book or podcast list holds is accepted (400 otherwise), so this is not
a way to play any file on the host.

#### Radio (29 Sep 2026)

Songs like one track, one after another, until turned off
(docs/proposals/2026-09-29-radio.md). The `GET` answer (and every `POST`
answer) carries `"radio": {"on", "seed": {"id", "title"} | null, "next":
[{"id", "title", "channel", "state"}], "more"?}`: `next` is at most ten
songs to come, `state` `"ready"` (queued in the player or in the phone's
cache), `"fetching"` (downloading now) or null; `more` how many are listed
past those. Off is `{"on": false, "seed": null, "next": [], "available"}`.

- `{"action": "radio"}` starts a station from what is playing;
  `{"action": "radio", "uri"}` plays `uri` and starts one from it. A
  station is YouTube's Mix for the track, listed on the phone, so only a
  YouTube track (400 otherwise) on a phone player — Sasonica's own or the
  Termux mpv (409 when music plays elsewhere; 502 when the Mix could not
  be listed).
- `{"action": "radio", "dj": true}` (29 Sep 2026) starts the DJ's station: a
  model picks songs for the moment (radio_dj). What is playing plays on until
  the first pick is found and downloaded (about a minute), then it replaces
  it. `radio` gains `"kind": "mix" | "dj"` and `"note"`, the DJ's sentence on
  the mood (`""` for a Mix station); a DJ station's `seed` is
  `{"id": null, "title": "Claude DJ"}`.
- **The house speakers** (20 Oct 2026) are a radio player too (`rooms`,
  radio_io.RoomsPlayer): a station started while music plays on the house,
  or with Plays on set to House speakers, runs on Mopidy. Each song is
  fetched on the phone and copied into the rooms cache before it is
  queued (the house cannot fetch from YouTube), so a phone that is asleep
  or away holds the station up; a song that will not come is skipped, never
  streamed. Putting a station on in the house stops the phone's players.
- `{"action": "radio", "dj": true, "player"?: "handoff" | "sasonica" | "phone" | "rooms"}`
  (30 Sep 2026): `handoff` is the Sasonica app's hand-off player — each pick
  asked of the listener's own music app by name (licensed-music proposal,
  step 3; needs MEDIA_RADIO_HANDOFF_ENDPOINT, and no YouTube). Without
  `player`: MEDIA_RADIO_PLAYER, else `handoff` where the YouTube path is
  off, else the music's own place. `radio.available` is true where either
  path is on.
- `{"action": "radio", "off": true}` ends it; what is playing plays on.
- The YouTube path is **off unless the server sets `MEDIA_RADIO_YOUTUBE=1`**
  (29 Sep 2026, licensed-music proposal): `radio.available` says whether a
  station can start here (the app hides 📻 when it is false); starting one
  (`radio`, `uri`, `dj`) is 409 when it cannot, and a station on when the
  switch goes off ends at the next tick. `off`, `dislike` and `play` answer
  as before.
- `{"action": "radio", "play": "<id>"}` plays a song from `next` now (a
  tap on Up next); the songs before it stay to come. 400 when the id is
  not on the list, 409 with no station, 502 when the player would not take
  it.
- `{"action": "dislike"}` is 👎: the song is kept off the station (a second
  👎 on the same channel keeps the channel off) and the next song plays.
  With no station it is `next`.
- A `play` (not `add`) of something else ends the station, from here or
  `media music play`; so does `stop`. A song the station has no record of
  coming up is played on, not taken as the end. A song the player lost
  part-way (the app restarted) is put on again where it was.

Pinned by `packages/server/tests/test_music_route.py`,
`packages/core/tests/test_radio.py` and `packages/core/tests/test_radio_rooms.py`.

Clients: S (the Media tab).

### 6.10 The Organiser (org) — gated (built 22 Sep 2026)

Renamed from Notes on 26 Sep 2026 (routes `/notes/…`, `MEDIA_NOTES_*`, `[notes]`); the old names were retired the same day, once the app calling `/org` was installed.

The Org tree (`~/org`, `MEDIA_ORG_DIR` to move it), browsed, searched and
captured into with no Emacs involved (`org.py`). The layout comes from the
**org profile** (`org_profile.py`, docs/reference/extensions.md §1c). By
default it is plain Org: every top-level `.org` file is a view, captures go to
`inbox.org`, and the roam shelves are the folders under `roam/`. With the
`org-paragtd` package installed, and a tree that has `next-actions.org` (or
`[org] profile = "paragtd"`, `MEDIA_ORG_PROFILE`), it is paragtd's: the
GTD files at the top, and the refile targets and fresh tree below. paragtd
describes its setup in `.paragtd.json` at the top of the tree (written by
Emacs at startup, synced with the tree). When it is there, its files are the
agenda (and the views, except astro, journal and visioning), its keywords are
the default keywords, and its astro `stale_days` ages alerts out. Where a rule
below is the profile's, it says so. Commits are not the server's job: `org-autosync` commits and pushes the tree from every host.
All five routes use `auth.gate`. GETs are gzipped when the client accepts it.

#### `GET /org`

`{"ok", "root", "views": [{"name", "label", "kind": "agenda"|"file"|"folder",
"path"?, "count"?, "states"?}], "profile", "capture_file", "states",
"refile_targets": [{"name", "label", "path", "needs_date"?}], "capture_kinds":
[{"name", "label", "path", "fields", "needs_text"}], "calendar"?}`. `agenda` comes
first. There is one `file` view per profile file that exists (`count` = open
TODO-like headings, `states` = that file's keywords as `{"open": [...],
"done": [...]}`): for paragtd, its core files in its order and with its
labels; for plain Org, the agenda files (below) labelled with their
`#+title:`, a file in a folder named `folder-file`. Then there is one
`folder` view per roam shelf (`count` = notes). `roam-sessions` holds the
agents' own notes, close to a thousand of them; `roam-notes` the zettels,
`roam-concepts` the concept nodes they link to and `roam-memory` a read-only
mirror of the assistants' memory files (agent-sessions `zk`). Under paragtd the
shelves are its fixed folders, shown when they exist; under plain Org they are
whatever is under `roam/`.

The rest is what the app offers rather than hard-codes: `profile` (`"paragtd"`
or null), `capture_file`, `states` (the keywords a file with no declaration of
its own gets), and `refile_targets` (a target with `needs_date` asks for a
date, as the tickler does). `capture_kinds` are the profile's capture
templates the phone can fill (below); plain Org has none.

**Agenda files (plain Org).** `[org] agenda_files` in config.toml, else
`MEDIA_AGENDA_FILES` (colon-separated, the list `media agenda` reads): paths
relative to the notes root or absolute, a directory meaning the `.org` files
in it, as in Org. Files outside the root are left out, since nothing outside
it is served. With neither set, every top-level `.org` file is used. Setup's
`agenda` row copies Emacs' own list in (below).

**TODO keywords.** A file's `#+TODO:` / `#+SEQ_TODO:` / `#+TYP_TODO:` lines
(several join up; `(w@/!)` keys are dropped; with no `|` the last word is the
done state), else `[org] todo_keywords` in config.toml (`["TODO", "NEXT",
"|", "DONE"]`), else the profile's: paragtd's are the manifest's, or without
one `paragtd-todo-keywords` (TODO NEXT WAITING | DONE CANCELLED); plain Org's
are Org's TODO | DONE. A word that is not one of the file's
keywords is part of the title.

#### `GET /org/view?name=<view>[&done=1]`

`{"ok", "view", "items": [...]}`.
- A file view returns its headings as `{path, at, level, state, priority,
  title, tags, scheduled?, deadline?, timestamp?}`. `at` is the 1-based line of the
  heading. DONE/CANCELLED headings are left out unless `done=1`.
- `agenda` returns the headings scheduled or due within 7 days, plus
  overdue ones, each with `date` and `overdue`. A heading with a plain active
  timestamp in its body (`timestamp`) is an event: listed on its day and up to
  7 days ahead, never overdue, as Org shows it. paragtd's agenda reads
  `lunar.org` (the new and full moon routines, such events) and not
  `astro.org`, which has its own view in Emacs; where a manifest still lists
  `astro.org`, an older, scheduled alert more than 2 days past is dropped,
  matching `paragtd-astro-skip-stale`. Repeaters are
  not expanded, so a routine not yet marked done shows as overdue from its
  first date.
  `done=1` keeps finished ones there too, never overdue (25 Sep 2026): an
  agenda digest's items, ticked since it was sent (§6.17 `view`).
- A folder view returns `{path, title, modified}`, newest first, at most 300.
- An unknown view is a 404.

#### `GET /calendar?from=<date>&to=<date>[&done=1]` (20 Oct 2026)

The Organiser's calendar (org_calendar.py): the agenda files' dated headings
for any range of days, for a day, 3-day, week or month view. `GET /org`
says `"calendar": true` where this answers; an older server lacks the key
and the app shows no calendar. Dates are `YYYY-MM-DD`; `from` defaults to
today and `to` to six days after `from`. `to` before `from`, or a range of
more than 92 days, is a 400.

`{"ok", "from", "to", "today", "calendars", "items": [...]}`, sorted by
date, then time. Each Org item is one heading on one day: `{path, at, level, state, priority,
title, tags, type, date, overdue, time?, end_time?, end_date?, repeat?,
due?}`.

- `type` is `scheduled`, `deadline`, or `event` (the first plain active
  timestamp in the heading's title or body; one in the title is taken out
  of `title`). A heading with both SCHEDULED and DEADLINE is two items.
- `time` and `end_time` are `HH:MM` where the stamp has them
  (`<2026-10-22 Thu 14:00-15:00>`). A span (`<…>--<…>`) gives `end_date`,
  its last day (and `end_time` from that stamp); it is listed once, on its
  first day, wherever any of its days is in the range.
- A repeater (`+1w`, `++1m`, `.+2d`) is expanded: an item on every day it
  falls on in the range from its date on, `repeat` the repeater as written.
  Months and years count from the stamp's date, so the 31st falls on each
  month's last day where it has no 31st.
- `overdue`: a scheduled or deadline heading not done, whose date is past,
  is listed on its own day (`overdue: false`) and again on today, when
  today is in the range, with `overdue: true`, `due` its own date and no
  time. Where a repeat of it already falls on today, that repeat stands
  for it and there is no second item. Events are never overdue.
- DONE headings are left out unless `done=1`; the profile may drop stale
  ones (paragtd's old astro alerts), as the agenda does.
- At most 2000 items.

Every item has `source`: `org` for the headings above, `calendar` for an
event from one of the install's calendars (20 Oct 2026). Those are merged in
for the owner only (an account paired here sees Org alone), and
`calendars` lists them: `[{id, name, color, kind: vdir|ics, ok, synced?,
error?}]`, `synced` when the source last changed (epoch seconds), `error`
why one cannot be read. A calendar event is `{source: "calendar", type:
"event", calendar: {id, name, color}, uid, title, location?, date, time?,
end_time?, end_date?, all_day, overdue: false, state: "", priority: "",
tags: [], level: 1}`, with no `path` or `at` (it is not in the Org tree):

- Times are this server's local time. An all-day event's `end_date` is its
  last day (DTEND is the day after); a timed one that runs past midnight has
  `end_date` and `end_time` on that day.
- Repeats (RRULE, RDATE, EXDATE) are expanded in the range; one occurrence
  moved or changed on its own (RECURRENCE-ID) stands in for the one it
  replaces; a cancelled event is left out.

The calendars come from config.toml (calendar_sources.py), never from code:

```toml
[[calendar.sources]]
kind = "vdir"                 # a folder of .ics files, one item each
path = "~/.calendars"         #   (vdirsyncer, khal); a folder of such
                              #   folders is a calendar per folder
[[calendar.sources]]
kind = "ics"                  # a published calendar address
url = "https://…/basic.ics"
name = "Family"               # optional, as is color (#rrggbb)
refresh_min = 15
```

A vdir calendar's name and colour are the source's `name`/`color`, else
the folder's `displayname`/`color` files, else its name. An ICS link is
fetched at most every `refresh_min` minutes into `<state_dir>/calendar/`
(0600), the last good copy standing when it fails; the address is never
logged or sent to the app. Keeping a vdir fresh is the host's (a timer
running `vdirsyncer sync`). Reading needs the `icalendar` package, a
dependency of the server; without it each calendar says so in `error`.
Calendars are read only for now
(docs/proposals/2026-10-20-organiser-calendar.md).

#### `GET /org/read?path=<rel>[&at=<line>]`

`{"ok", "path", "at", "title", "text", "links": [{label, path}], "chats":
[{session, title, at}], "state", "states"}`. `state` is the heading's keyword
(`""` for none, or for a whole note), `states` the file's keywords. `text` is raw Org, capped at 256 KB. With `at`,
only the subtree under that heading is returned. `links` resolves the text's
`[[id:…]]` links to paths. `chats` lists the chats started about this item
with `POST /org/ask`, newest first (at most 20). `draft: true` (whole notes
only, and absent otherwise) marks a zettel waiting for review: a note in
`roam/notes/` still tagged `inbox` (`POST /org/zettel`). A folder view's items
carry the same flag.
- 404 for anything outside the tree, in a dot-dir, a directory, or a file that
  is not `.org`/`.md`/`.txt`.
- 409 when the line at `at` is no longer a heading, meaning the file changed
  under the client. Refetch the view.

#### `GET /org/search?q=<text>[&all=1][&memory=0]`

`{"ok", "q", "notes": [{path, line, text}], "memories": [{id, user, score,
text}]}`.
- `notes` is a case-insensitive fixed-string ripgrep: at most 3 hits per file
  and 30 in total. It skips backups unless `all=1`; paragtd also skips
  `roam/sessions/` and `astro.org`.
- `memories` comes from the memory store (Hippocampus, the `ryer` and `sam`
  namespaces, as `agent-memory-search` uses), searched in parallel. When the
  store is down, the list comes back empty and the notes half still answers.
  `memory=0` skips the store.

#### `GET /org/todos?q=<text>` — gated (`auth.gate`)

`{"ok", "items": [{path, at, title, state}], "more"}` (21 Oct 2026).
- Every open to-do across the profile's editable files (the capture file and
  the file views), filtered by each word of `q` — the list the app's
  link-a-thread sheet picks from. Headings with no TODO keyword are left
  out, done ones too.
- `path`, `at` and `title` are what `POST /org/link` takes to find the
  heading again; at most 40 items, `more` the count beyond that.

#### `POST /org/capture`

`{"text", "kind": "todo"|"note", "memory": bool}` → `{"ok", "path", "at",
"kind", "remembered"}`.
- The capture is appended to the profile's capture file (`inbox.org` for both
  profiles) under an flock, using paragtd's "t" template: `* TODO <first line>`, a `:CREATED:` drawer, then the remaining
  lines as the body. `note` writes a plain heading instead of a TODO.
- A body line starting with `*` is indented one space, so one capture is
  always one entry.
- Unless `memory` is false, the text is also written to the memory store in
  the background, as best effort.
- With `"session"` (21 Oct 2026) the new entry is born linked to that
  thread: its `:SESSION:` property, `[[id:<harness>:<host>:<sid>]]`, sits in
  the same drawer, and the answer adds `"session"` — the link sheet's New
  to-do, one call instead of capture-then-link. 404 when no harness knows
  the session.
- 400 when the text is empty, 413 when it is over 8 KB.

**Capture templates** (`org_capture.py`). With `"kind": "<template key>"`
and `"fields": {id: value}`, the body is filled from that template and filed
where it says, as org-capture would. Answer: `{"path", "at", "kind",
"remembered"}`. The templates are the profile's: paragtd's come from its
manifest, site ones included, or else its defaults t n w k p j. A template is
offered only when the phone can fill it:
- `%?` takes the first line of the text, and the rest becomes the body;
- `%U %u %T %t` and `%<fmt>` are now;
- `%^{Prompt}` is a text field, `%^{Prompt|a|b}` a choice, `%^{Prompt}t`,
  `%^t` and `%^T` a date or date-and-time field;
- `%i` and `%a` are empty.

Anything else (`%(sexp)`, `%[file]`, tag prompts) needs Emacs, so the
template is left out. Each field is `{id, label, type: text|choice|date|datetime,
options?, active?}`, and a date value is `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`.
The targets are:
- `file`: the end, or the top with `prepend`;
- `file+headline`: under the headline, which is made if missing;
- `file+olp+datetree`: under the day, in a year/month/day tree, or
  year/ISO-week/day with `tree_type: "week"`, made in date order.

The file must be an `.org` file inside the tree. 400 for an unknown key, a
missing field or a bad date. `needs_text` false means the text may be empty.

#### `POST /org/say` — `auth.may_control_speech`, like `/speech/ctl`

`{"path", "at"?}` → `{"ok", "path", "at", "title", "chars"}`.
- The note, or with `at` its subtree, is spoken through `media say`, the same
  way as a reply.
- The text is cleaned for listening first (`notes.spoken`): drawers,
  keywords and dates are dropped, links become their labels, and each
  heading becomes a sentence ("Todo: Call the bank.").
- It is capped at 6,000 characters.
- 404 and 409 as for `/org/read`; 422 when nothing readable is left.

#### `POST /org/listen` · `GET /org/listen` · `POST /org/listen/ctl` (15 Oct 2026)

Listening to a note with follow-along (`org_listen.py`). `/org/say` reads a
note as a reply; this plays it on the **book channel**, with no length cap,
a place to resume from, chapters at its headings, and the paragraph being
read known to the line.

`POST /org/listen {"path", "at"?, "line"?}` — `auth.may_control_speech`,
like `/org/say` → `{"ok", "state", "path", "at", "title", "line"}`.
- The note (or with `at` its subtree, as `/org/read` returns it) is projected
  as `media doc` projects a document, and rendered one clip per paragraph,
  synthesised in parallel (`MEDIA_RENDER_JOBS`, default 6). A sidecar beside
  the audio (`<cache>/docs/note-<hash>.json`) keeps each paragraph's
  `{start, end, line}`. The render is cached on the text, so an edited note
  is rendered again, and a heading on its own is a render of its own.
- It plays on `MEDIA_LISTEN_TARGET`, else on the phone's own book player
  when there is one (`MEDIA_BOOK_SOCKET_PHONE`: the file is copied to the
  phone first), else on the book channel's default. The default comes after,
  because it follows speech, and where speech goes to the app (`sasonica`)
  the app's speech player can't open a file on the server.
- With `MEDIA_LISTEN_ENDPOINT` set (the server's `book` frames channel,
  `MEDIA_BOOK_FRAMES_LISTEN`) it plays in **the app's book player** instead:
  a stream from the clip server (`MEDIA_BOOK_BASEURL_PHONE`, nothing
  copied), with the title and the headings as chapters
  (`user-data/agent-media/chapters`) on its own media session and
  lock-screen card, so a headset's pause reaches it; Termux's mpv has no
  session (David, 15 Oct 2026). The app subscribes with `?book=frames` and
  reports to `POST /book/state`, as for `music`. The place is kept on pause
  and stop here, and `resume` with nothing loaded reopens the last note.
  Its volume tops out at 100. At the end the app's player goes idle where
  mpv stays paused; `eof-reached` is read so the state is still `"ended"`.
- `state` is `"rendering"` while synthesis runs (it then plays by itself),
  `"copying"` while the file goes to the phone (the book channel plays it
  when it lands), or `"playing"`.
- `line` (0-based in the `text` `/org/read` gave) starts at the paragraph at
  or after it. When this note is already the one playing, that is a seek.
  Without `line` a note resumes where it was left, or from the top once it
  was heard to within 3 s of its end. Edited since, it is a new render with
  no place of its own: it starts at the paragraph the last render was left
  in (found by its opening words, else its line).
- 404 and 409 as for `/org/read`; 422 when nothing readable is left; 502
  when the book channel refused it.

`GET /org/listen` — gated → `{"ok", "state", "path", "at", "title", "line",
"pos_ms", "dur_ms"}`.
- `state` is `"playing"`, `"paused"` or `"ended"` when the book channel's
  file is a render with timings (matched by name, so the phone's copy
  counts). `"ended"` means paused within 1.5 s of its end: the phone's mpv
  keeps a finished file open there, and `resume` (or `toggle`) on
  `/org/listen/ctl` then starts it again from the top. It is `"rendering"`
  or `"copying"` for one on its way, else `"idle"` with nothing else.
- `line` is the paragraph being read, in the same numbering as `POST`'s.
  The reader marks the block that starts on it.
- The book channel is asked at most once a second.

`POST /org/listen/ctl {"action": "pause" | "resume" | "toggle" | "stop" |
"prev" | "next"}` — `auth.may_control_speech` → the `GET` answer, after it.
A press is one batch of commands to the player, decided from the last
reading (its position run on by the clock, if no older than 6 s), and its
answer is what that made of the player, not a fresh read. The next `GET`
answers the same until the player is read again. On the phone a read is
about 1 s and a batch about 0.65 s. `GET` reads every property in one call,
and a failed read keeps the last good one for 15 s.
`prev` and `next` move a paragraph back or on: the app's mini player keys.
The sheet's keys (15 Oct 2026): `seek` takes `to` (seconds), `skip` takes
`by` (± seconds; 400 without it); `speed+`/`speed-` step 0.1 between 0.5 and
3, and `speed0` is 1; `vol+`/`vol-` step 10 between 0 and 150 (mpv's boost
past 100); `mute` toggles. The `GET` answer for something playing carries
`speed`, `volume`, `muted` and `chapters` (`[{title, start}]`, its headings,
from the sidecar).
Speed and volume are kept between notes (15 Oct 2026, listen-prefs.json):
every press of `speed±`/`speed0`/`vol±` writes them, playing or not, and
each note is loaded with them. With nothing loaded those keys are not 409:
they turn the kept values, and every answer that is not about something
playing (`idle`, `rendering`, `copying`) carries `speed` and `volume` as
the next note will play them (1 and 100 until set). `mute` is not kept.
`prev` restarts the paragraph when it is more than 2 s in, and it is 409
when nothing is being listened to. 400 for any other action.

**A project's document** (15 Oct 2026). `GET /doc/read?path=<absolute
path>` — gated → `{"ok", "path", "title", "text", "fmt"}` (`doc_read.py`):
a `.md`, `.org` or `.txt` file under the projects folder (MEDIA_PROJECTS_DIR,
else ~/projects) or a doc root (MEDIA_DOC_ROOTS). There must be no dot-dir
on the way and no symlink out; 404 otherwise, whose `error` is "outside the
folders the reader opens" when the file is there but not readable from here
and "no such document" when it is not (18 Oct 2026). `title` is a Markdown file's
first `# ` heading (an Org file's `#+title:`), else the file's name, and
`fmt` is `"org"` or `"md"`. `POST /org/listen {"doc": <that path>, "line"?}`
listens to it as to a note, and the `GET` answers about it carry `doc` (and
`title`) instead of `path` and `at`.
The answer also carries `mtime` (the file's, in seconds) and `chats` (the
sessions `POST /doc/ask` started about it, newest first, as a note's). With
`&since=<mtime>` and the file unchanged since, the answer is only `{"ok",
"path", "mtime", "unchanged": true}`: the reader asks every few seconds
while open, to show an edit as it lands (16 Oct 2026).

**A project's documents** (15 Oct 2026). `GET /docs?project=<label>` or
`?path=<folder>` — gated → `{"ok", "project", "root", "docs": [{path, title,
kind, status, date}]}`: the app's project page.
- `project` is a thread's label (`p-agent-media`, or a bare `agent-media`):
  `~/projects/<name>`. `path` is a folder a thread runs in: its git root, so
  a worktree lists its own copy.
- The README comes first, then the dated documents newest first (a
  `Date:` line, else a `YYYY-MM-DD-` name), then the rest by title. `kind` is
  the folder a document sits in (`proposals`, `notes`, `reference`…), and
  `status` its `Status:` line (else a `Status:` anywhere in its opening
  lines). Empty files, hidden and vendor folders are left out, and the list
  stops at 300.
- `area` is `"readme"` (the project's own README), `"docs"` (under `docs/`)
  or `"elsewhere"` (a package's README and the like); the app folds the last
  away. `state` groups proposals as an index like Python's PEP 0 does:
  `"open"` (nothing built, or no status), `"partly"` (some steps built) or
  `"settled"` (built, decided, or closed: not doing, superseded). A decision
  is `"settled"`, and any other kind has `""`.
- 404 for a folder outside the reader's roots, a hidden one, or the projects
  folder itself.
- `&session=<id>` (the thread the page was opened from) adds `thread`: the
  listed paths that thread created or edited (its Write/Edit tool calls,
  its subagents' too) or was started about from the reader (`POST
  /doc/ask`), last touched first; `[]` when none. Reads do not count: nearly
  every thread reads this contract. The app pins them above the list as
  "From this thread" (David, 19 Oct 2026).

`GET /search` answers a `docs` section too, on the first page only:
`[{path, title, project, kind, snippet}]`, at most 8. These are documents
under the projects folder holding every term (ripgrep, so `.gitignore` is
honoured): title matches first, then file name, then text, the most
recently changed first within each. A clone or worktree beside a project
(`agent-media-audio`) counts its copy once. `snippet` is the first line
holding the first term.

#### `POST /org/ask` — gated (`auth.gate`), like `/ask`

`{"path", "at"?, "text", "agent"?}` → `/ask`'s answer for a new session
(§6.3: `session`, `pane`, `opened`, `fresh`, …) plus `path` and `at`.
- The Organiser's chat box (`org_chat.py`). A fresh session opens in the
  notes tree itself, so the agent can read and edit the file. No session need
  have run there before: the server chose the directory, not the client.
- Its first message names the item, then the words:
  `About "<title>" in my Org notes (~/org/<path>, line <at>, <STATE>): <text>`.
  A whole note (no `at`) leaves out the line and the state.
- The session is recorded against the item's file and title (not its line,
  which moves), for `/org/read`'s `chats`. A refile to another file starts
  that list afresh.
- 400 when the text is empty; 404 and 409 as for `/org/read`.

#### `POST /doc/ask` — gated (`auth.gate`), like `/ask`

`{"path", "line"?, "action"?, "text"?, "agent"?}` → `/ask`'s answer for a
new session (§6.3) plus `path` and `line` (`doc_chat.py`; David, 16 Oct
2026: a chat box in the document reader, aware of where you are in it).
- The document reader's box and its paragraph menu. `path` is a document
  `GET /doc/read` would read (404 otherwise). The session opens in its
  project: the git root above it, else its folder.
- `line` is the block's `data-line` (0-based). The first message quotes that
  paragraph whole, as the file has it now, with the heading above it:
  `About my document "<title>" (<path>), under "<heading>", the paragraph at
  line <n>: "<paragraph>": <what> <commit note>`. Without `line` it is about
  the whole document. The quote, not the line, is what the agent goes by:
  a line moves once the file is edited.
- `action`: `ask` (the default) or `rewrite` need `text` (400 without it);
  `shorten`, `expand`, `plainer` and `delete` need none, and any `text` is
  added after. Anything else is a 400.
- The message ends by asking the agent, if this is a change, to edit only
  that file and commit just it on its own (`git commit -- <file>`), so one
  edit is one commit and can be reverted.
- Recorded against the document for `/doc/read`'s `chats`, titled by the
  words, else the action ("Shorten").
- `action: "notes"` takes `notes: [{line, text}]` (1 to 30, each with
  words; 400 otherwise): margin notes made while reading, sent as one
  message quoting each paragraph and its note, in line order, then the same
  commit request. Its chat is titled "2 notes".

`POST /doc/undo {"path", "sha"}` → `{"ok", "path", "undone", "sha"}`: the
reader's Undo (16 Oct 2026). It reverts the document's last commit, the
`last_edit` that `GET /doc/read` answers, which is `{sha, subject, at}` or
null. `last_edit` is given only when that commit touched no other file, is
not the file's first, and the file has no change since. Every `/doc/read`
answer carries it, `unchanged` ones too, because the commit comes after the
write. The revert is a commit of its own, `Undo "<subject>"` (only that
file, as `git checkout <sha>^ -- <file>` then `git commit -- <file>`), so
Undo again puts the edit back. 409 when there is nothing to undo, or when
`sha` is no longer the last edit (the answer then carries the current
`last_edit`).

#### `POST /org/state` · `POST /org/refile` · `POST /org/date` · `POST /org/priority` · `POST /org/link` — gated (`auth.gate`)

These change a heading in one of the profile's files: for paragtd, inbox,
next-actions, waiting-for, tickler, someday, projects, areas and routines; for
plain Org, the agenda files and the capture file. Roam notes cannot
be changed here (400). Every file touched is
flocked while it is read and rewritten, the same lock capture takes.

**Finding the heading.** All three routes take the heading as `at` (its line as
the app last saw it) plus `title` (its text).
- If that line still holds that title, it is used.
- If not, the single heading with that title is used.
- If there are none, or more than one, the answer is 409. The app should
  refresh and ask again; the server never guesses.

`/org/state {"path", "at", "title", "state"}` → `{"ok", "path", "at",
"state", "repeated", "next"?}`
- `state` is one of the file's keywords, or `""` (no keyword); anything else
  is a 400. Closing means moving to one of its done keywords.
- Closing a heading puts `CLOSED: [stamp]` on its planning line, creating
  the line if there is none. Reopening takes the stamp off again.
- A heading whose planning line has a repeater (`+1d`, `++1w`, `.+1m`) is
  not closed. Its timestamps move to the next occurrence, as Org does:
  - `+` moves one interval;
  - `++` moves to the next occurrence after today;
  - `.+` moves one interval from today.

  The heading keeps its state, and the answer says `repeated: true` with
  `next`, the new date.
- **Dependencies.** Under paragtd, or plain Org with `[org]
  enforce_todo_dependencies = true`, closing follows
  `org-enforce-todo-dependencies`: a heading with an open child, or one
  under an `:ORDERED:` parent with an open heading before it (asked again of
  each open ancestor), is refused with 409, and the error names the heading
  in the way.
- **paragtd's trigger.** Closing a step whose `:TRIGGER:` is the form
  `paragtd-sequence-subtree` writes, `next-sibling todo!(NEXT)
  scheduled!("++Nd")`, does what org-edna would: the next sibling takes that
  state and is SCHEDULED N days from today. The answer adds `"trigger":
  "ran"` and `"triggered": {"title", "state", "scheduled"?, "at"}`. Any other
  trigger is not run (`"trigger": "skipped"`; finish it in Emacs), and a
  last step says `"trigger": "no next step"`.
- `at` in the answer is where the heading is now.

`/org/refile {"path", "at", "title", "to", "date"?}` → `{"ok", "path",
"at", "to"}`
- `to` is one of the profile's refile targets. The whole subtree moves, with
  its heading levels shifted to fit.
- Plain Org: `to` is a file view's name, and the heading lands at the top
  level of that file.
- paragtd: `to` is one of next, waiting, tickler, someday, projects or inbox,
  and where it lands follows its capture templates:
  - `next`: under `* Inbox` in next-actions.org, as NEXT.
  - `waiting`: under `* Waiting` in waiting-for.org, as WAITING.
  - `tickler`: under `* Tickler` in tickler.org, with `SCHEDULED:` set to
    `date` (YYYY-MM-DD, required; replaces any earlier SCHEDULED). A target
    that needs a date and has none is a 400.
  - `someday`, `projects`, `inbox`: at the top level of their files.
- A missing file or headline is created.
- 400 when the heading is already in the target file.

`/org/date {"path", "at", "title", "kind", "date", "time"?}` → `{"ok",
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

`/org/priority {"path", "at", "title", "priority"}` → `{"ok", "path",
"at", "priority"}` (24 Sep 2026)
- `priority` is `"A"`, `"B"`, `"C"` or `""` (take the cookie off); it is
  written as Org's `[#A]` after the state. 400 for anything else.
- An `[#A]` TODO whose SCHEDULED or DEADLINE has a clock time is read aloud
  at that time (`media agenda-alarm`, agent_media_core `agenda_alarm.py`).

`/org/link {"path", "at", "title", "session"}` → `{"ok", "path", "at",
"session"}` (21 Oct 2026)
- Stamps the heading's `:SESSION:` property with the thread's roam id,
  `[[id:<harness>:<host>:<sid>]]` — the same stamp an agent puts on a to-do
  it files (`agent-session-uid --link`), here from the app's link-a-thread
  sheet. The org bridge exports a note for the linked session, and org-roam
  backlinks show the to-do from that note.
- The property goes into the heading's drawer, made after its planning line
  when it has none; an existing `:SESSION:` is replaced. An empty `session`
  takes the stamp off, and the drawer with it when it held nothing else.
- 404 when no harness knows the session. 400, 401 and 409 as for the routes
  above.

#### `POST /org/zettel` — gated (`auth.gate`)

`{"path", "action": "keep"|"discard"}` → `{"ok", "path", "action", "title"}`
(28 Sep 2026, `org_zettel.py`). This reviews a draft zettel, as paragtd's
`paragtd-zettel-promote` / `-discard` do in Emacs.
- `keep` takes `inbox` out of the filetags, sets `:ZK_STATUS:` (if the note has
  one) to `permanent`, and drops a Denote name's `_inbox` keyword. `path` is
  the note's new path.
- `discard` deletes the file. A note distilled by `agent-sessions zk` is not
  written again.
- 400 for another action or a note outside `roam/notes/`, 404 as for
  `/org/read`, 409 when it is no longer a draft.

#### `GET /org/setup` · `POST /org/setup` — `auth.may_control_speech`, like `/harnesses`

This is the Notes tab's checklist for a host that has no notes yet
(`org_setup.py`). GET returns `{"ok", "components": [{name, label, state:
"ok"|"missing"|"off"|"down", detail, why, actions, optional}], "search":
"ripgrep"|"built-in"}`. It lists these components:

| name | what it checks | actions |
|---|---|---|
| `org` | the tree, with the profile's files (it is `ok` once the capture file exists) | `clone` (only when `MEDIA_ORG_REPO` is set and the folder is empty); `create` (writes any missing profile files and roam folders: paragtd's set, or for plain Org only `inbox.org`; never overwrites; runs `git init` if the folder is not a repo) |
| `agenda` | plain Org only: whether `[org] agenda_files` is set | `import` (when `emacsclient` is on the host): asks the running Emacs for `org-agenda-files` and `org-todo-keywords` once and writes them into `[org]` in config.toml, leaving the rest of the file as it is; answers `{"files", "outside", "keywords", "enforce_todo_dependencies"}` (it copies `org-enforce-todo-dependencies` too), 502 when Emacs does not answer |
| `astro` | paragtd only: whether the monthly `paragtd-astro.timer` is on, and which years `astro.org` has | `enable` (writes the user unit and timer, enables it, and fills in any missing year now); `run` (this year and next, where missing, with paragtd's generator and `--append`); 502 when the generator fails |
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
                    "title": "", "sentence": "", "target": "sasonica", "replay": false},
            "queued": [{"session": "5f8c…", "title": "…", "urgent": false, "at": 1790031449.7}]},
 "recent": [{"session": "5f8c…", "title": "…", "live": true, "at": 1790053201.164, "rested": null,
             "recap": {"text": "…", "at": 1790052694.779, "source": "claude"}}],
 "replies": [{"session": "5f8c…", "title": "…", "at": 1790053201.164, "text": "Both fixes are live…",
              "live": true, "project": "agent-media", "cwd": "/home/ryer/projects/agent-media"}],
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
- `replies` (3 Oct 2026): every thread whose transcript ends on a reply —
  an assistant message with words, its turn over — newest first: Home's
  Unread replies. Archived threads, and ones working or on a dialog, are
  left out; of the rest, the 30 written to most recently in the last 3 days
  are asked (`transcript.last_reply`, the file's last 256 KB, read again only
  when it changes). `at` is the transcript's mtime — when the reply
  finished — and `text` its last words as plain words on one line (240
  chars): markers out, Markdown read as speech reads it (a table as
  "Option: Keep, Cost: 0.", links and code as their words). The server keeps no read state: the app shows the ones newer
  than when that device last had the thread open. Claude Code, Codex, pi
  Gemini CLI and Grok from the transcript (Gemini's and Grok's folded whole); opencode from its database (`at` its newest part's
  change); Hermes, which has no reader here, from its newest spoken line
  (alerts left out; one scan of the last 600 speech rows, kept 15 s) — so
  a Hermes reply that was never spoken is not listed.
- `project`, `cwd` (22 Sep 2026) on every `recent`, `working`, `replies` and
  `needs_you` row: as on `/targets` (§6.1), `null` when unknown.
- `places`: `/targets.places`. `agents`: every harness (five since opencode, 24 Sep 2026; six since Gemini CLI and seven since Grok, 17 Oct 2026), `present` =
  installed on this host (a PATH lookup only — `/harnesses` has versions and
  sign-in).
- `signins` (27 Sep 2026): Sasonica Shell sign-ins waiting for Approve
  (§6.17a), `[{"id", "code", "client", "host", "at"}]`, `[]` without a shell.
- `alerts` (27 Sep 2026): the open alerts (§6.17, warn and up), worst then
  newest, at most 20: `{"id", "level", "title", "detail" (≤600 chars), "fix",
  "host", "first_seen", "changed_at", "acked_at"}`. Home's Alerts section:
  **Fix it** opens a new chat (`POST /ask`) with the title, detail and fix as
  its words; **Ack** is `POST /alerts/ack`. `[]` when the store will not answer.
- `digests` (25 Sep 2026): the last 36 h of spoken digests (§6.17), newest
  first — `[{"id", "title", "level", "changed_at", "speech": {"id", "heard"}, "n"}]`.
  Home shows each as a Play row; nothing is read out until it is pressed.
  `n` opens it to read (`GET /alerts/digest`).
- `hosts`: this host first, then `MEDIA_DASHBOARD_PEERS` (unset: `hpo`,
  and only when the tailnet knows it, 23 Oct 2026; empty for none). Local: `role` is its roles (`config.host_roles`), comma
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

### 6.11a Which optional parts this server has — gated (built 23 Oct 2026)

#### `GET /features` — gated (`auth.may_control_speech`, like `/dashboard`)

`{"ok": true, "org", "music", "contacts", "bridges", "personas"}`, each a
boolean: whether this server has that optional part. The app shows the
Organiser (and Home's Agenda), the Media tab (and Home's Listening while
nothing is said), and Settings' Contacts, Bridges and Personas only where it
is true, so a plain install is not shown another setup's extras; Advanced
(per device) shows them all, to set one up. An older server answers 404 and
the app shows everything, as before. Code: `features.py`; each check is a
local look, never a network call:

| key | true when |
|---|---|
| `org` | the notes tree's capture file exists (`/org/setup`'s `org` row is ok) |
| `music` | `MEDIA_DUCK_ROOMS_STREAM` is set, or the play history has a music row |
| `contacts` | the contacts store (`contacts.db`) or a Google client file exists |
| `bridges` | a Matrix bridge config is found (`MATRIX_BRIDGES`, §6.26) |
| `personas` | the personas repo is there (`MEDIA_PERSONAS_DIR`, default `~/agent-personas`) |

A check that fails answers false. In `CORS_PATHS`. Pinned by
`packages/server/tests/test_features.py`.

---

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


### 6.12a Acting on a background agent — gated (built 7 Oct 2026)

Stop one subagent, or send it a message, from the agents strip — what
Claude Code's own agent viewer offers (David, 7 Oct 2026; roadmap item 17).
Code: `agent_media_server/agent_actions.py`; pinned by
`packages/server/tests/test_agents.py` and `test_headless.py`. Both answer
`{"ok": true, "session", "agent": <§6.12 row>, "via"}`; refusals are 400
`"not a session id"`, 404 `"no such agent"` (not one of the thread's rows),
409 as below.

#### `POST /threads/{session}/agents/{id}/stop`

- 409 unless the row is `running`. Claude Code's `stop_task` answers success
  for an id it has never heard of (measured 7 Oct 2026, 2.1.289), so the row
  is the check.
- **Headless** (`via: "stop_task"`): the `stop_task` control request through
  sessiond (op `stop_task {session, task_id}`), the one the TaskStop tool
  uses. The thread's turn goes on; the agent's `killed` notification turns
  its row `stopped` on a later read. 409 when the session is not live.
- **A pane** (`via: "message"`): there are only keys, so the main agent is
  asked in the thread to stop it with TaskStop, as for a message below.

#### `POST /threads/{session}/agents/{id}/message {"text"}`

`via: "message"`, `queued`: the main agent is sent, as the listener's turn,
"Please pass this to your background agent <id> (“<description>”) with
SendMessage, word for word, then carry on: <text>". Claude Code refuses the
`send_task_message` control request headless (2.1.289), and a pane has only
keys. The words show in the thread and wait behind a running turn
(`queued`). Any status: SendMessage resumes a finished agent from its
transcript. `text` is whitespace-collapsed, 1–4000 characters (else 400).
409 when the thread is not running, or is stopped on a question (the words
would be typed into its dialog).
### 6.13 The session list as a stream — gated (built 22 Sep 2026)

#### `GET /sessions/events[?ping=<s>][&alerts=<n>][&mic=1][&phone=<kinds>]` — gated (`auth.may_control_speech`, like `/sessions/state`)

What a phone's background notifier holds open while the app is closed
(Sasonica's `NotifyService`): one connection that says when any live
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
  **A row in `approval` carries what it asks** (1 Oct 2026), so a
  notification can offer the answers: `approval: {key, kind, question,
  options: [{n, label}], multiSelect, partial, several}` — §6.2.1's approval
  trimmed; `kind` is `question` (AskUserQuestion) or `tool` (any permission
  prompt, a pane's or a headless one's); `several` means more than one
  question. One tap answers it with `POST /session/answer {session, choice:
  n, key}` (§6.4) only when it is not `multiSelect`, `partial` or `several`
  and has options. A new question is a new `key`, which sends the list again
  though the state has not changed. Absent when the dialog could not be read.
- `alerts` (27 Sep 2026; only with `?alerts=`): the alert store's
  **notices** (§6.17) after cursor `n` —
  `{"last": 57, "notices": [{"n", "id", "change", "level", "title", "detail",
  "fix", "host", "at"}]}`. Sent after the first `sessions` frame, then
  whenever the store's log moves. A notice is a `raised` or `escalated` status
  alert that is still open, or a digest at `warn`+; the latest per alert
  (a raise then an escalation is one), at most 5, none older than 24 h.
  The client keeps `last` and sends it as `?alerts=` next time, so a
  reconnect catches up without repeats; `?alerts=` empty (a first
  connection) is handed only `last`, never the backlog. A cursor past `last`
  (the log was emptied) is answered with `last`. Sasonica posts each notice
  as an "Alerts" notification — how a disk warning reaches a phone whose
  ringer is holding the spoken one.
- `mic` (30 Sep 2026; only with `?mic=1`): `{"asks": [ask, …]}` — every
  open, unexpired ask to speak for another device (§6.20), oldest first,
  **leaving out the connecting device's own** (a phone that has a mic asks;
  a TV does not). Sent after the first `sessions` frame only if there are
  any, then whenever the set changes — an ask, a cancel, an expiry. The
  client diffs by `id`: a new one is a notification, a gone one takes it down.
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

#### `GET /search?q=<words>[&limit=][&before=<at>][&tools=1][&memory=0][&sort=][&after=<at>][&offset=<n>][&since=<at>][&who=][&rooms=][&session=<id>]` — gated (`auth.gate`)

Every thread's messages — both sides; live, closed, headless and archived
threads alike — plus thread titles, recaps and projects, plus long-term
memory as its own section when this host has agent-memory. Code:
`agent_media_server/search.py`; pinned by
`packages/server/tests/test_search.py`. Gzipped when accepted.

```json
{"ok": true, "q": "follow along", "terms": ["follow", "along"], "tools": false,
 "sort": "newest",
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
- **`sort`** (19 Oct 2026): `newest` (the default), `oldest`, or `best` —
  FTS5's bm25 rank, the newest first among equals; text and tool steps are
  merged by it. The answer says `sort`. Each sort pages by its own cursor,
  which `next` holds: `before=<next>` for newest, `after=<next>` for oldest,
  `offset=<next>` (a count) for best; a cursor the sort does not use is
  ignored. A best page can come back short when a hit's thread is not
  answered (machinery, an excluded folder); `next` still says whether there
  is more. `threads` follow the sort too (best: the most fields matched,
  then a title match, then the newest).
- **Narrowing** (19 Oct 2026), applied in the index so a page is full of
  what is asked: `since=<at>` (messages at or after it; threads whose
  latest activity is), `who=me` (the owner's words, `role: user`) or
  `who=agent` (everyone else's; threads are not narrowed by it), and
  `rooms=only` (only Matrix rooms, messages and threads) or `rooms=none`
  (none of them). A bad `sort`, `who` or `rooms`, or a `since`, `after` or
  `offset` that is not a number, is 400.
- **One thread** (19 Oct 2026, the thread's find bar): `session=<id>`
  answers only that thread's messages, narrowed and paged as above (the app
  asks `sort=oldest`). `threads` is `[]`, `docs` and `memory` are left out,
  and the answer says `session` and, on the first page, `total`: how many of
  its messages match, each once though its words and a tool step both
  may (0 for a thread search does not answer). A hit opens
  as any other, `/conversation/log?session=<id>&around=<message>`.
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
  items as `/org/search`'s `memories` (Hippocampus, the `ryer` and `sam`
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
  Subagents' own turns are not. **Matrix rooms** (17 Oct 2026): every room
  `/targets` lists as a Matrix row (the allow list and the owner's bridge
  rooms), by event id — `message` is the id `/conversation/log` gives the
  message, `role` is `user` for the owner and `assistant` for anyone else,
  `thread.harness` is `"matrix"`, `thread.title` the room's title and
  `thread.project` null, so the app's `agent=matrix` filter takes them. A
  room's held messages (the newest page, the sync's since, pages scrolled
  back to) are indexed as they change, an edit with its new words and a
  message taken back dropped; older history is paged in by the background
  thread, 10 pages of 50 a minute across the rooms, down to each room's
  start (and first any gap left while the canvas was down). A query never
  waits on the homeserver, so a room's oldest words may come a few minutes
  after it is first read (`indexing` does not count them). A room no
  longer served (off the list, or the sync off) is not answered; its rows
  stay until a rebuild. **Left out:** deleted and sealed threads, rooms
  included; threads in an excluded folder
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
  thread by `at` instead. A Matrix hit opens the same way (17 Oct 2026):
  the room's log takes `around=<event id>` and finds it, paging back for
  it if the room does not hold it yet.

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
id. A Matrix room has a `files` row of its own, `matrix:<room id>`, holding
how far back its history has been paged (17 Oct 2026). The canvas builds it on a background thread at nice 15 after start,
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

### 6.15 Move a thread to another project — gated (built 23 Sep 2026; waits out a turn since 15 Oct 2026; waits out questions and background agents, cancellable, since 19 Oct 2026)

#### `POST /session/move` — gated

`{"session", "project"?, "cwd"?}` → `{"ok": true, "session", "project",
"cwd", "restarted": bool, "pane": null | "%23", "live": bool, "folder":
null | "…", "folder_error"?: "…", "pending"?: true, "why_waiting"?:
"turn" | "question" | "agents", "move_pending"?: {…}}`.

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

**A busy session's move waits** (15 Oct 2026 for a turn, it was a 409
before; 19 Oct 2026 for the rest). A move closes or parks the session's
process, so it is never made while that would lose something. The session
is busy, and `why_waiting` says with what, while:

| `why_waiting` | What would be lost |
| --- | --- |
| `"turn"` | a turn running, starting, or a message queued behind one — whatever it had not written yet |
| `"question"` | a permission dialog or a question (`approval`) — the request itself |
| `"agents"` | background work after the turn: a subagent still running (§6.12, read off its transcript, any session), or for a headless session any task sessiond still hears from (`tasks`, a background Bash included). A pane session's background Bash cannot be seen from the server, so only its subagents hold the move |

A session that is not running is never busy. Busy, the answer is `ok:
true, pending: true, why_waiting, move_pending` with nothing moved yet; the
move is kept in `<state_dir>/moves_pending.json` (so a canvas or sessiond
restart keeps it) and the server carries it out (all four steps) the moment
the session is free — woken by sessiond's own events for a headless session
(a 15 s look as the backstop), every 3 s for a pane. A message sent
meanwhile only means more waiting, and `why_waiting` follows what it is
waiting on now. sessiond itself refuses to park a session for a move while
any of this is true (`busy`); a turn that starts between the look and the
move puts the move back to waiting. A later move replaces a waiting one
(one per thread); a server restart picks waiting ones up again.

**When a waiting move cannot be made** — its directory is gone, a step was
refused, or the session was still busy six hours on — it is dropped and
kept with why in `<state_dir>/moves_failed.json` until the next move of
that thread or a cancel, and the thread shows it (`move_failed`). The
answer to the original `POST` was already sent, so this is the only place
it is said; nothing is raised as an alert.

400 for an unknown project or a directory that is not there, 404 when no
harness still holds the session, 401 unpaired.

**How a thread shows it** (additive: absent when there is none, so old apps
see nothing new). Every `/targets` row and the thread snapshot (§11) carry

- `move_pending: {"project": "…" | null, "cwd", "since": <epoch s>,
  "why_waiting"}` while a move waits, and
- `move_failed: {"project": "…" | null, "cwd", "at": <epoch s>, "error"}`
  after one failed,

and the thread stream sends a `move` event (`{"move_pending",
"move_failed", "project", "cwd"}`, nulls when none) when either changes or
the move lands.

#### `POST /session/move/cancel` — gated

`{"session"}` → `{"ok": true, "session", "cancelled": bool}`: the waiting
move is dropped (`cancelled: false` when none was waiting) and any
`move_failed` with it. A move already under way is not undone. 401 unpaired,
400 for something that is not a session id.

**What the folder move costs.** Audiobookshelf reads a folder that moved as
a *new* item — new id, no progress, the old one left behind (the same reason
`book_tracks.folder_for` keeps its first folder for ever), so a moved
conversation loses its place in the old ABS app. David decided on 23 Sep
2026 that the files and the app agreeing is worth more than progress in an
app on its way out. The title part of the folder is kept exactly as it was:
only the project above it changes.

Clients: the chat app's thread ⋯ menu ("Move to project…") and a long press
on a row in the thread list; a waiting move shows on the row and in the
thread's header, which offers Cancel.

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
| gemini | `~/.gemini/tmp/<slug>/chats/session-<stamp>-<id[:8]>.jsonl` (`GEMINI_CLI_HOME` moves it; each file's first line read once, for the whole id) |
| grok | `~/.grok/sessions/<cwd, url-encoded>/<id>/updates.jsonl` (`GROK_HOME` moves it; a subagent's session folder, named in its parent's `subagents/`, left out) |

`harnesses.stored()` (core) is that sweep, and it is **stat-only** — one
`scandir` per directory, no transcript opened, ~4,000 conversations in
0.06 s — so the list can be built on every poll.

- **Every row carries `harness`**: `"claude" | "codex" | "pi" | "hermes" | "opencode" | "gemini" | "grok"`.
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
- `spoken` (digest only, ≤12,000 chars; 25 Sep 2026): its read-out. The
  server renders it **held** in the background (`media say --hold`): nothing
  is said; the desk gets a toast (`prefix y` plays it) and the row's `speech`
  gets an id to play. A digest reported without `spoken` drops the last one's.
- A raise files one TODO in `~/org/inbox.org` carrying `:ALERT_ID:`; a clear
  appends `Cleared` and marks a routine one (never acked, never `needs`) DONE.
  `MEDIA_ALERTS_INBOX` points elsewhere, `0` turns it off.

- `actions` (status only, 22 Oct 2026): up to three `{"id":
  "[a-z][a-z0-9-]{0,31}", "label": ≤40 chars}` buttons, for a producer that
  can act on the answer itself (malformed ones are dropped). The latest
  report's word, like the title; a report below warn drops them.

#### `GET /alerts[?open=1]` — gated

`{"alerts": [...], "at"}`: open ones first (worst, then newest); without
`open=1`, then digests and clears from the last 14 days. Each row: `id, kind,
level, peak, step, title, detail, fix, host, every_s, first_seen, last_seen,
changed_at, cleared_at, acked_at, open, speech, actions` (`actions` `[]` when
none or not open; `/dashboard`'s `alerts` rows carry it too). `speech` is null with no
read-out; else `{"id", "heard"}`, `id` null while it renders (or if it never
did), otherwise the history row `POST /speech/ctl {"action": "replay-id",
"arg": id}` plays — which marks it heard.

#### `GET /alerts/digests[?id=&before=n]` — gated (25 Sep 2026)

`{"digests": [{"n", "id", "at", "level", "title", "view", "speech"}]}`: every digest
reported in the last year (the landscape watch's for good), newest first, 60 a page; `id` narrows to one
digest, `before` (an `n`) pages back. A digest's `detail` may run to 64,000
characters (markdown), so it is only in:

#### `GET /alerts/digest?n=` — gated (25 Sep 2026)

`{"digest": {"n", "id", "at", "level", "title", "view", "detail", "speech",
"prev", "next"}}`, 404 for an unknown `n`. `prev`/`next` are the same id's neighbours
(null at either end). `/dashboard`'s `digests` rows carry `n`, their latest.

`view` (reported with the digest, `[a-z0-9_-]{1,32}`, else null) names the
Organiser view (§6.10 `GET /org/view?name=`) whose items the body's lines
are: the org agenda digest reports `agenda`, one line per item. The app shows
that view's live rows for the lines it can match by title — tickable, and
opening their heading — and the rest as no longer on it.

#### `POST /alerts/ack {"id"}` — gated

Seen it: kept open, not re-notified; a clear leaves its TODO open. 404 for an
unknown id. The next raise forgets the ack.

#### `POST /alerts/action {"id", "action"}` — gated (`auth.may_admin`; 22 Oct 2026)

Runs one of an open alert's `actions` through the handler registered for its
id's prefix (`alerts.ACTION_HANDLERS`), which does the work and settles the
alert. → `{"ok": true, …the handler's answer}`. 404 unknown id; 409 not open,
or not one of its actions now (already answered); 501 no handler on this
host; 502 the handler refused or failed, with its `error` (a sentence to
show). Today: `calendar.rye.booking.<n>` (the Rye booking mirror,
`booking_calendar.alert_action`; docs/reference/shared-calendar.md) with
`apply` | `apply-quiet` | `cancel` | `keep`. The app's Home renders each
alert's `actions` as buttons (22 Oct 2026), asks first for any but `keep`, and
shows the answer's `message` or `note` (else "Done: <label>") or its `error`;
Fix it still opens a chat with the alert, whose `fix` names the matching
`media calendar bookings-decide` commands.

The `alerts` frame on `/sessions/events` (§6.13, 27 Sep 2026) carries the
notifying changes to the phone; Home's Alerts section (27 Sep 2026) reads
`/dashboard`'s `alerts` (§6.11), with Fix it and Ack.

### 6.17a Sasonica Shell sign-ins — approved on the phone (built 27 Sep 2026)

When this machine's Sasonica Shell runs with `SASONICA_SIGNIN=app`, adding it
as a connector shows a six-character code on the sign-in page, which waits
for the owner. Code: `agent_media_server/shell_signin.py`; the shell's side in
sasonica-shell `docs/tools-and-approvals.md` §6. Pinned by
`packages/server/tests/test_shell_signin.py`.

```
GET  /shell/signins            gated (may_control_speech)
  → {"ok", "signins": [{"id", "code", "client", "host", "at"}], "shell": bool}
POST /shell/signin {"id", "approve": bool}     a device with the enrol bit (§9)
  → {"ok", "id", "status": "approved"|"denied"}
  → 400 not an id · 403 not_enrolled · 409 no shell here · 404 not waiting (expired)
```

```
GET  /shell                    gated (may_control_speech)
  → {"ok", "shell": bool, "url": "https://<worker>/mcp" | null, "signin": "app"|"access"|null,
     "secret_url": "https://<worker>/<secret>/mcp" | null}
```

`secret_url` is the shared secret URL (a password for the shell), given only
to a device with the enrol bit (§9) and null for any other.

`url` is the plain sign-in URL (no secret), only when OAuth is on; the
app's Settings → Connect an assistant copies it and opens the assistant's
connector page (27 Sep 2026).

`/dashboard` carries the same list as `signins` (cached 5 s). The server
reads the shell's own config (`~/.config/sasonica/env`, `SASONICA_CONF`), signs
the decision with its relay key (`HMAC-SHA256(key, "signin\n<id>\n<approve|deny>")`)
and passes it to the Worker's runner API with the runner token; the Worker
checks both. The shell's runner raises a `needs` alert,
`shell.signin.<runner>`, while any wait, so the phone is notified. `client`
is what the assistant calls itself (unverified); `host` is where its access
goes. The app shows the code so it can be matched against the page.

### 6.18 Files shared to the app — gated (built 25 Sep 2026)

The phone's share sheet hands the app text, a link or files (Sasonica's
`ShareInPlugin.java` → `routes/share.tsx`). The text needs no route of its
own: it goes into a draft (§6.2), `/org/capture` (§6.10) or `/share` (a
link, §6.3). A file is sent here first and the app puts a line per file,
`Shared file: <path>`, into the words it sends, so the assistant can open it.
Code: `agent_media_server/uploads.py`; pinned by
`packages/server/tests/test_uploads.py`.

#### `POST /upload?name=<file name>` — the file as the raw body

```
Authorization: Bearer <device token>
Content-Type: application/octet-stream
Content-Length: 48213
→ {"ok": true, "path": "/home/ryer/shared/2026-09-25/photo.jpg",
   "name": "photo.jpg", "size": 48213}
```

- Kept under `~/shared/<YYYY-MM-DD>/` (`MEDIA_UPLOAD_DIR` moves the root),
  never over another file: a second `photo.jpg` that day is `photo-2.jpg`.
- The name is cut to its last path part, stripped of control and shell
  characters and a leading dot, and capped at 120 characters; none is `shared`.
- The only route past the 64 KiB body cap (#139): up to `MEDIA_UPLOAD_MAX_MB`
  (512). Streamed to a `.part` file, renamed when whole; the bearer is checked
  before a byte is read.
- Refusals: 401/403 as `/reply` · 411 no `Content-Length` (or an empty file) ·
  413 over the limit · 400 a body that ended short (nothing is kept).

#### `GET /upload?path=<its path>` — the file back (29 Sep 2026)

```
Authorization: Bearer <device token>
→ 200, the file's bytes; Content-Type from its name (image/png, …;
  application/octet-stream when unknown), Cache-Control: private, max-age=86400
```

- What the app's file chips draw a photo from, in the reply box and in the
  thread: the `Shared file:` lines stay in the words, and the app shows each
  as a chip (a thumbnail, or the file's name) instead of the line.
- Only a file under the upload root: the path is resolved, symlinks included,
  before it is checked. Outside it, a folder, a `.part` still being written
  or a file that is not there is the same 404. 401/403 as `/reply`.
- Streamed a chunk at a time; open cross-origin like the POST.

### 6.19 Take back your last message — gated (built 27 Sep 2026)

Code: `agent_media_server/retract.py`, the drivers' `interrupt(drop_queued=)`.
Pinned by `packages/server/tests/test_retract.py`. The app's tap on your own
latest message: **Cancel** takes it back; **Edit** takes it back and puts its
words in the reply box, to be sent again changed. A correction, not a rewind
(David, 27 Sep 2026): the original stays in the transcript, struck through.

#### `POST /session/retract {"session", "id"?, "text"?}`

`id` is the message's (a transcript uuid); absent for a message the app has
sent but not yet seen come back, which is then matched by `text` (its words).
One of the two is required.

- **Working** → the turn is interrupted through its driver, with the stop's
  `after` speech cutoff (§12), and the messages **queued behind it go too**:
  a headless session's `interrupt` carries `cancel_queued`; a Claude pane
  showing "Press up to edit queued messages" gets Up (the queue back into the
  composer), Ctrl-U until the words have left it, then Escape — Escape alone
  runs the queue as the next turn (measured, Claude Code 2.1.283). Codex and
  herdr panes get the Escape only. A session on a dialog is not pressed.
- Either way the message is **marked**: `retracted: true` on it in
  `/conversation/log` and the thread stream, from then on
  (`<state_dir>/retracted.json`).
- The **next** `/reply` to the session opens with `(I took back my last
  message, “<its first 80 characters>” — disregard it.)` and a blank line,
  once; the bubble is shown without it.

Response: `{"ok": true, "session", "retracted": {"id", "text"}, "interrupted":
bool, "why", "state"}`. Refusals: 400 a bad id or neither `id` nor `text` ·
401/403 as `/reply` · 404 a session nothing knows. In `CORS_PATHS`.

### 6.20 The phone as the TV's mic — gated (built 30 Sep 2026)

A TV with no microphone of its own (the living-room Google TV: no
`android.hardware.microphone`; its remote's mic is kept by Google for its own
search) borrows the phone's. Its mic key asks here instead of dictating; every
phone on `/sessions/events?mic=1` (§6.13) gets the ask in a `mic` frame and
posts a heads-up notification, "Speak for <device>"; a tap opens the same
thread on the phone already listening, and the words are **sent from the
phone** (`/reply`) — the TV just sees them arrive. Only a TV that cannot hear
(TV ui mode and no `FEATURE_MICROPHONE`) does this; phones, tablets and a TV
with its own mic keep the mic key as it was. Code:
`agent_media_server/mic.py`. Pinned by `packages/server/tests/test_mic.py`
and the `mic` tests in `test_session_events.py`.

An **ask**:

```
{"id": "3f9a01c2", "device": "Living room TV", "device_id": "d_1a2b3c4d5e6f",
 "session": "0f1e…" | null, "title": "Sasonica web" | null,
 "at": 1790053383.513, "expires": 1790053473.513,
 "taken": false, "chat": null}
```

- `id`: 8 hex. `device` / `device_id`: the asking device's name and id (§9);
  an ABS login (not a device) asks as its username with `device_id` null.
- `session`: the thread the TV is showing; null is a new chat. `title`: that
  thread's title when a live session has one (the `/sessions/state` sweep),
  else null.
- `expires` = `at` + 90 s. Kept in memory: a server restart drops open asks.

#### `POST /mic/ask {"session"?}` — gated like `/reply`

```
{"session": "0f1e…"}          → {"ok": true, "ask": {…}}
{}  or  {"session": null}      → a new chat
```

- One open ask per asking device: a new ask from the same device replaces
  its old one (the old id is gone from the next `mic` frame).
- Wakes the session stream at once — the phones hear it without waiting for
  the watcher's 3 s tick.
- Refusals: 400 `not a session id` · 401/403 as `/reply`.

#### `POST /mic/cancel {"id"}` — gated the same

The TV's Cancel, its words arriving in the thread, or the phone saying it
took the ask. Any gated caller may cancel any ask.

- → `{"ok": true, "id"}` · 404 `no such open ask` (unknown, cancelled or
  expired). Also wakes the stream.

#### `POST /mic/answer {"id", "session"?}` — the phone that took it (20 Oct 2026)

What the phone sends instead of a cancel, so the TV can follow it into a new
chat (David, 20 Oct 2026: "I wonder how it would go if it did follow it").

- Without `session` — the tap: the ask is **taken**. It leaves every phone's
  `mic` frame (their notifications go), but is kept `TAKEN_S` (180 s) from
  now for the TV to read.
- With `session` — the new chat the phone's words started (the `/ask` that
  answered a new-chat ask): kept as `chat`, and the 180 s start again.
- → `{"ok": true, "ask": {…, "taken": true, "chat": "…" | null}}` · 400 `not a
  session id` · 404 `no such open ask` · 401/403 as `/reply`.

#### `GET /mic/ask?id=` — the ask as it stands (20 Oct 2026)

The TV on its new-chat screen, while its bar is up, reads this every couple of
seconds; once `chat` is set it opens that thread. An ask carries `taken`
(false until a phone takes it) and `chat` (null until a new chat started).
→ `{"ok": true, "ask": {…}}` · 404 when gone (cancelled, expired, a restart) ·
401/403 as `/reply`. In a thread the TV needs none of this: the words arrive
there.

Expiry is noticed by the stream's watcher on its tick (≤ 3 s late), and a
`mic` frame without the ask follows. Both routes are in `CORS_PATHS`.

### 6.21 The phone as an agent's eyes and hands — host asks, device answers (built 1 Oct 2026)

An agent on this host asks David's phone for something only it has — a
photo first (location and Do Not Disturb to come); David allows or denies
on the phone, and the agent's tool gets the outcome. Every ask needs a yes;
nothing is standing. Code: `agent_media_server/phone.py`, the tool
`agent_media_core/phone_ask.py` (MCP `phone_ask`, in the narrow set).
Pinned by `packages/server/tests/test_phone.py`; the frame by
`test_session_events.py`. Proposal:
`docs/proposals/2026-10-01-the-phone-as-eyes-and-hands.md`.

- `POST /phone/ask {kind, why, session?, params?}` — **the host's own token only**
  (`X-Auth-Token` or `Authorization: Bearer`, `~/.amux/auth_token`), never
  a device's: an agent asks, a phone does not. `kind`: `photo` | `location`
  | `dnd`; `why`: one sentence, shown as-is, ≤ 200 characters, 400 when
  empty; `session` names the thread (the tool finds its own). → `{ask}`.
  When no stream that can do `kind` is connected, the ask is settled
  `no_phone` at once. One open ask per session: a second one settles the
  first `cancelled`. `dnd` (3 Oct 2026) needs `params.until`, epoch seconds,
  at least a minute and at most 12 hours ahead (else 400); the ask carries
  it as `until`.
- `GET /phone/ask?id=&wait=<s>` — host token. Long-polls up to `wait`
  (≤ 60 s) for the ask to settle; → `{ask}`, 404 when unknown (a restart).
- `POST /phone/answer {id, decision: "allow"|"deny", result?}` — a paired
  device (the app's gate). Allowed with `result: {error}` is `failed`; a
  photo allowed with no `result.path` is `failed`. 409 `{ask}` when it is no
  longer open (another phone answered, it timed out or was cancelled).
- `POST /phone/cancel {id}` — host token: the tool gave up.

An ask: `{id, kind, why, session, title, at, expires, status, result?,
error?}`. `status`: `open` → `ok` | `denied` | `failed` | `timeout` |
`cancelled` | `no_phone`. Open for 5 min (photo) or 2 min (the others);
kept 2 min after settling for the tool's last poll. A photo's `result` is
`{path, width, height}`: the phone uploads it first (§6.18) and answers with
the path the upload returned. A dnd's is `{on: true, until}` (allowed
without `on: true` is `failed`): the phone set Do Not Disturb to priority
only, and puts back what it was at `until` — unless it was changed by hand
in between. A location's (20 Oct 2026) is `{lat, lon, accuracy_m?, at,
precise}`, checked here: lat/lon missing or out of range is `failed`, other
keys are dropped; `precise` is false when the app's "Rough location only" is
on (the phone rounds to 0.01°, accuracy at least 1 km). The phone offers
`location` in `?phone=` only while Settings → "Let agents ask where I am" is
on (off by default).

**The `phone` frame.** `GET /sessions/events?phone=photo[,location,dnd]`
(§6.13) says what this phone can be asked for (unknown kinds dropped) —
and, while the stream is up, makes asks of those kinds wait for it. Frame:
`{"asks": [ask…]}`, every open ask of those kinds, oldest first; after the
first `sessions` frame if there are any, then whenever the set changes (an
ask, an answer from any phone, a cancel, an expiry). A stream that died is
only noticed at its next write (the ping), so an ask made in that window
waits and times out rather than saying `no_phone`.

**Audit.** Every settled ask is a line in
`$XDG_STATE_HOME/agent-media/phone-asks.jsonl`: id, kind, why, session,
title, at, status, device, settled_at, a photo's path, an error. No
coordinates.

### 6.21b The phone's location, shared in the background (built 20 Oct 2026)

The one standing thing, and the phone's own choice: with Settings → "Share
my location in the background" on (off by default; David, 20 Oct 2026:
"location data … as an optional extra with the default being off"), the app
posts where it is now and then, and an agent reads it without asking. Code:
`phone.py` (`share`, `shared`); tests in `test_phone.py`.

- `POST /device/location {lat, lon, accuracy_m?, at?, precise?}` — a paired
  device only (401 otherwise). `at` is the fix time, epoch seconds (default
  now); 400 for lat/lon out of range or an `at` more than 5 min ahead or
  older than 2 h. `{off: true}`: the switch went off, forget this phone's.
  → `{ok: true}`.
- `GET /device/location` — **the host's own token only**. → `{location:
  {lat, lon, accuracy_m?, at, precise, device, age_s} | null}`: the freshest
  phone's, null when none shares or the newest is over 2 h old.
- Kept in memory only, the newest per phone: never written to disk or
  logged, gone on a restart until the next post.
- `phone_ask(kind="location")` (core `phone_ask.py`) answers from this when
  it is under 30 min old (`shared: true`, nothing asked), else asks (§6.21).

### 6.21c The phone's mic listens, reported (built 25 Oct 2026)

David, 25 Oct 2026: "In hands-free, so often the mic doesn't seem to accept
my spoken text, even though it often shows up in the black screen
transcript." The phone's own listen log (Readouts.java, loopback :8774) is
out of reach off the home network, so the app reports each listen here,
outbound (sasonica-app `lib/listenLog.ts`). Code: `listen_log.py`; tests in
`test_listen_log.py`.

- `POST /device/listen-log {lines: [{t, at, outcome, text?, partial?, error?,
  confidence?, voiceNear?, session?}]}` — a paired device only (401
  otherwise); 400 without a `lines` list. `at`: `handsfree`, `countdown`,
  `confirm` or `dictation`. `outcome`: `taken`, `command`, `echo` (dropped as
  the reply's own voice; `voiceNear` says a voice was playing), `error`,
  `empty`, `stale` (heard after the listen was retired), `rescued` (the
  listen ended on an error or empty, and its last partial was taken as the
  words). Unknown fields are dropped, strings cut to 400 characters, 200
  lines a post. → `{ok: true, kept}`.
- Sent in batches: soon (5 s) after a line that dropped words, at 20 lines,
  else a minute after the first; a failed post keeps them for the next.
- Appended to `<state>/listen-log.jsonl`, one JSON line each with `received`
  and `device`; the newest 5000 kept.

### 6.21a Restarting the phone's services — down the connection it opens (built 18 Oct 2026)

`media restart-services --host p8a` on red5 must not need Tailscale or any
connection into the phone (David, 18 Oct 2026: "I don't want to have to rely
on tailscale"). The phone's canvas listens on 127.0.0.1 only, so §6.6's
`POST /services/restart` cannot reach it. Instead the restart rides the
phone's own outgoing connection: the stream its Termux jobs worker holds open
to red5 (`GET /jobs/events`, `agent_media_server/phone_jobs.py`,
`deploy/phone/service/phone-jobs/phone-jobs.py`; it reaches red5 through the
tunnel, see `docs/proposals/2026-10-03-off-the-tailnet.md`).

It is a new kind of job on that channel, not on §6.21's asks. An ask waits
for David's yes on the phone, and is answered by the Sasonica app. A restart
needs no one at the phone. It is the jobs worker that runs agent-media's own
code in Termux, and it is already paired with red5. What it carries is
narrowed to a restart's four fields: no command.

- `POST /jobs/restart {device, only?, after?, force?, dry_run?}`: the host's
  own token (`media restart-services --host` on this host:
  `restart_services.run_device`), else a caller `auth.may_admin` lets in.
  `device` names the jobs worker (`MEDIA_PHONE_JOBS_DEVICE`) by its id, its
  whole name or the first word of its name, in any case (`p8a` for
  `p8a Termux jobs`).
  - **404** `code: not_a_device`: not that device. The CLI then tries NAME as
    a §6.6 peer.
  - **400**: any other field, or one of the wrong shape. `only` is a list of
    service names (`[A-Za-z0-9][A-Za-z0-9@._-]*`), `after` is 0 to 86400 s,
    `force` and `dry_run` are booleans.
  - **503**, with `rc` 1 and `lines`: no worker connected (offline); a worker
    whose stream did not ask for restarts (too old: pull on the phone, then
    restart phone-jobs); not taken within 10 s; or not answered within 60 s.
    The job is gone from the queue in every case.
  - **200**: the phone's `restart_services.run()` dict (`rc`, `lines`,
    `selected`, `busy`, `unknown`, `scheduled`, `own`, `after`, `host`), and
    `device`, the device's name.
- The worker's stream asks for the kind: `GET /jobs/events?kinds=restart`. A
  restart is handed only to such a stream. The frame is `event: job`,
  `{id, kind: "restart", restart: {only, after, force, dry_run}, timeout}`.
  A command's frame is unchanged: `{id, cmd, timeout}`, no `kind`.
- The worker checks the fields again, runs `python -m
  agent_media_core.restart_services --json …` (the checkout's
  `.venv/bin/python`, else `MEDIA_PHONE_JOBS_PYTHON`), which is `run(…,
  caller="", detach_own=True)`, and answers `POST /jobs/result {id, rc,
  result, err}`. It refuses a restart with another field, or a job of a kind
  it does not know, with rc 2, and runs nothing. A restart of the worker's own
  service is detached, 10 s out, so the answer gets back first.

**Updates** (built 18 Oct 2026). The same channel carries an update: pull,
then restart.

- `POST /jobs/update {device, only?, after?, force?, dry_run?}`: the same
  gates, fields and statuses as `/jobs/restart` (503 "too old to take an
  update" for a worker whose stream did not say `?kinds=…,update`). Waits up
  to 180 s. 200: `{ok, rc, lines, pulled: {from, to}, moved, restart, device}`,
  `restart` the device's `run()` dict, or null when nothing moved (or the
  pull failed).
- Frame: `{id, kind: "update", update: {only, after, force, dry_run},
  timeout}`. The worker refuses a checkout with uncommitted changes (rc 1,
  nothing pulled), runs `git pull --ff-only` (90 s at most; a hang is named
  "no route to its git remote"), and only when HEAD moved runs the restart
  as above, detached, `after` 10 s when 0 or absent. A dry run fetches and
  says what a pull would bring (`would pull a -> b (n commit(s))`), and its
  restart is a dry run. Nothing else: no ref, no branch, no command.
- CLI: `media update --host NAME [--only …] [--after S] [--force]
  [--dry-run]` (`restart_services.update_device`). `media doctor --fix`
  deploys a host that is one of this server's jobs devices this way instead
  of over ssh, and also one ssh could not reach.

**Any host, several workers** (built 18 Oct 2026). Any host can run the
same worker (`packages/core/services/jobs-worker/jobs-worker.py --generic`,
installed by `media jobs pair` for systemd --user, runit or a Windows logon
task), each its own device:

- A **jobs-only device** is paired with `media-visual-canvas pair --device
  "pn jobs" --jobs` (the bit rides on the code, like `--enrol`, and the
  redeemed row carries `jobs: true`; `POST /pair` adds `jobs: true` to its answer
  for such a code only), or made
  one with `media-visual-canvas devices --jobs ID`. `MEDIA_JOBS_DEVICES`
  (ids) also counts. It takes `restart` and `update` jobs and never a
  command, and its token is refused by every other route (the gate leaves it
  out, so it falls through to ABS, which refuses it).
- `MEDIA_PHONE_JOBS_DEVICE` stays the phone: the one device that takes
  commands (`/jobs/run`), and restarts and updates too.
- Every job names its device. A stream takes only its own device's jobs,
  and `POST /jobs/result` for another device's job is 403. `device` in
  `/jobs/restart` and `/jobs/update` matches any of these devices by id or
  whole name first, then by the first word of its name.
- `GET /jobs/devices` (host token, else may_admin): `{devices: [{id, name,
  phone, may, connected, takes}]}`; `media jobs list` on the server.

`canvas` names the canvas wherever it runs: `canvas-local` on the phone,
`agent-media-visual-canvas` on red5, `sasonica-canvas` on an Android
install. So restarting the phone's canvas, on red5, is:

    media restart-services --host p8a --only canvas

### 6.22 Can David be spoken to now — device reports, gated read (built 2 Oct 2026)

The phone reports what only it can see; red5 answers "free or busy, and
why", and while busy, non-urgent speech to the phone waits. Code:
`agent_media_core/free.py`, the gate `intake/submit.py` `_busy_hold`, the
routes in `agent_media_server/app.py` `_free`. Pinned by
`packages/core/tests/test_free.py` and
`packages/server/tests/test_free_routes.py`. Proposal:
`docs/proposals/2026-10-01-speaks-when-youre-free.md` (step 1).

- `POST /device/state {call?, voice?, quiet?, meeting_until?,
  meeting_title?, manual_until?}` — **a paired device only** (401 for
  anything else, the host's token included). Booleans: a phone call, a voice
  session, `RingerState.quiet()`. `*_until` are epoch seconds; a meeting
  under way that is busy, timed and not declined; "busy for an hour".
  `meeting_title` ≤ 120 characters. The app reads meetings from the phone's
own calendar once Settings turns it on ("Hold replies during meetings",
`READ_CALENDAR`): a timed, busy, not-declined event under way, with
back-to-back events 10 min apart or less chained into one `meeting_until`.
A new meeting is put to Jev (`event_busy`) in shadow, logged only.
Unknown fields are dropped. Each report
  **replaces** that device's last one. Send on every change, and every 2 min
  while any field says busy. → the answer below.
- `GET /free` — the app's gate, or the host's own token. → `{free, why,
  since, until, age_s, held}`.

`why` lists the reasons, in this order: `call` (call or voice), `quiet`,
`meeting`, `manual`; any device's report can make David busy. `since`: when
this busy spell began. `until`: the latest end among `meeting` and
`manual`; null while a call or a quiet ringer is a reason (no end known).
`age_s`: the freshest report's age, null with none. `held`: speech held
since `since`.

**Fails open.** A report older than 5 min counts for nothing; no report
is free.

**What waits.** Speech to the ringer's targets (`MEDIA_RINGER_TARGET`,
default `phone,sasonica`) below HIGH priority. An alert is written down
unspoken (history `extras.silenced = "busy"`, `busy_why`); a reply is
rendered and archived with a Play (`extras.held`, `held_why = "busy"`), as
an unwatched reply is. HIGH and URGENT speak (an interrupt-level thread, a
prompt, `media say --urgent`), and a quiet-level thread is untouched.
`MEDIA_FREE_GATE=0` turns the gate off. Jev (`jev.py`) is asked whether a
held item should break through: logged only by default
(`MEDIA_JEV_MODE=shadow`); with `on`, a sure yes speaks it.

**The catch-up** (`agent_media_core/catchup.py`, built 2 Oct 2026). The
canvas server watches the answer every 15 s; on busy → free, after a
settle (30 s after a call, 60 s after quiet, none after a meeting or
manual), it says one clip if anything was held in that spell: held
replies not yet heard and alerts recorded unspoken, summarised by the
gateway (`MEDIA_CATCHUP_MODEL`, else `MEDIA_FOLLOWUP_MODEL`; about 120
words, the decisions first), or on failure a template with counts and
thread names. Spoken at HIGH, `extras.kind = "catchup"`, with
`catchup_items` (the history ids it covers), `catchup_how` (`summary` |
`template`), `busy_since`, `busy_why`. Busy again within the settle
continues the spell. `MEDIA_CATCHUP=0` turns it off. A digest rendered
held (the morning agenda) in the spell joins it: `kind: digest`.

- `POST /catchup` — the app's gate (`may_control_speech`) or the host's
  token. "Catch me up" now: every held reply and digest not yet heard and
  every alert recorded unspoken (any reason), since the last catch-up or
  the last 12 h; composed and said in the background, beginning "Here's
  what's waiting". → `{items}` (0: nothing is said). `media catchup` calls it.
- `GET /sessions/events?catchup=1` (§6.13) — a `catchup` frame for each
  catch-up made, and on connecting the latest if under 10 min old: `{id,
  at, text, how, why, replies, alerts, digests}` (`why` empty on demand).
  The app posts it once per `id` on a quiet "Catch-ups" channel, "While you
  were busy · 2 replies, 1 alert", the words below, a tap opening Home.
- `quiet_replies: false` in a report (Settings' "Hold replies while on
  silent" off): when `quiet` is the only reason, replies speak and only
  alerts wait. Absent means on. The answer carries `quiet_replies`.
- `quiet_kind` in a quiet report (built 17 Oct 2026): which quiet,
  `"dnd"`, `"silent"` or `"vibrate"`; Do Not Disturb wins (under it Android
  reports the ringer as silent too). Any other value, or a report that is not
  quiet, drops it. The answer (and the `free` frame) carries the freshest
  quiet device's `quiet_kind`, absent when none names one: the app's silent
  chip says "Do Not Disturb" rather than "on silent".
- `POST /free {speak: true}` — the app's gate or the host token. "Speak as
  it comes": speech goes through for the rest of this busy spell (the
  answer's `speaking: true`); the next spell holds again. 400 without
  `speak: true`.
- `GET /sessions/events?free=1` — a `free` frame on connecting and on every
  change: the answer without `age_s`, plus `held` (held this spell; 0 when
  free). The server polls the held count every 15 s. The app shows a quiet
  card while busy with `held > 0` and not `speaking`: "Holding 2 for
  later", Catch me up (`POST /catchup`), Speak as it comes, Later (down
  until more is held).

### 6.23 Notes in the phone's shade — host posts, the stream delivers (built 8 Oct 2026)

Roadmap item 15, #5 of "the phone dials out" (`notes.py`). What red5 used to
put up with `ssh p8a termux-notification`: a `converse` question ("Sam is
asking") and "N spoken replies didn't reach this phone".

- `POST /notes {id, title, text, priority?, ttl_s?, keep?}` — the host's own
  token only (401 for a device). `id` `[a-z0-9][a-z0-9._:-]{0,63}`; the same
  id again replaces the note. `priority` `default` | `high`; `ttl_s` default
  3600, at most a day; `keep`: the app leaves it in the shade when it leaves
  the set. Answers `{ok, note, listening, seen}`: `listening` is how many
  `notes` streams are connected now; `seen` whether any device has ever asked
  for notes here (a stamp file), so a caller keeps its old path for an app
  without notes and only for that. 400 for a bad field, 429 past 20 open.
- `POST /notes/clear {id}` — the same token; `{ok, cleared, listening, seen}`.
- `GET /sessions/events?notes=1` — a `notes` frame, `{"notes": [{id, title,
  text, priority, keep, at, until}]}`, on connecting if any are open and
  whenever the set changes. The app posts an id that is new or whose words
  changed (tag `note:<id>`; `high` on the needs-you channel, else alerts),
  and takes one down when it leaves the set unless it was `keep`. In memory:
  a restart forgets open notes, and the producers post again.

### 6.24 New projects, worktrees and presets — gated (15 Oct 2026)

A project is the folder a thread ran in (§18, `sessions.project_of`), so
there is no project to register: a new one is a folder made by the first
chat in it. Everything here rides on `POST /ask` (§6.3) — one call, so a
folder never appears without the chat that asked for it — and is done only
after the sign-in and agent checks pass. `new_project` and `worktree` each
imply `target: "new"`; together, or with `cwd` / `project`, → 400.

**New project** — `new_project: "<name>"`. The folder is `<root>/<name>`,
`root` (18 Oct 2026) one of the project roots below — as listed, or a path
that resolves to one; not listed → 400, before anything is made — and the
first (the default) when it is left out. A name is letters, digits,
`.` `_` `-`, starts with a letter or digit, ≤ 64, not ending `.git` → else
400; a folder already there → 409 with its `cwd` (pick it from the places).

- `repo`: `git clone` it there instead of `git init`. `https://…`,
  `ssh://…` or `user@host:path` only → else 400. A clone that fails (or
  takes over 5 min) → 502 with git's last line, and the folder is removed.
- `agents_md: true`: write `AGENTS.md` (a clone's own is kept) and
  `CLAUDE.md` as a symlink to it (an existing one is kept). The text is the
  named `preset`'s `agents_md`, else the host's template
  (`~/.config/agent-media/agents-template.md`), else a short built-in one;
  `{name}` becomes the project's name.
- `agents_md: "draft"` (15 Oct 2026): the chat writes it instead. Only
  `CLAUDE.md` → `AGENTS.md` is made, and the first message is the server's
  fixed instruction (read the repo, write a short AGENTS.md from what is
  there, improve one already there) followed by `text` — which may be empty.
  Agent-neutral, where `/init` is each harness's own or missing. Meant for a
  clone; a fresh `git init` has nothing to read.

Answers as `/ask` does, plus `"cwd"`: the folder.

**Project roots** (18 Oct 2026) — the folders a new project may go in, kept
in `<state_dir>/project-roots.json` (a list of paths, the first the
default). With no file it is one: `MEDIA_PROJECTS_DIR`, else `~/projects`,
which is the host's word and not judged. One added here must be an existing
directory under the home folder, judged after resolving symlinks (a link in
`~` that points out of it → 400), and not listed already (by its resolved
path → 409). The last one cannot be removed (409); an unknown one → 404.
Answered on their own route and on `GET /presets` as `project_roots`, so
the new-chat sheet has them without a second call. `label` is the path with
the home folder as `~`.

A root whose folder has gone (18 Oct 2026) stays listed with
`"missing": true` — checked at each request (exists and is a directory), so
it is `false` again once the folder is back. A new project in it — named as
`root`, or the default when the default is missing — is refused 409 `"that
folder is gone"` (with `"root"`) before anything is made: a new project never
creates a root, the built-in `~/projects` included (only Settings does, below). There is no fallback to
another root and `default` stays the stored first one; the app starts its
form on the first root that is not missing and sends it as `root`. A
missing root can still be removed (and made default, though nothing is made
in it until it returns).

A folder that is not there yet (18 Oct 2026) can be made — from Settings,
explicitly, never during a new project. `POST /projects/roots` for a path
that does not exist answers 404 with `"would_create"`: the folders `mkdir
-p` would make, top first, as labels (`["~/work", "~/work/clients"]`); sent
again with `"create": true` it makes them, then adds the root, and the
answer's `"made"` lists them (`[]` when nothing was made). `POST
/projects/roots/recreate {"path"}` remakes a listed root whose folder has
gone — it is itself the explicit act, so there is no would_create step —
and answers `"made"` the same way (`[]` if the folder is there; not listed
→ 404). Both are judged from the nearest existing ancestor, after resolving
it: it must be a directory under home (a file in the way, a broken link, or
a link that leads out of home → 400); no `..` among the folders to be made;
not home itself nor inside the server's state dir (400); at most 4 new
levels (400). A refused path never gets `would_create`. The folders get
mkdir's own mode (0777 less the umask), as `mkdir -p` would.

Edge cases (18 Oct 2026), all on `POST /projects/roots` (with or without
`create`) and `…/recreate`:

- **Unmounted drive.** Each stored root keeps its folder's `dev` and `ino`
  (the file's rows are `{"path", "dev", "ino"}`; a bare path, the format
  before, gets them on the first read that finds the folder). A root whose
  folder is now on another device is listed `"missing": true, "why":
  "unmounted"` (a gone one has `"why": "gone"`) and a new project in it is
  refused 409 `"that folder's drive is not mounted"` (`"why": "unmounted"`).
  Recreate refuses it 409, and refuses a gone root (409, `"why":
  "unmounted"`) whose nearest existing folder above is on another device
  than the root was: it would be made on the wrong disk. A folder that has
  moved to a new disk for good is removed and added again.
- **Inside a repo or another root** → 400: in a git work tree (a `.git` in
  it or a folder above it, short of home) unless that repo's git ignores it
  (`git check-ignore`: `~/spl/projects` in the `~/spl` repo, whose
  `.gitignore` has `/projects/`; 19 Oct 2026), inside a listed root, or
  holding one.
- **Hidden folders** → 400: any component under home starting with `.`
  (`~/.config`, `~/work/.x`), judged after resolving links.
- **Failure partway.** Made one level at a time; on a failure every level
  this request made is removed again (deepest first) and the answer is
  403 (no permission), 507 (disk full), 409 (read-only disk; or a level
  that changed while being made), else 400 — never a 500. `made` lists only
  the folders this request made.
- **Links swapped in.** Each level is made with mkdirat in its parent opened
  `O_NOFOLLOW` and checked with fstat (the existing part walked down from
  `/` the same way); a link or file put in the way → 409, nothing followed.
  Hosts without `dir_fd` (Windows) mkdir by path and re-check each level.
- **One folder, several names.** Compared after `~`, `.`/`..`, trailing `/`,
  links in the existing part, Unicode NFC, and case where the filesystem
  folds it (Windows; elsewhere tested by looking up a case-swapped name) →
  409 already listed.
- **Two edits at once.** Every change to the list holds a lock on
  `project-roots.json.lock` (flock; msvcrt on Windows); the file is still
  replaced atomically.
- **Portable names** → 400 on every host: `CON`, `PRN`, `AUX`, `NUL`,
  `COM1`–`COM9`, `LPT1`–`LPT9` (with or without an extension), and names
  ending in a dot or a space. Windows junctions count as links.

```
GET  /projects/roots → {"ok": true, "roots": [{"path": "/home/d/projects", "label": "~/projects", "default": true, "missing": false}, …]}
                       // missing ones add "why": "gone" | "unmounted"
POST /projects/roots {"path": "~/work"}           → the same, with it added last, plus "made": []
POST /projects/roots {"path": "~/new/x"}          → 404 {"ok": false, "error", "would_create": ["~/new", "~/new/x"]}
POST /projects/roots {"path": "~/new/x", "create": true} → the roots, plus "made": ["~/new", "~/new/x"]
POST /projects/roots/remove {"path"}              → the same, without it
POST /projects/roots/default {"path"}             → the same, with it first
POST /projects/roots/recreate {"path"}            → the roots, plus "made": [what it made]
```

The four POSTs are gated by `auth.may_admin`, which today admits every
device and owner exactly as the gate does; roles will narrow it to admin
devices (docs/proposals/2026-10-18-roles.md). The list is tidiness, not a
boundary: a device that may chat may already start an agent that runs
anywhere it likes.

**Worktree** — `worktree: {"from": "/home/…/projects/agent-media",
"branch": "fix-x"}`. `from` must be a `/targets` place (404) and a git repo
(422). `git -C <from> worktree add -b <branch> <from>/.worktrees/<branch>`
off its current HEAD; `.worktrees/` goes in the repo's `.git/info/exclude`.
A branch name `git check-ref-format --branch` refuses → 400; the branch or
directory already there → 409. Inside the project's folder, so the thread
is filed under that project. Answers as `/ask`, plus `"cwd"` and `"branch"`.

**Presets** — a saved way to start: which agent, login, model and plan,
the AGENTS.md a new project gets, and the words the box starts with. Kept
in `<state_dir>/presets.json`. "Profile" stays the harness login (§6.6).

```
GET  /presets → {"presets": [Preset…], "agents_md_default": "<the template a new project gets without a preset>",
                 "project_roots": [{"path", "label", "default", "missing"}, …],   // as GET /projects/roots
                 "personas": ["zara", …], "roles": ["reviewer", …], "private": ["zara", …],
                 "scope": {"zara": {"agents": ["pi"] | null, "projects": ["agent-media"] | null, "private": true}, …},
                 "trusted_models": ["venice/*", …], "private_mode": false}
GET  /personas/scope → {"scope", "trusted_models", "layers": {"default", "local", "user"}, "private_mode"}
POST /personas/scope {"id", "agents"?: [...] | null, "projects"?: [...] | null} | {"trusted_models": [...] | null}
                 → the same as GET, after setting the user layer (null clears a field)
POST /presets Preset → {"ok": true, "preset": Preset}       // added, or replaced by name
POST /presets/remove {"name"} → {"ok": true} | 404

Preset = {"name": "Web app",            // 1–40 characters, the key
          "agent": "claude" | "",       // "" = the host's default agent
          "profile": "", "model": "", "plan": false,
          "persona": "", "role": "",    // ids from personas/roles; "" = none
          "agents_md": "",              // "" = agents_md_default
          "prompt": ""}                 // put in the box, not sent
```

A preset naming an agent `/ask` does not know, or a persona or role not in
the lists → 400. The app applies a preset by filling the new-chat sheet
with it (persona and role included, sent on `/ask`); the server reads one
only for `agents_md`.

**Personas** (16 Oct 2026; step 1 of
docs/proposals/2026-10-15-personas-in-presets.md) — `personas` and `roles`
are what `persona-render --list` names in `MEDIA_PERSONAS_DIR` (default
`~/agent-personas`); both `[]` on a host without it, so the app shows no
persona chips. `private` (17 Oct 2026) is the personas with private material
in the repo — a `private/<id>.sops.md` or `private/<id>-….sops.md`, judged by
name alone; the app keeps them off the card until its lock is opened. They
are still in `personas`, so an older app shows them all.

**Scope** (17 Oct 2026, docs/proposals/2026-10-17-persona-scope.md) — per
id, `agents` (the only agents that take it; `null` every one) and
`projects` (folder names it belongs to: the app lists it first there;
`null` none), merged from three layers, later winning field by field: the
id's front matter (`persona-render --list --json`), the host's
`~/.config/agent-media/personas.toml` (`MEDIA_PERSONAS_CONFIG`), and the
user's, set by `POST /personas/scope`. `/ask` or a preset naming a persona or
role its scope keeps from the agent → 400 (a preset with no agent is not
judged). `trusted_models` are glob patterns on `/ask`'s model id (default
`venice/*`, `ollama/*`, `local/*`; no model is never trusted): a private
persona on a trusted model renders `--mode private` when `private_mode` is
true — only for Claude and pi, which take it as a system prompt. The session
is then marked private (`~/.local/state/agent/private.d/<session>`, the mark
agent-config's `agent-private` uses; `/ask` answers `"private": true`): it
speaks and is listed, but nothing of it is rewritten, drawn, suggested from,
recapped, titled by a model, indexed, fed to the shelf or a feed, or caught
up; and the reaper seals it once it has ended and been quiet 30 min, or after
`MEDIA_PRIVATE_IDLE` (2 h) idle, deleting its render (`MEDIA_PRIVATE_SEAL=0`
turns that off). `/ask`'s `persona` and `role` (either, or both) start a
fresh session as `persona-render <persona or role> --adapter <agent>
[--role <role>] --mode public` — always public, never `sops`. Unknown id →
400; a render that fails or takes over 20 s → 502 with its last line, before
a folder is made or a window opened. Delivered by the harness's own means:

| Agent | `persona_via` | How |
| --- | --- | --- |
| claude | `system_prompt` | `--append-system-prompt-file <file>` — in a pane, and headless at every sessiond spawn (start and each resume) while the file is there |
| pi | `system_prompt` | `--append-system-prompt <file>` (pi reads a path as its contents) |
| codex, opencode, hermes | `prepend` | the first message is `# Persona`, the render, `---`, then the words |

A headless Claude whose sessiond predates this (its `ping` lacks
`"system_prompt": true`) gets `prepend` instead. The file is 0600 under
`$XDG_RUNTIME_DIR/agent-media/personas/` (else the state dir), named
`<random>.md` with a second name `<session>.md` once the id is known, and
kept: a resume may be handed it again. The answer carries `persona`, `role`
and `persona_via`.

### 6.25 Contacts — gated (17 Oct 2026)

People, not rooms (`docs/proposals/2026-10-17-contacts.md`, steps 1 and
2, read-only). The server keeps a copy of David's contacts in
`<state_dir>/contacts.db` (`agent_media_server/contacts.py`). Pinned by
`packages/server/tests/test_contacts.py`. Both routes are `auth.gate`d, as
every thread route is.

**Where they come from.** Two sources, each kept as it came (`records`) and
merged into the book:

- **`android`, the default** (17 Oct 2026; David: "I'm pretty unfamiliar
  with Google cloud. can you run it with ADB?"): the phone's contacts
  provider, read as shell uid over `agent-phone-adb shell`
  (`contacts_android.py`; `MEDIA_CONTACTS_ADB` replaces the command). The
  provider already holds his Google Contacts (the `com.google` raw contacts
  Android syncs) merged with WhatsApp's into one person each, so that
  person is one contact here, identified by the provider's lookup key. Read
  whole every `MEDIA_CONTACTS_INTERVAL_S` (900 s); a read the same as the
  last writes nothing. adb answers only while the phone is on Wi-Fi: a read
  that fails keeps the last good copy and says why (`sources[].error`), and
  the next interval tries again. `content query` escapes nothing, so each
  free-text column is read in a query of its own as the row's last column,
  and a line is a new row only when it carries the next row number.
- **`google`, optional:** the People API (`contacts.readonly`), only once
  this host is signed in — `python -m agent_media_server.contacts login`
  (PKCE, a loopback redirect to `http://127.0.0.1:<port>`, and the
  redirect's address pasted back works too: on a phone that page never
  loads). The OAuth client is Google's Desktop-app JSON at
  `~/.config/agent-media/google-oauth-client.json` (or
  `MEDIA_GOOGLE_OAUTH_CLIENT`, a path or the JSON itself); the refresh
  token is kept 0600 at `~/.config/agent-media/google-contacts-token.json`,
  next to `secrets.env`. Synced with a sync token (an expired one, 410, is
  a full resync). Where a Google contact shares a number (else an address)
  with exactly one phone contact they are one person, and **Google wins**:
  each field is Google's when Google has one, the phone's otherwise.

`MEDIA_CONTACTS_SOURCES` (comma separated) picks the sources;
`MEDIA_CONTACTS=0` leaves the background sync off. `… contacts status`
says what is read and when it last was; `… contacts sync [--source
android|google]` syncs now.

**Whose book** (18 Oct 2026; proposal, "Where contacts live": one address
book per person, several per server). Every route here and in §6.25a, and
the `contact` on `/targets`' Matrix rows, answers from **the caller's
book** only; another account's contacts are not there to be found (a
contact id from another book is 404, as an unknown one is). Pinned by
`packages/server/tests/test_contacts_accounts.py`.

- **The owner's book** is the paths above, unchanged — no migration. It is
  the book of a code-paired device (`pair`, someone at the desk), of ABS's
  root, and of the owner's account: `MEDIA_OWNER_ACCOUNT`
  (`MEDIA_OIDC_ALLOW`'s form, `<iss>|<sub>` or `<iss>|email=<addr>`), or,
  unset, any account whose device may enrol others (an admin, who has the
  store from a shell anyway). The host's `MEDIA_*` settings, the phone over
  adb and the Matrix rooms are the owner's.
- **Another account's book** — a device an account enrolled (§9, its row's
  `iss` and `sub`) — is the same files under
  `<state_dir>/accounts/<key>/` and `~/.config/agent-media/accounts/<key>/`,
  `<key>` being `acct-` and 20 hex digits of the SHA-256 of `<iss>|<sub>`
  (an ABS login that is not root: `abs-…` of its username). Its settings
  are its `contacts.json` there (`{sources, self, timeline}`), its Google
  sign-in and CRM connection (§ "The CRM") its own files there; Google's
  OAuth client is shared. It never reads the phone, and no Matrix room is
  any of its contacts' (they are the owner's rooms).
- A request no one signed reads no book at all. The background sync
  visits every book in turn; `… contacts [--account WHO] <command>` acts on
  the owner's book unless WHO names another (its key, or an enrolled
  account's email or sub; unknown is an error, not a new book), and
  `… contacts accounts` lists the books and the accounts enrolled here.

#### `GET /contacts?q=[&limit=]` — gated (`auth.gate`)

Everyone, by name; or, with `q`, those it matches as it is typed. Gzipped
when accepted.

```json
{"ok": true,
 "contacts": [
   {"id": "3f0c…-uuid", "name": "Lionel Smith",
    "phones": ["+61412345678"], "emails": ["lionel@example.com"],
    "labels": ["Family", "My Contacts"]}],
 "total": 1, "synced_at": 1792200000.5,
 "sources": [{"kind": "android", "synced_at": 1792200000.5, "error": null}]}
```

- **`q`** matches a name (any of the contact's names; case and accents
  ignored) at its start or a word's start first, then anywhere in it; a
  phone's digits as stored (`+61412…`), as dialled at home (`0412…`) or as
  written on the phone; or part of an email. No `q`: everyone.
- **`contacts`**: at most `limit` (default 200, at most 5000), best match
  then name order. `phones` are E.164 (default region AU,
  `MEDIA_CONTACTS_REGION`); a number that cannot be placed (no area code,
  letters) is left out here and kept on the card. `total` is every match.
- **`sources`**: each source this host reads or has read: `synced_at` its
  last good read (epoch s, `null` before one), `error` why the last try
  failed (`"… adb: no devices/emulators found"` with the phone off Wi-Fi),
  else `null`. The app can say "as of <time>" from it. **`synced_at`** is
  the newest of them. A server with no source answers `"contacts": []`,
  `"total": 0`, `"sources": []`.

#### `GET /contacts/<id>` — gated (`auth.gate`)

One person's card. 404 `"no such contact"` for an id the store does not
hold (and for any path that is not a uuid, as an unknown route).

```json
{"ok": true,
 "contact": {
   "id": "3f0c…-uuid", "name": "Lionel Smith",
   "names": [{"display": "Lionel Smith", "given": "Lionel", "family": "Smith"}],
   "phones": [{"value": "+61412345678", "raw": "0412 345 678", "type": "Mobile"}],
   "emails": [{"value": "lionel@example.com", "type": "Home"}],
   "addresses": [{"formatted": "1 Main Rd\nDonvale VIC 3111", "type": "Home"}],
   "organizations": [{"company": "Acme", "title": "Boss"}],
   "notes": "",
   "sources": [{"kind": "android", "id": "3789r11-4F2B4D.3789r12-4F2B4D",
                "raw": [11, 12], "accounts": ["com.google", "com.whatsapp"], "version": 6}],
   "updated": 1792200000.5,
   "rooms": [{"session": "a1b2…-uuid", "room": "!abc:ryer.org", "title": "Lionel Smith",
              "network": {"id": "gmessages", "name": "Google Messages"},
              "at": 1792199000.0}]}}
```

- One entry per value: the phone's copies of a number (Google's and
  WhatsApp's raw contacts both hold it) are one phone. `phones[].value` is
  E.164 or `null` (then `raw` is all there is); `raw` is as written, `type`
  the label ("Mobile", "Work", a custom one). `notes` are the phone's notes
  (Google's biography for a Google contact), blank-line separated.
  `addresses[].formatted` may hold newlines.
- **`sources`**: where the card came from. `android`: `id` the provider's
  lookup key (the contact's id is a uuid made from it), `raw` its raw
  contact ids, `accounts` their account types, `version` the sum of their
  versions (it moves on any edit). `google`, when signed in: `{"kind":
  "google", "id": "people/c123", "etag"}`, first when it won.
- **`updated`**: when the card last changed here (epoch s).
- **`crm`**: the person in the Drupal CRM and the fields only it has
  (below, "The CRM"), or `null`.
- **`mail`**: `{"url", "label"}` for "Their emails", a mail search over
  every address on the card and every old chat address (`old_addresses`),
  `from:(a) OR to:(a)` each, URL-encoded into the book's template:
  `MEDIA_CONTACTS_MAIL_URL` (the owner's) or `contacts.json` `mail_url`,
  `{query}` where the search goes, default
  `https://mail.google.com/mail/u/0/#search/{query}`; `off` leaves it out.
  `label` is "Gmail" for Gmail, else the link's host. A search longer than
  2000 characters stops at the last address that fits — the card's own
  first, then the old ones last seen first — and says `left_out` (how many
  did not). The app opens it outside itself, as it does `tel:`; the Gmail
  app does not take a search (tried 20 Oct 2026: an ACTION_SEARCH is
  dropped, a `#search/` link opens the inbox), so it is the browser's.
  `mail` also carries `mailto` (`mailto:` the card's first email, for
  "Email them", which Android hands to the Gmail app) and `recent: true`
  when their newest mail can be listed (below). `null` with none of these.

  **Their newest mail**: `GET /contacts/<id>/mail[?limit=5]` (gated as the
  card; limit at most 20) → `{"ok": true, "available": true, "limit",
  "items": [{"id": "<notmuch message id>", "date": <epoch s>,
  "from": "Lionel Smith", "mine": false,
  "subject": "Lunch?", "gmail_url": "https://mail.google.com/mail/u/0/#search/rfc822msgid%3A…"}]}`,
  newest first, read live from this host's notmuch index (contacts_mail.py:
  `search --output=messages --sort=newest-first`, then `show --body=false`)
  over the card's addresses and its old chat addresses, raw and decoded:
  mail from them or to them. Left out as the timeline's email is: a list's
  or a machine's mail (List-Id, List-Unsubscribe, Precedence, Auto-Submitted,
  a no-reply sender), spam, deleted, the Chats label. `from` is the
  sender's name (else their address's local part), `mine` whether it is
  one of David's own addresses; `gmail_url` is that one message by its
  Message-ID in the book's mail template. Nothing is kept: notmuch's answer
  is read in memory and dropped, never logged or written. `{"ok": true,
  "available": false, "items": []}` when it cannot be listed (another
  account's book — it is this host's mail —, no notmuch with a database,
  `MEDIA_TIMELINE_MAIL=off|imap` or `MEDIA_CONTACTS_MAIL_RECENT=0`); 503
  when notmuch does not answer; 404 no such contact (David, 20 Oct 2026:
  "Both" — the newest on the card, the browser search for the rest).
- **`old_addresses`**: `[{"address", "real"}]`, the old Jabber (XMPP)
  transport addresses to MSN, Yahoo, AIM, ICQ, Facebook chat, Google Talk
  bots and the like (`someone%hotmail.com@msn.example.org`), folded off
  `emails`: `real` is the address the local part encodes (`%` for `@`), or
  `null`. A `real` the card lacks is added to `emails` as
  `{"value", "type": "derived", "derived": true, "from"}`, and matching
  reads it (a gateway address matches no one by itself). A transport is a
  host matching `MEDIA_CONTACTS_GATEWAYS` / `contacts.json` `gateways`
  (comma-separated fnmatch patterns, each `+pattern` added to the default
  list, else replacing it; the default covers `*.gajim.org`, `*.jaim.at`,
  `msn*.jabber.*`, `yahoo*.jabber.*`, `j2j.*`, `gtalk2voip.com`,
  `chikkatalk.com`, `*.talk.google.com` and others), or a host starting
  with a transport's name (msn, yahoo, aim, icq, fb, …) whose address
  encodes one, so `someone@yahoo.com.au` never is. Every one ever seen is
  kept per contact in the store's `aliases`, through schema bumps, so they
  stay on the card and in the mail search after they are deleted from
  Google; this list is current ones first, then those only `aliases`
  remembers, last seen first. Cards made before this are made again from
  the kept records at the next sync (`CARDS_VERSION`), nothing read again.
- **`rooms`**: the Matrix rooms (§6.1 "Matrix rows") that are this
  person's, newest message first: `session` is the room's thread id, so
  every session route takes it; `at` its last message, or `null`.
- **Edits are not here yet.** When they come (the proposal's step 4) they
  will go through the phone's provider — a content update on the person's
  `com.google` raw contact, which Android then syncs to Google — with the
  raw contact's version as the guard against a newer change.

**Which room is whose.** Each member of a room other than the owner, the
agent (`MATRIX_SAM_ID`) and the bridge bots is matched by phone (E.164),
else email, else — a bridge's name for its puppet being the phone's own
contact name — a name only one contact has (since 19 Oct 2026 only on a
trusted network, `gmessages`, and first of all a ghost a contact holds as a
handle: "The bridged networks" below). A room is a contact's when its
members match exactly one; a group of several people is no one's, and so is
a number two contacts share. The puppets: mautrix-gmessages' are
`@gmessages_<login>.<participant>:<server>` (no number in the id) named
`{{or .FullName .PhoneNumber}}`; Signal's `@signal_<uuid>` (older ones the
escaped number, `@signal_=2b61…`); WhatsApp's `@whatsapp_61412…`. A number
comes from a Signal or WhatsApp id that is one, and from a displayname that
is one; Messenger's (`@facebook_<id>`), Instagram's and Teams' ids are
account ids, never a number.
`MEDIA_CONTACTS_SELF` (David's own numbers and addresses, comma separated)
are never matched: texts sent from the phone itself arrive as his number's
puppet. A manual **join** (this room is this person's) or **split** (it is
not) is kept in the store and wins over the matcher; no route writes them
yet.

Contacts are personal data: they stay on this host and go only to a
signed-in device that asks; nothing here speaks them.

**The CRM, a third source** (18 Oct 2026, proposal step 3). `drupal`,
optional and only once its connection is set: the Drupal CRM's people,
over JSON:API at the CRM's own address — its public HTTPS one, not a
tailnet listener (David: "we're working towards not relying on tailnet for
the software") — signed in as a machine with Simple OAuth's
`client_credentials` grant (`contacts_drupal.py`). The connection is
`~/.config/agent-media/drupal-crm.json` (or `MEDIA_DRUPAL_CRM`, a path or
the JSON), 0600, `{url, client_id, client_secret, scope}`; the access token
is cached beside it, 0600. Read whole each interval, the last good copy
kept when it fails. A CRM person joins the contact the seed linked them to,
else the one sharing their number, else their address, and **only fills
what the phone and Google left empty**. Their other fields (any field the
site has, `field_` dropped from the name) are the card's **`crm`**:
`{"id": <person uuid>, "url": <their CRM page>, "fields": {…}}`, or `null`
when the CRM does not know them. `… contacts seed` adds the people the CRM
lacks (a name and a number or address each), **a dry run unless `--go`**
(`--limit 1` to try one); each one made is linked to its contact before
their numbers are added, so a second run adds nobody. Addresses are not
seeded: the card holds them as text, the CRM's address field wants parts.

**Google, per book** (18 Oct 2026; David: "let's do it but keep it in
testing mode for now"). Each account signs its own book in to Google from
the app; the token lands in the caller's book (§ "Whose book") and nowhere
else. The OAuth client is the host's one Desktop client (above), so the app
runs the loopback listener: it listens on `127.0.0.1:<port>` on the phone,
opens the consent page in a Custom Tab (Google refuses sign-in inside a
WebView), and hands back what the redirect carried. The PKCE verifier stays
on the server. A sign-in asks for `openid email contacts.readonly` (`email`
is not a sensitive scope), so the book can say whose Google it reads.
Pinned by `packages/server/tests/test_contacts_google_signin.py`.

**Testing mode.** The Google project stays in testing: only the test users
added on its OAuth consent screen (at most 100) can sign in, and Google
ends a test user's refresh token after **7 days**. A refresh Google refuses
(`invalid_grant`) marks the book's Google **expired**: the token is kept
with `expired_at`, the sync stops trying it, the last good copy of the
contacts stays, and `GET /contacts/google` says `"expired": true` so the
app offers "Sign in again". A new sign-in clears it.

#### `GET /contacts/google` — gated (`auth.gate`)

```json
{"ok": true,
 "google": {"available": true, "signed_in": true, "expired": false,
            "email": "kat@gmail.com", "signed_in_at": 1792300000.1,
            "last_sync": 1792300004.2, "last_error": null, "contacts": 412}}
```

`available` is whether this server has an OAuth client at all (false: the
app hides the row's Sign in). `email` is `null` for a sign-in made before
the `email` scope was asked. `contacts` counts Google's records in the book.

`exports` (always there, whatever Google's state) is where each network's
export is requested, for Settings → Contacts → Import a file:
`[{"network": "facebook", "label": "Facebook friends", "url":
"https://accountscenter.facebook.com/info_and_permissions/dyi", "steps"},
{"network": "instagram", "label": "Instagram followers and following", …}]`.
`steps` names the same choices the export reminder does (below, "The export
cycle"; `contacts_exports.CHOICES`): Some of your information → Connections
→ Friends and followers (Followers and following), All time, Format JSON.
Then the past conversations (below, "Past conversations";
`contacts_exports.HISTORY`): `messenger` "Messenger conversations" and
`instagram_messages` "Instagram conversations" (the same page: Messages
only, All time, JSON, Media quality Low) and `chats` "Old Google Talk
chats" (`https://takeout.google.com/`: Deselect all → Mail → only the
Chats label → .zip), each `steps` ending with where a large one goes (the
import folder or `contacts import`).
The app shows each as a button opened outside itself, its steps under it
(David, 19 Oct 2026).

#### `POST /contacts/google/login {"port"}` — gated (`auth.gate`)

→ `{"ok": true, "url", "state", "redirect_uri": "http://127.0.0.1:<port>",
"expires_in": 900}`. Open `url`; Google redirects to `redirect_uri` with
`?state=…&code=…` (or `error=…`). The state is good for 15 minutes, once,
and only for the account that started it. 400 for a missing or privileged
port (1024–65535); 503 `"no_client": true` when the server has no OAuth
client.

#### `POST /contacts/google/finish {"state", "code" | "url"}` — gated

`url` is the whole redirect address (its `state` must match), `code` the
bare code. → `{"ok": true, "google": <as GET>}` once the token is kept; a
full sync of the book's Google then runs in the background (`last_sync`
moves when it is done). 400 for an unknown, spent or expired state, or an
address from another sign-in or carrying Google's `error`; **403 for a
state another account started** (it stays that account's to finish); 502
when Google refuses the code or cannot be reached.

#### `POST /contacts/google/signout` — gated

The book forgets Google: the refresh token is revoked at Google (best
effort) and deleted, and Google's records leave the book (the phone's, for
the owner, stay). → `{"ok": true, "google": <as GET>}`. A POST rather than
a DELETE, as `/me/signout` is.

**The bridged networks** (19 Oct 2026; David: "draw in contacts from the
other providers we have bridges for"). `bridges`, the owner's book only, on
whenever this host reads bridges as the owner (`MATRIX_OWNER_BRIDGES`):
the other party of **each one-to-one bridged room** this process holds
(Messenger `facebookgo`, Instagram `instagramgo`, Teams `msteams`, SMS
`gmessages`, …), and — since 19 Oct 2026 (David: "a full list of my
bridged contacts") — **every ghost in its group rooms** (a member whose id
is a bridge's puppet, `@<network>_…`; a Matrix account of its own is not).
Someone met only in groups carries the label **"Messenger groups"** or
**"Instagram groups"**; everyone on Teams carries **"Teams"**; someone in a
one-to-one chat carries none of its own. A group room is still no one's
(its members are several people). Neither the bots nor **David's own
ghosts** come in — a ghost in every room of its network (at least 3 of them;
Instagram puppets him into each chat), one named as the owner is in that
room, or his own number's (`MEDIA_CONTACTS_SELF`). One record per ghost:
its name, the number or address its id or displayname carries, and its
**handle**. It joins a contact by, in order: a kept **Join** (below); the
one contact sharing its number; its address (Teams' displayname is often
one); and only on a network whose names are the phone's own
(`MEDIA_CONTACTS_TRUSTED_NAMES`, default `gmessages`) the one contact with
its name (never a person a bridge alone knows) — else it is **a person of
its own**, its id a uuid of `bridges:<ghost id>`. A join only fills what
the contact left empty. The canvas reads the rooms, so a process with none
loaded (the CLI) keeps the last copy (`… contacts sync --source bridges`
says so). Pinned by `packages/server/tests/test_contacts_bridges.py`.

On the card and its rooms:

- **`handles`**: `[{"network": "facebookgo", "network_name": "Facebook
  Messenger", "id": "@facebook_1000…:ryer.org"}]`, each ghost that is this
  person. A room whose member is a contact's handle is that contact's
  first of all. A bridge's source is `{"kind": "bridges", "id": <ghost id>,
  "network", "network_name"}`.
- **`suggestions`**: `[{"id", "name", "why": "same name" | "nickname"}]` —
  other contacts with this one's name, or a nickname of it with the same
  surname ("Dave Jones" / "David Jones", "Liz Wong" / "Elizabeth Wong";
  `contacts_review.NICKNAMES`, a small table of common English short forms,
  a one-word name never), where one of the two is known only from a bridge
  or an export, the two do not both have numbers and share none (two people
  of one name with phones of their own are two people), and David has not
  said they are not the same. A name off a trusted network joins nobody by
  itself, and a nickname never does; this is how they are offered.
- **`labels`**: below.

#### `GET /contacts/suggestions` — gated (`auth.gate`)

Every open "Same person?" of the caller's book in one place (David, 19 Oct
2026: a review screen rather than card by card; `contacts_review.py`) →
`{"ok": true, "count": n, "suggestions": [{"a": <side>, "b": <side>, "why":
"same name" | "nickname"}]}`, each pair once, the side known only from a
bridge or an export first; a side is `{"id", "name", "sources": ["Phone",
"Google", "Messenger", "Facebook export", …], "labels", "last": {day,
channel} | null}` (`last` as on a `GET /contacts` row). Same names come
before nicknames, then whoever was talked to last. Answered with the routes
below. Pinned by `packages/server/tests/test_contacts_review.py`.

#### `POST /contacts/<id>/same {"other"}` — gated (`auth.gate`)

"Same person?" → Join. The one of the two known only from a bridge joins
the other (kept in the store's `merges`, so it holds through renames and
every later read; two bridged people can be one), → `{"ok": true, "id":
<the contact left>}` — navigate there, the other id is gone. 400 when
`other` is not a contact id or neither is a bridge's alone; 404 when either
is not in the book.

#### `POST /contacts/<id>/different {"other"}` — gated (`auth.gate`)

"Not the same": the suggestion goes from both cards, and a trusted name
never joins them. → `{"ok": true, "id": <id>}`; 400 and 404 as above.

**Labels** (19 Oct 2026; David: "Can we bring Google groups too?"). Each
person's **`labels`** (a list of names, also on each `GET /contacts` row):
the phone's contact groups — Google's labels, synced by Android
(`com.google` groups only, read like the rest; Google's own groups by name:
"My Contacts", Friends, Family, Coworkers; starred left out) — and, for a
book signed in to Google, its People API `memberships` by `contactGroups`
name. Both are one list. These are also the work/personal tags the
proposal waited on. `GET /contacts?label=<name>` (case and accents
ignored) lists only the people with it. Several labels are `label`
repeated (`?label=Family&label=Friends`, not a comma list: a label may hold
a comma), with `labels_mode=any` (the default: anyone with any of them) or
`labels_mode=all` (only people with every one; David, 19 Oct 2026: "select
multiple Google contact labels at once", with a switch between the two).
Pinned by
`packages/server/tests/test_contacts_labels.py`.

**Show and Sort** (19 Oct 2026; David: "a recent setting … a similar kind
of show and sort filter like the threads list"). Each `GET /contacts` row
also carries **`last`** (`{day, channel}`, the newest day in the book's
timeline ledger, §6.25a, live rollups and imported days alike; `null` when
never) and **`count_90d`** (messages in and out over the last 90 days).
Notes are not talk: they count for neither. Parameters, each combining
with `q` and the labels:

- `sort=name` (the default), `recent` (last talked to first) or `most`
  (`count_90d` first); people never talked to come after, by name; with a
  `q`, how well it matches still comes first. Anything else is `name`.
- `since=<days>`: only people talked to within that many days.
- `service=<channel>`: only people on that channel (`sms`, `signal`,
  `messenger`, `instagram`, `teams`, `whatsapp`, `call`, `email`, `chat`):
  a day of theirs in the ledger on it, or a bridged handle on its network.

The answer gains `sort` (the order used) and `talked` (whether the book
has any activity at all, so the app can default to `recent` once there is
some). The activity is one grouped query over the ledger, kept until the
ledger changes or the day turns (`timeline.activity`). Pinned by
`test_contacts_labels.py`.

**Exports** (19 Oct 2026; David: "a full list of my bridged contacts"). A
bridge knows only who is in a chat, so whole lists come from each
network's own data download, the `import` source, per book: a **Facebook**
"Download your information" export's friends (`your_friends.json` under
`connections/friends/`, or the older `friends_and_followers/friends.json`
and `friends/friends.json`; `friends_v2`, `friends` and the newest
`label_values` rows; Facebook's escaped UTF-8 undone) and an **Instagram**
one's followers and following (`followers_1.json`…, `following.json` under
`connections/followers_and_following/`). Removed friends, requests and close
friends are not read; an HTML export is refused with "download it as
JSON". Labels **"Facebook friends"**, **"Instagram followers"**,
**"Instagram following"** (someone both is one person with both). An
export person joins as a bridge's does (a kept Join, a number, an address;
a same name is "Same person?", and Join / Not the same take an export's
person as a bridge's), and an Instagram username joins the bridged
Instagram ghost it names (its displayname, `{{or .DisplayName
.Username}}`). Each person's id is stable (Instagram's username; a Facebook
friend's name and the day they became friends), so importing a newer
export replaces that network's last import (an unfriended person goes) and
David's Joins hold. The source is `{"kind": "import", "id", "export":
"facebook" | "instagram", "lists": ["followers", …]}`; an Instagram
person's handle is `{"network": "instagram", "id": "instagram:<username>",
"username", "url"}`. Pinned by
`packages/server/tests/test_contacts_import.py`.

**Uploaded contacts** (19 Oct 2026; David: help Google, Facebook,
Instagram and Teams people come together). The address books David once let
Facebook (and Messenger) or Instagram sync, from the same downloads:
Facebook's `your_address_books.json` (`address_book_v2.address_book` rows
of `{name, details: [{contact_point}]}`, the older `address_book`, the
newest `label_values` rows) and Instagram's `synced_contacts.json`
(`contacts_contact_info` rows of `string_map_data` "First name", "Surname" /
"Last name", "Contact information"). They are his own address book again,
so **they make nobody**: each entry with a name and a number or an address
is kept as an `upload` record (`POST /contacts/import` answers
`facebook_uploads` / `instagram_uploads` counts), a newer upload replacing
that network's last. As evidence: a bridged or exported person of that
network (Messenger's are Facebook's) who joined nobody by number, address
or username, whose name is exactly one uploaded entry's, joins the one
contact (not a bridge's or an export's alone) holding that entry's number
or address; its source on the card says why (`"why": "Facebook's uploaded
contacts"`). Two uploaded entries of one name speak for nobody, and a kept
"Not the same" holds. `exports` (`GET /contacts/google`) lists both, with
the choices (Facebook: Personal information, which holds Your address
books; Instagram: Connections → Contacts). Pinned by
`packages/server/tests/test_contacts_uploads.py`.

**Past conversations** (19 Oct 2026; contacts_history.py; David: backfill
the timeline from the exports). The same downloads hold the years before
this server first counted a room. Read into §6.25a's timeline as its own
entries — per contact, per channel, per day, counts in and out, **never a
word of what was said** — marked `via: "import"`:

- **Messenger**: Facebook's export, Messages, JSON:
  `…/messages/{inbox,archived_threads,e2ee_cutover}/<thread>/message_N.json`
  (`participants [{name}]`, `messages [{sender_name, timestamp_ms}]`; a
  thread's files summed; message requests, media and everything else left
  unread), channel `messenger`.
- **Instagram**: its export, Messages: `your_instagram_activity/messages/
  inbox/<thread>/message_N.json`, the same shape, channel `instagram` (a
  path or a file name naming Instagram says which).
- **Old chats**: Google Takeout, Mail, only the Chats label: an `.mbox` (or
  the Takeout zip holding it) of the Google Talk and gateway chats Gmail
  kept. Only From, To, Cc, Date and `X-Gmail-Labels` are read, the file
  streamed a line at a time; a mail whose labels lack `Chat` is left out
  (`chats_without_the_label`). Channel `chat`.

**One-to-one only**: a group's messages belong to no one person, as a group
room does (`groups_left_out`). **Who David is**: the name in at least 60%
of a network's threads (two threads or more), the address in 60% of the
chats, or `MEDIA_CONTACTS_SELF_NAME` (comma separated; another account's
`contacts.json` `self_name`) and `MEDIA_CONTACTS_SELF`'s addresses; a
thread where he is not found is left out (`unknown_self`). **Whose**: a
thread's other person is the one contact with that name, one known on that
network first (a bridged ghost of it, a friends-list or follower person),
else an import person of their own labelled **"Messenger history"** /
**"Instagram history"** (source `{"kind": "import", "id":
"<network>-history:<hash>", "export": "<network>-history", "lists":
["messages"]}`; they join and are joined as an export's person is). A
chat's address is matched by the card's addresses and the old chat
addresses (`aliases`, §6.25 "Old chat addresses"), raw or decoded (an XMPP
resource after `/` dropped); an unmatched address adds no one
(`unmatched`). **Idempotent**: counts are summed per day across a
person's threads, then merged with the ledger by the larger, so the same
export again changes nothing and a day the rollup also counted is never
counted twice. **Size**: a message export with its media is large;
`… contacts import <file>` and `MEDIA_CONTACTS_IMPORT_DIR` read a zip's
message files and an mbox's headers as they go, any size; the route and
the phone's Downloads (read whole over the shell) stay at 64 MiB. Pinned by
`packages/server/tests/test_contacts_history.py`.

**The export cycle** (19 Oct 2026; contacts_exports.py; David: "how might we
automate the exports?"). Meta's website is never driven: requesting an
export stays David's tap. Around it, in the sync loop, for the **owner's
book only** (an alert reaches the owner's phone, so another account's book
gets none of this):

- **Files.** Export zips in the phone's Download folder
  (`facebook-*.zip`, `instagram-*.zip`, `meta-*.zip`, `takeout-*.zip`,
  listed and read over `MEDIA_CONTACTS_ADB`, base64 over the shell, never
  deleted there; at most 64 MiB each, a larger one said as "too large to
  read over the phone") and, with `MEDIA_CONTACTS_IMPORT_DIR`, those zips,
  `.mbox` files or the list JSONs in that folder (read as they go, any
  size), are imported once each (by path, size and time; a newer export
  is a new file). Each import is said as the `contacts.import` digest at
  warn (a phone notification: "Imported Facebook friends 412", counts,
  never names). A file that is not an export is tried once; one that could
  not be read is tried again; the phone off Wi-Fi is skipped.
  `MEDIA_CONTACTS_EXPORT_WATCH=0` turns it off.
- **The reminder.** Once a network has been imported, its export is due
  `MEDIA_CONTACTS_EXPORT_REMIND_DAYS` (90; 0 off) after the last import:
  the `contacts.export.<facebook|instagram>` alert at warn, `fix` the
  Download your information page, `detail` the exact choices (Some of your
  information → Connections → Friends and followers / Followers and
  following; All time; JSON). The next import of that network clears it.
- **The "ready" mail** (opt-in: `MEDIA_CONTACTS_EXPORT_MAIL=1` and a way
  to the mail: this host's notmuch index, else the timeline's IMAP
  connection, `timeline-imap.json`; §6.25a `email`). Mail from
  facebookmail.com or mail.instagram.com (the address's own domain) whose
  subject says the information is ready raises
  `contacts.export.ready.<network>` at warn, `fix` the download link from
  the mail when it is Meta's own (facebook.com, instagram.com,
  accountscenter / accountscentre, meta.com), else the Accounts Center
  page. Each mail once; the next import of that network clears it.
- **Google Drive** (David, 19 Oct 2026: a daily "Export to external
  service → Google Drive" of the previous week). `… contacts drive-import`,
  run daily by `agent-media-contacts-drive-import.timer` (05:00, up to 30
  min late, `Persistent=true`; `deploy/systemd`, installed as links), lists
  the `facebook-*`, `instagram-*` and `meta-*` zips under
  `MEDIA_CONTACTS_DRIVE_REMOTE` (an rclone remote, `gdrive:`) and
  `MEDIA_CONTACTS_DRIVE_DIR` (a folder in it; the whole drive when unset)
  with `rclone lsjson -R`, newest first, and takes each not seen before
  (by Drive id, size and time) one at a time: `rclone copyto` into
  `drive-import/` (0700) in the book's state dir, imported as it goes
  (`import_path`, any size, media never read, said as the
  `contacts.import` digest "From Google Drive"), and only then
  `rclone deletefile` on Drive and the local copy removed. A file that is
  not an export is left on Drive and said once; a copy or import that
  fails is tried again next run; one bigger than the free disk less 2 GB
  is skipped and left. `MEDIA_RCLONE` names the binary. The remote needs
  write scope (`drive`) to delete (red5's has it, through our own Google
  client); a read-only one imports and says each delete failed. Owner's
  book only. Point it at Meta's folder once known:
  `MEDIA_CONTACTS_DRIVE_DIR=<folder>` in the unit
  (`systemctl --user edit agent-media-contacts-drive-import.service`), or
  try it first with `… contacts drive-import --root 'gdrive:<folder>'`.
  **Meta's folders.** "Export to external service → Google Drive" writes no
  zip: it writes a `meta-<date>/` folder at the remote root (or under
  `MEDIA_CONTACTS_DRIVE_DIR`), `facebook-<user>-<date>-<id>/…/*.json` beside
  the media, filled over hours with the messages last. Each `meta-*` folder
  is listed (`rclone lsjson -R`) and kept as a hash of its file list. It is
  read once ready: no file newer than 2 hours, or Meta's "ready" mail raised
  (`contacts.export.ready.*`) since its newest file. Reading copies its JSON
  only (`rclone copy --include '*.json'`, never the media) into
  `drive-import/`, zipped there and imported as an unpacked export, then
  removed. Unchanged on a later run, it is not read again; grown (the
  messages landing late), it is read again, which is safe since lists
  replace and history merges by the larger count. It is `rclone purge`d from
  Drive only once settled (nothing new for 2 hours), on that run or a later
  one. A settled folder with no JSON (an HTML export) is left and said once.

State: `exports.json` in the book's state dir (last import per network,
the files and mails seen, the Drive files taken under `drive`, what is
raised). Pinned by `packages/server/tests/test_contacts_exports.py` and
`test_contacts_drive_import.py`.

#### `POST /contacts/import[?name=<file name>]` — gated (`auth.gate`)

The body is the export: a `.zip` of it (the whole download, or the
friends / followers / messages part, or a Takeout zip), an `.mbox`, or one
of those JSON files (a list, or one `message_N.json`), raw
(`application/zip`, `application/json`, `application/mbox`) or as a
`multipart/form-data` file. At most 64 MiB (413 before a byte is read); a
larger one goes through the CLI or the import folder. Into the caller's
own book. →

```json
{"ok": true, "imported": {"facebook_friends": 412, "messenger_conversations": 96},
 "people": 412, "joined": 37, "contacts": 3290,
 "history": {"messenger_conversations": 96, "instagram_conversations": 0,
             "old_chats": 0, "days": 5120, "new_people": 14, "groups_left_out": 31,
             "unmatched": 0, "unknown_self": 0, "chats_without_the_label": 0}}
```

`imported` counts each list read (`instagram_followers`,
`instagram_following`) and each kind of past conversation matched to
someone (`messenger_conversations`, `instagram_conversations`, `old_chats`:
people with old chats); `people` is how many contacts hold a list's person
now, `joined` how many of them are someone already here. `history` is
there when the export held conversations ("Past conversations" above):
`days` the timeline entries added or raised. 400
`{"code": "not_an_export"}` with why (HTML, no list found, not a zip that
opens). Names are never logged. CLI: `media contacts [--account WHO]
import <file>`.

#### `GET /contacts/labels` — gated (`auth.gate`)

```json
{"ok": true, "labels": [{"name": "Family", "count": 12},
                        {"name": "My Contacts", "count": 2410}]}
```

Every label on someone in the caller's book, by name, with how many have it.

#### Pictures — `GET /contacts/<id>/photo` — gated (19 Oct 2026)

Every `GET /contacts` row and every card carries `photo`: `{"url":
"/contacts/<id>/photo", "etag": "<16 hex>"}`, or `null` when there is none.
The route answers the picture's bytes from the caller's book only (a JPEG at
most 256 px a side when the server has Pillow), with `ETag` (the etag above,
quoted), `Last-Modified` and `Cache-Control: private, max-age=86400`; a
matching `If-None-Match` is 304; none to have, or a person not in the
caller's book, is 404. An `<img>` that cannot set a header may put the
token in `?access_token=` (as `/bridges/<id>/icon`).

Where a picture comes from (contacts_photos.py), the first that has one:

1. **Google** — the People API's `photos` (`personFields` asks for them; a
   default letter is no picture), for a book signed in to Google. The
   phone's own contacts are Google's, so this is their picture too: the
   phone cannot give it — its thumbnails are database blobs adb's `content`
   prints only as `BLOB`, and `content read` has no file for them.
2. **A bridge's ghost** — its own `avatar_url` on the homeserver, fetched as
   a thumbnail through authenticated media (the owner's book only). This is
   where Messenger, Instagram, WhatsApp and Teams pictures come from: never
   from a network's own pages.
3. **A ghost David picked** with "Find on" (below).

The server looks after each book's sync (every 15 min): a Google URL that
changed, a ghost not looked at for 7 days, at most 60 fetches a pass; a
failure waits longer each time (15 min doubling, at most a week). Kept as
`<state>/photos/<sha256>` (0600, the folder 0700); nothing else of the
source is kept. `MEDIA_CONTACTS_PHOTOS=0` turns it off.

**The larger one** (20 Oct 2026, for the card's tap-to-enlarge):
`GET /contacts/<id>/photo?size=large` answers the same picture at up to
1024 px a side — Google's URL with a bigger size option, the ghost's mxc as
a 1024 px thumbnail (else whole) — fetched the first time it is asked for,
never up front, and kept as `<state>/photos/large/<the thumbnail's hash>`
(0600), so a new picture makes a new one and the old goes with its
thumbnail. Its ETag starts `L`; If-None-Match, `Last-Modified`, the gate and
`?access_token=` are as for the small one. A source with nothing bigger, or
a fetch that fails, answers the thumbnail (its own etag), and a failed one
is not tried again for a day. No `photo` field changes: the app adds
`size=large` to the `url` it has.

#### Editing from the card — `POST /contacts/<id>/edit` — gated, owner only (20 Oct 2026)

A card says `"edit": true` when it can be edited here — the owner's book,
write-back on (`MEDIA_CONTACTS_WRITEBACK`), and a Google entry on the phone
behind it — and carries `"google_url"`, the person in Google Contacts
(`https://contacts.google.com/person/c…`, from a Google source's resource
name), or null. Edits go through the phone, as the read does: `content`
updates, inserts and deletes on the person's `com.google` raw contact,
which Android syncs to Google (contacts_edit.py). (A POST, not a PATCH:
the server routes GET and POST.)

```json
{"base": {"name": "Peter Ryan", "notes": "old note"},
 "set":  {"name": "Pete Ryan",  "notes": "met at the springs",
          "phones": ["0412 345 678", "0400 111 222"],
          "emails": ["pete@example.test"], "labels": ["Family", "Work"]}}
```

- `set` — the fields to change, any of `name`, `phones`, `emails`,
  `notes`, `labels`; `phones`, `emails` and `labels` are the whole list
  wanted (a value gone is deleted, a new one added — a mobile number, an
  "other" address — the rest left as they are). A label joins or leaves
  the Google group of that name; one Google has no group for is not made
  and comes back in `skipped`.
- `base` — what the app showed for those fields. A field whose value now
  differs was changed elsewhere since (in Google Contacts, by a sync):
  **409** `{"error", "conflict": {field: {"yours", "theirs"}}}`, nothing
  written. (The phone write has no People API etag; the card is the guard.)
- **200** `{"ok": true, "done": [fields], "skipped": [labels]}`; the phone
  is read again straight after, so the card shows Google's copy within
  moments. Nothing to change: `done: []`. **400** a field that cannot be
  edited or a bad shape; **403** not editable here; **404** no such
  contact; **503** the phone cannot be reached; **502** it refused.

#### Find on a network — `POST /contacts/<id>/find` — gated, owner only (19 Oct 2026)

A card's `find` lists the networks it may be searched on: `["messenger"]`
when the person has no handle there and "None of these" was not said for
them (always `[]` outside the owner's book). It is David's own act: one
search per tap, never batched, scheduled or in the background, and at most
one search every 10 s across the server.

- `{"network": "messenger"}` — ONE search for the person's name through the
  network's bridge (bridgev2 provisioning `POST /v3/search_users`; the
  Messenger bridge is appservice `facebook`,
  `MEDIA_CONTACTS_FIND_MESSENGER_BRIDGE` names another):

  ```json
  {"ok": true, "network": "messenger", "query": "Lionel Smith",
   "candidates": [{"id": "1001", "name": "Lionel Smith",
                   "handle": "@facebook_1001:ryer.org",
                   "avatar": "data:image/jpeg;base64,…"}],
   "pick": "1001"}
  ```

  At most 5 candidates; `avatar` a small picture from the homeserver or
  `null`; `pick` names a candidate only when exactly one has exactly the
  person's name — the app shows it chosen, and David still confirms.
- `{"network": "messenger", "confirm": "<candidate id>"}` — that candidate
  (one of this person's last search, within 15 min) is them: kept (`found`,
  through every rebuild and schema change) with a kept Join, so the card
  gains the handle (`sources` a `found` entry, `why` "found on Messenger"),
  the same ghost met in a room later lands on them too, and its avatar is
  their picture. `{"ok", "id", "handle"}`; 409 when it is not one of the
  last search's (search again).
- `{"network": "messenger", "none": true}` — "None of these": kept, and the
  card stops offering it.

404 no such person; 403 another account's book; 429 `{retry_after}` within
10 s of the last search; 503 the bridge cannot search now (none, signed
out, down); 400 another network or no name.

#### Reading one email — `GET /mail/message`, `GET /mail/attachment`, `POST /mail/say` — gated, owner only (20 Oct 2026)

Step 1 of docs/proposals/2026-10-20-mail-in-sasonica.md: a person's email
opens inside Sasonica (mail_read.py). The `id` is notmuch's message id, as
the card's newest-mail `items` carry it. It is this host's mail: another
account's book is 403. Nothing is kept: the file is read and dropped, and
no subject, address or body is logged or written.

`GET /mail/message?id=<id>[&images=1][&format=html]` → `{"ok", "id",
"from", "to", "cc": [{"name", "address"}], "subject", "date": <epoch s>,
"mine", "formats": ["text"?, "html"?], "text", "html", "images_blocked",
"truncated", "attachments": [{"part", "name", "type", "size"}],
"gmail_url"}`.
- The body is `text` (the text/plain part) when there is one, and `html`
  when there is no plain part or `format=html` asks for it and there is
  one; the other is `null`. `formats` says which the message has.
- `html` is made safe on the server with an allow-list: only p, br, div,
  span, b, strong, i, em, u, s, small, sub, sup, a, lists, blockquote, pre,
  code, hr, headings, tables, dl and img; script, style, head, form,
  frames, svg, math and the like go with their contents; no attribute but
  a link's href (http, https, mailto, tel; with `rel="noopener
  noreferrer"` and `target="_blank"`), an image's src, alt, width and
  height, and a cell's colspan and rowspan. No class, id, style or event
  handler survives.
- Pictures: from the web they are a placeholder (`<span
  class="mail-image-off">[alt or image]</span>`) unless `images=1` ("Show
  images", for this one message), and even then only https; one carried in
  the mail (cid:) is put in as a `data:` URL then. `images_blocked` counts
  the pictures left out.
- A body over 512 KB is cut (`truncated`). 400 for a missing or malformed
  id, 404 for one notmuch does not know, 503 when notmuch does not answer.

`GET /mail/attachment?id=&part=` → the attachment's bytes, with its
Content-Type, `Content-Disposition: attachment` and its name, `nosniff`
and `no-store`. `?access_token=` stands in for the bearer, so a plain link
can download it. `part` is from `attachments`; 404 for a part that is not
an attachment of the message, or one over 25 MB.

`POST /mail/say {"id"}` — `auth.may_control_speech`, as `/org/say` →
`{"ok", "chars"}`. The email is read aloud through `media say --private`
only on David's tap, never by itself, and **never kept** (20 Oct 2026): no
speech-history row (so recent, history, catch-up and replay never list
it), "(private)" in place of its words in the speech event log, and its
rendered clips removed with the words kept beside them once played. What
is read: "From <name>. <subject>." then its words (the plain part, else
the HTML's words), with quoted lines (`> …`) left
out and at most 6,000 characters. 422 when nothing readable is left.

#### Speech level — `POST /contacts/<id>/level` — gated (20 Oct 2026)

A person's speech level, on the same scale as a thread's (§6.4
`/session/priority`; David, 20 Oct 2026: "maybe we should … call it the
same thing to keep it simple"). The card carries `level`: `null` for
"Default (follows …)" or one of `interrupt`, `auto`, `pocket`, `normal`,
`quiet` — in the app Interrupt, Auto speak, When open or locked, When open,
Quiet, with the thread sheet's notes.

It is the level of every chat that is theirs — each Matrix room whose one
person they are — unless the room has a level of its own, and the level
their mail notices will follow. A room's level is its own, else its
person's, else quiet (a room starts quiet, 9 Oct 2026): the server
registers that fallback with agent_media_core.speak_priority
(contacts_level.py), so the speech intake's decision and the rows' `speech`
both see it (§6.1 `speech_from`, `speech_fallback`). Kept per book, through
schema bumps; another account's book has only the rooms' quiet.

`POST /contacts/<id>/level {"level": "default" | <level>}` → `{"ok",
"level"}` (null after "default"). 400 for an unknown level, 404 for no such
contact.

It replaced the card's first setting of the same day (`notify`: Default,
Priority, Quiet per chats and email): priority became auto, quiet stayed
quiet, a person's chats setting else their email one, moved once.
`POST /contacts/<id>/notify {"channel", "mode"}` is still read, as a level,
for an app of that day. The rooms' old quiet seed (a level of their own
written once, `seeded-quiet.json`) was moved to the fallback once: a room
still at the seeded quiet lost its own level; one David had set to the
default kept the default of then as its own.

### 6.25a A contact's timeline — gated (18 Oct 2026)

"When did I last talk to them?" (proposal, "Timeline"). Daily rollups —
one entry per contact, per channel, per day: counts in and out, call
seconds, a one-line summary, **never message bodies** — kept in the CRM's
`crm_activity` entity (`integrations/drupal/crm_activity`, an installable
Drupal module) and in this host's ledger, `<state_dir>/timeline.db` (0600),
so a CRM that cannot be reached loses nothing and is written later
(`timeline.py`; pinned by `packages/server/tests/test_contacts_timeline.py`).

Each book (§6.25 "Whose book") has its own ledger:
`<state_dir>/accounts/<key>/timeline.db` for an account other than the
owner's, which counts only `notes` and `email` (`texts` and `calls` are
the owner's rooms and phone) and is switched in its `contacts.json`
`timeline`.

**Sources**, each switched on in `MEDIA_TIMELINE_SOURCES` (comma
separated; default `texts,notes`):

| Source | Default | Channel | From |
|---|---|---|---|
| `texts` | on | `sms`, `signal`, `messenger`, `instagram`, `teams`, `whatsapp` | the Matrix rooms that are the contact's (§6.25 "Which room is whose"): Google Messages is `sms`, Signal `signal`, mautrix-meta's `facebookgo` `messenger` and `instagramgo` `instagram`, Teams `teams`; David's (the owner, his own number's puppet, or his own ghost on a network, §6.25 "The bridged networks") are out |
| `notes` | on | `note` | `POST /contacts/<id>/notes`, or `… contacts note <who> <text>` |
| `calls` | **off** | `call` | the phone's call log over `MEDIA_CONTACTS_ADB`, by number |
| `email` | **off** | `email` | mail headers (From, To, Cc, Date and the list headers), by address — the card's emails and its old chat addresses, raw or decoded: from this host's **notmuch** index when it has one (the owner's book; `notmuch show --body=false`, a month at a time, `MEDIA_TIMELINE_NOTMUCH` the command), else over IMAP: `~/.config/agent-media/timeline-imap.json` or `MEDIA_TIMELINE_IMAP`, `{host, user, password \| password_file \| password_cmd, folder?}` (default folder `[Gmail]/All Mail`; `password_cmd` as mbsync's PassCmd is written, run without a shell). `MEDIA_TIMELINE_MAIL=notmuch\|imap\|off` chooses |

**Which mail counts** (both ways in): **out** when From is one of David's own addresses (`MEDIA_CONTACTS_SELF`'s, every `User` in ~/.mbsyncrc, notmuch's `user.primary_email`/`user.other_email`, the IMAP account), one per contact among To and Cc; **in** when From is a contact's and one of David's own addresses is in To or Cc (a Bcc, a list's copy or a forward is not a conversation); **never** a list or bulk mail (List-Id, List-Unsubscribe, Precedence bulk/list/junk, Auto-Submitted other than `no`), a no-reply sender, spam or deleted, or the Chats label (Google Talk, imported as `chat`, §6.25 "Past conversations", so nothing counts twice). **Only the counts are kept**: the ledger holds the contact, channel, day, counts and its summary line, never a subject, message id, address or link, and notmuch's answer is read in memory and dropped (David, 19 Oct 2026: "I'm not sure we want to double up with all that email"). notmuch holds what mbsync syncs; on red5 that is each account's Inbox, Sent and Spam, so mail archived in Gmail is not counted from it (a 10-year window on 19 Oct gave 19 contacts over 832 days).

Besides the sources, an import writes the years before (§6.25 "Past
conversations"): Messenger and Instagram threads and Gmail's old chats,
into the same entries, merged by the larger count.

The rollup runs after each contacts sync over the last
`MEDIA_TIMELINE_DAYS` (2: yesterday and today), and on `… contacts rollup
[--days N] [--no-push]`; `MEDIA_TIMELINE=0` leaves it off. **A count never
goes down**: a room holds only its newest messages, so a recount of a day
it no longer holds whole is less than the first. An entry's CRM
`source_key` is `agent-media:<channel>:<day>:<contact id>` (a note's
`agent-media:note:<uuid>`): it is found by that key and updated, never
added twice. A contact the CRM does not hold yet keeps its entries here
until it does.

#### `GET /contacts/<id>/timeline[?limit=]` — gated (`auth.gate`)

```json
{"ok": true,
 "entries": [
   {"key": "agent-media:sms:2026-10-18:3f0c…", "channel": "sms",
    "day": "2026-10-18", "count_in": 5, "count_out": 7, "duration": 0,
    "summary": "12 texts (5 in, 7 out)", "origin": "local", "synced": true},
   {"key": "manual:1", "channel": "note", "day": "2026-10-02", "count_in": 0,
    "count_out": 0, "duration": 0, "summary": "Typed in the CRM",
    "origin": "crm", "synced": true}],
 "sources": ["texts", "notes"],
 "crm": {"id": "<person uuid>", "error": null}}
```

- Newest day first, then by channel (`sms`, `signal`, `messenger`,
  `instagram`, `teams`, `whatsapp`, `call`, `email`, `chat`, `note`); at most `limit` (default 100, at most 1000).
- **`via`**: `import` for a day an export wrote (§6.25 "Past
  conversations"; channel `chat` is only ever that: old Google Talk and
  gateway chats), else empty.
- **`origin`**: `local`, this host's ledger, or `crm`, an entry the CRM
  holds that this host did not write (typed in its UI, another writer).
  **`synced`**: whether it is in the CRM yet.
- **`crm`**: `null` when the CRM does not hold the contact; `error` says
  why it could not be read, and the ledger still answers.
- 404 `"no such contact"` for an id the store does not hold.

#### `POST /contacts/<id>/notes {"text"}` — gated (`auth.gate`)

A note on them, today: kept, and written to the CRM. `{"ok": true, "note":
{"key", "day", "summary", "synced"}}`. 400 for no text, 404 for no such
contact, 403 when `notes` is switched off. A note is at most 4000
characters (cut, not refused).

#### `POST /contacts/timeline/backfill` — the owner, or this host's token (19 Oct 2026)

The timeline's past, counted once (timeline_backfill.py): what the phone
and Matrix already hold, never fetched again from a network.

- **The phone** (the owner's book, `MEDIA_CONTACTS_ADB`): `content://sms`
  (`date`, `type`, `address`; type 1 in, 2 out, the rest left out) and
  `content://mms` (`msg_box` 1 in, 2 and 4 out) with each MMS's addresses
  (`content://mms/<id>/addr`: 137 from, 151/130/129 to). `body` is never
  asked for. A message with one other party counts; a group MMS is no one's.
  Channel `sms`.
- **Every bridged room** this server holds that is a contact's (one-to-one,
  §6.25 "The bridged networks"), on a network the timeline knows (`sms`,
  `signal`, `messenger`, `instagram`, `teams`, `whatsapp`): paged back
  through `/rooms/<id>/messages?dir=b`, 100 a page,
  `MEDIA_TIMELINE_BACKFILL_PAUSE_S` (0.25) between pages. Notices, edits
  and other events do not count. **Resumable**: each room's next token, and
  the oldest day reached and not yet whole, are kept in the ledger's
  `backfill` table, so a run cut short goes on where it stopped and a day
  split across two runs is counted once; a room read to its start is not
  read again.
- **Not the bridges' own backfill**: asking a bridge to fetch older history
  into a room would store those messages a second time (and Tuwunel cannot
  take it into an existing room; §6.26). Only counts are wanted.
- Written as imported days are (`via` `import`): **merged by the larger
  count**, so the live rollup of the same rooms and the phone's SMS against
  the Google Messages room never add up to more than happened, and running
  it again changes nothing. Then the ledger goes to the CRM when there is
  one.

It runs in the server's process (the rooms are held there), as a job:
`POST` starts it unless it is running and answers at once,
`{"ok": true, "started": bool, "running": bool, "started_at", "phone",
"rooms"}`; `GET` of the same path answers how it is doing, `finished_at`
once done. `phone` is `{"sms", "mms", "counted", "nobody", "groups",
"days"}` or `{"error"}` (no phone tool, the phone off Wi-Fi); `rooms` is
`{"rooms", "done", "messages", "days", "errors"}`, `errors` the rooms cut
short (run it again). Counts only, never a name or number. The owner's
book alone: an enrolled owner's device or this host's own token (the CLI);
403 for another account, 401 for nobody. `… contacts backfill` asks the
running server and waits (`--no-wait` returns, `--status` only asks).

### 6.26 Bridges: signing in from the app — gated, owner only (19 Oct 2026)

David: "Can we create a way to login to bridges from sasonica?" The
Matrix rooms (§6.1 "Matrix rows") come from mautrix bridges on this host
(Google Messages, Meta, Instagram, Teams, …). Each speaks the standard
bridgev2 provisioning API (`/_matrix/provision/v3`, bearer = its
`provisioning.shared_secret`, `?user_id=` the owner's Matrix id). The
server relays it so the app can see each bridge's sign-ins and sign in,
sign in again or sign out, without the bot chat. The app stays generic: it
draws whatever step a bridge sends (`bridges.py`; pinned by
`packages/server/tests/test_bridges.py`).

**Which bridges.** Found from their config files: `MATRIX_BRIDGES` (a
comma-separated list of paths or globs; default
`~/.local/share/mautrix-*/config.yaml`), reading only `appservice.id`,
`appservice.hostname`/`port`, `provisioning.shared_secret` and
`homeserver.address` (for the icon); a file without a secret (or with
mautrix's `generate`/`disable`) is skipped, and a wildcard hostname
(`0.0.0.0`) is asked on loopback. The id is the bridge's `appservice.id`.
The secret never leaves the server. The Matrix user is `MATRIX_OWNER_ID`
(as §6.1).

**Who.** Every route is `auth.gate`d and answers only the owner's account
(`auth.request_account() == ""`); any other caller gets 403 `"bridges are
the owner's"`. With no bridges configured, `GET /bridges` is `{"ok": true,
"bridges": []}`.

#### `GET /bridges`

```json
{"ok": true,
 "bridges": [
   {"id": "msteams", "name": "Microsoft Teams", "network": "teams",
    "icon": "/bridges/msteams/icon", "reachable": true, "error": null,
    "flows": [{"id": "device_code", "name": "Work or school account",
               "description": "…"}],
    "logins": [
      {"id": "…", "name": "david@example.com",
       "state": "CONNECTED", "error": null, "message": null,
       "since": 1792339271}]}]}
```

- One per bridge, in `name` order. `name` (`network.displayname`),
  `network` (`network.network_id`, else `network.beeper_bridge_type`,
  else the id) and `flows` come from the bridge's `whoami`; `icon` is its
  `network_icon` served through the server (`GET /bridges/<id>/icon`, the
  image bytes, fetched once from the homeserver's media with
  `MATRIX_OWNER_TOKEN` and kept; null when it has none; 404 when it
  cannot be had). The icon route takes `?access_token=` in place of the
  bearer, for an `<img>`.
- `logins[].profile` (20 Oct 2026, David: "can we display name, email,
  phone number and username or whatever is available"): the account as
  the network knows it, bridgev2's `RemoteProfile`, with only the fields
  the bridge filled: `name`, `email`, `phone`, `username`, and `avatar`,
  the path of its picture (`GET /bridges/<id>/avatar/<login id>`, served
  like the icon, `?access_token=` for an `<img>`; 404 when it has none).
  What each fills varies: Telegram all four and a picture, WhatsApp name
  and phone, Instagram name and username, Messenger name and picture,
  Google Messages and Teams nothing beyond `name`. `{}` when nothing.
  A field the bridge leaves out is filled from the owner's own data export
  when it has it (`<state_dir>/owner-networks.json`, `{network: {name?,
  email?, phone?, username?}}`, written by the contacts import from
  Facebook's and Instagram's personal-information files; Messenger's email
  comes from there), never over what the bridge said. The import reads
  Facebook's `personal_information/profile_information/profile_information.json`
  (`profile_v2`: `emails.emails[0]`, `phone_numbers[0].phone_number`,
  `username`, `name.full_name`) and Instagram's `personal_information.json`
  (`profile_user[0].string_map_data`: Email, Phone Number, Username, Name),
  each alone in a zip or as one file; only the fields present are kept, a
  network's entry is replaced whole by each newer export and the others
  are left; 0600, written atomically, and only by the owner's book (another
  account's import writes nothing). `POST /contacts/import` answers
  `profile: {network: [field names]}`, never the values; the export links
  ask for Personal information → Profile information too.
- `logins[].state` is the bridge state event as the bridge says it
  (`CONNECTED`, `CONNECTING`, `BACKFILLING`, `TRANSIENT_DISCONNECT`,
  `BAD_CREDENTIALS`, `UNKNOWN_ERROR`, `LOGGED_OUT`, …); `error` its error
  code, `message` its text, `since` its timestamp (s). **Needs you**:
  `BAD_CREDENTIALS`, `LOGGED_OUT` and `UNKNOWN_ERROR` — the app marks them
  and offers Sign in again.
- A bridge that does not answer (or refuses) is listed with `reachable:
  false`, `error` saying why, its id as `name` and `network`, no `icon`
  and empty `flows`/`logins`; the others still answer (they are asked at
  once, 5 s each).

#### Signing in: one login process, polled

A sign-in is a **login process** the server holds (in memory, for as long
as the bridge does; forgotten 30 min after its last step). Its shape, as
every route below answers:

```json
{"ok": true,
 "login": {"id": "<process id>", "bridge": "msteams",
           "status": "step" | "waiting" | "complete" | "failed" | "cancelled",
           "error": null,
           "step": { …the bridge's LoginStep, as it sent it… }}}
```

- `step` is the bridgev2 `LoginStep` verbatim (`type`, `step_id`,
  `txn_id`, `instructions`, and exactly one of `user_input`, `cookies`,
  `display_and_wait`, `complete`, `client_http`, `webauthn`), minus
  `login_id`, which is `login.id`. The app draws `user_input` (a form:
  `fields[]` with `type`, `id`, `name`, `description`, `default_value`,
  `pattern`, `options` for `select`), `display_and_wait` (`type` `qr`
  (`data` the QR's text), `code` or `emoji` (`data`, `image_url`),
  `nothing`; URLs in `instructions` are links), `cookies` (below) and
  `complete` (`complete.user_login_id`). It does not do `client_http` or
  `webauthn`: it says the sign-in needs the bridge bot and offers Cancel.
- **`display_and_wait` is waited on by the server**, not the app: on that
  step the server starts the bridge's wait in the background and answers
  at once with `status: "waiting"`; the app polls `GET` (every 2 s) and
  can leave to a browser and come back. When the wait ends the next step
  (or `complete`, or the error) is there. A QR that changes is a new step
  (a new `txn_id`).
- `status: "failed"` carries `error`, the bridge's message (e.g. a wrong
  password); the process is over and a new sign-in starts again.

`POST /bridges/<id>/login {"flow"}` — starts `flow` (one of the bridge's
`flows[].id`); answers the process. 404 for no such bridge, 400 for no
- **`open_url`** (19 Oct 2026, "streamline … also for teams"): for a
  `display_and_wait` step of type `code`, the first http(s) URL in its
  `instructions` (Teams: `https://login.microsoft.com/device`), else null.
  The app opens it in one tap with the code copied and filled in.

**One flow first.** A bridge lists its recommended flow first (Teams:
`device_code`), so the app offers `flows[0]` as the main button and the
rest under "Other ways to sign in", and starts it directly when there is
only one.

**The watch** (`bridges.start_watch`, run by the canvas; off with
`MEDIA_BRIDGES_WATCH=0`): every 10 min each bridge's sign-ins become an
alert (§6.17), `bridge.<id>`, at `warn` while any of its sign-ins needs
you (title "<name> needs you to sign in again", the login names and the
bridge's message as detail), `ok` otherwise. A bridge that does not answer
reports nothing, so its last word stands; `every_s` is 30 min.
such flow, 502 when the bridge refuses (its message in `error`).

`GET /bridges/<id>/login/<process>` — the process now. 404 when the server
no longer holds it.

`POST /bridges/<id>/login/<process>/step {"txn_id", "values"}` — submits
the current step: `values` is `{field id: string}` for `user_input` and
`cookies` (a `display_and_wait` step is not submitted). A `txn_id` that is
not the current step's is 409 (`"step moved on"`, with the process as it
is), as is a submit while another is still with the bridge; a
`display_and_wait` (or `complete`) step submitted is 400. Answers the
process after the bridge's reply.

`POST /bridges/<id>/login/<process>/cancel` — cancels it at the bridge;
`status: "cancelled"`.

`POST /bridges/<id>/logout/<login id>` — signs that login out at the
bridge; answers `{"ok": true, "bridge": …}`, `GET /bridges`' entry for
the bridge. 404 when the bridge has no such login.

#### Setting a bridge up — its own keys (20 Oct 2026)

Some bridges need values of the owner's own before they can run at all:
Telegram's needs an API id and hash from https://my.telegram.org/apps
(David, 19–20 Oct 2026: "Maybe the API key should be able to be entered in
the bridges settings section too"). The server knows which
(`bridges.SETUP`, by appservice id) and reads the bridge's config file; a
value still the example config's placeholder is unset.

- A bridge with unset values is listed **without asking it** (it cannot
  start): `reachable: false`, `error: null`, no flows or logins, its
  `name` from the table, and `setup: {needed: true, instructions, url,
  fields: [{id, name, type, pattern}]}`. The app shows the instructions,
  the link and a form, and no sign-in buttons.
- A bridge in the table that is set up carries `setup` with `needed:
  false`, so the app can offer to change the keys. A value is **never**
  sent back.
- `POST /bridges/<id>/setup {"values": {field id: string}}`: each value
  trimmed and checked against its `pattern` (400 says which, nothing
  written); written into the bridge's config in its own section (0600,
  replaced whole), the bridge's `mautrix-<name>.service` restarted, and the
  answer is `{"ok": true, "bridge": …}`, its listing entry once it answers
  (up to 30 s; still `reachable: false` if it is slow). 404 for a bridge
  with nothing to set up. Owner only, like every route here.

#### A `cookies` step in the app

The bridge wants cookies from a real sign-in page (Facebook, Instagram,
Google Messages). `cookies` is `{url, user_agent?, fields[], extract_js?,
wait_for_url_pattern?, hidden?}`; each field has `id`, `required` and
`sources[]` (`type` `cookie` with `name` and `cookie_domain`;
`local_storage`; `request_header`; `request_body`; `special`), any one of
which is enough.

- **On Android** the app opens `url` in a native, full-screen WebView of
  its own (with `user_agent` when given), the user signs in, and the app
  reads the `cookie` sources from Android's cookie store for
  `https://<cookie_domain>` (HttpOnly ones included), `local_storage`
  ones and `extract_js` by script. It submits when every required field
  has a value and the page's URL matches `wait_for_url_pattern` (or when
  the user taps Done with every required field found). It then removes
  those domains' cookies from the WebView, so the app holds no session of
  its own. `request_header`/`request_body` sources are not read.
- **Anywhere else** (a browser) the app cannot read another site's
  cookies: it shows the step's `instructions` and a box for the JSON of
  the cookies (what the bridge bot takes), submitted as `values`.

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
| `hello` | `{"kind": "hello", "page": "<12-hex digest of the page>"}` — first frame of every connection, and sent again (within ~2 s) when the page's files change on disk; the canvas re-reads them on change, so a page change needs no restart. A client holding a different page reloads, once, after the current reply |
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
     the app reads; over a quick tunnel it also carries
     `&lookup=<percent-encoded https://sasonica.com/r/<install id>>` (see
     "The lookup" below);
   - `http://<host>:<port>/pair?c=<code>&device=1` — for the person at the
     terminal only. Opened in a browser it is **refused** (it reaches
     `GET /pair`, the amux page, which does not know device codes).

   `--host` and `--port` set the base, as they do for the amux link.
   Default host (22 Sep 2026): `MEDIA_VISUAL_PAIR_HOST`, else this
   machine's **tailnet IP** (`tailscale ip -4`), else its hostname — a bare
   MagicDNS name (`red5`) is not reachable from Sasonica, whose
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
    "server": {"name": "red5", "base": "http://red5:8781"},
    "server_id": "q3Zr0bW8kXy1Tz5m"}
   ```

   - `server_id` (20 Oct 2026) is this server's stable id, made once into
     `<state_dir>/server-id` (the id sasonica.com's server list uses,
     `servers_report.py`): an app paired with several servers (roadmap
     item 7, docs/proposals/2026-10-20-several-servers.md) tells them
     apart by it rather than by an address a quick tunnel changes.
     `POST /enrol` and `GET /me` carry it too.
   - `server.name` is `MEDIA_SERVER_NAME` (20 Oct 2026), else the host's
     `gethostname()`, as on sasonica.com's list. `server.base` is the
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
   - `server.lookup` (16 Oct 2026) is present only when the request came in
     on this server's running quick tunnel (`base` equals the URL in
     `<state>/tunnel.json`) and publishing is on: the URL the app asks for
     the server's new address after the tunnel restarts under another name.
     Absent over a tailnet, a named tunnel or a proxy, whose names do not
     change.

   **The lookup** (`deploy/lookup`, a Worker at `sasonica.com/r/*`;
   docs/proposals/2026-10-03-off-the-tailnet.md). `media-tunnel run`
   publishes each new quick-tunnel URL, and again daily:
   `PUT https://sasonica.com/r/<id>` `{"url", "ts", "pub"}`, header
   `X-Sasonica-Signature: base64url(Ed25519(raw body))`, signed by the key in
   `<state>/install-key` (made on first use, mode 600). `<id>` is
   base32(sha256(pub)[:16]), lower case, 26 characters, so only that key can
   write it. The app reads it with no auth:

   - `GET /r/<id>` → 200 `{"url": "https://<name>.trycloudflare.com",
     "updated": <unix s>}`, or 404 `{"error": "no such install"}`. CORS `*`.
   - The app keeps `lookup` (from the link or `server.lookup`, the latter
     winning) with its server. When a request to its base cannot get
     through — the fetch throws, or a dead tunnel's 502/503/504/530 — it
     GETs the lookup (one ask shared by concurrent failures, at most one per
     20 s); an https `url` other than the current base becomes the new base,
     stored, and the request is retried there once. The device token holds
     across names. (sasonica-app api/auth.ts `relocate()`, test/lookup.mjs.)
   - Only `https://*.trycloudflare.com` origins are accepted; entries live
     60 days from the last write. `MEDIA_LOOKUP_URL` sets the base
     (`-`, or `MEDIA_LOOKUP=0`, turns publishing off and drops `lookup`
     from links and answers). `media-tunnel id` prints the id and URL;
     `media-tunnel publish` publishes now.
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
  `[enrols]`. A device already paired gets it with `devices --enrol ID`
  (and loses it with `--no-enrol ID`), without pairing again (27 Sep 2026).
  No route grants it.
- The app keeps `enrol` from `POST /pair` and shows Settings → Devices only
  when it is true. A device paired by a build that did not keep it asks
  `GET /devices` once and records 200 or 403 (sasonica-app `devices.tsx`).
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

### Enrolling with an account — BUILT 29 Sep 2026

The accounts proposal, steps 2 and 3. An account enrols a device; it does not
authenticate requests. Code: `agent_media_server/oidc.py`,
`devices.enrol_account`; pinned by `packages/server/tests/test_oidc.py`.

```
POST /enrol {"id_token": "<jwt>", "device": "Pixel 8a"}     no credential, like /pair
  → {"ok", "token", "device_id", "name", "enrol", "account", "server": {"name", "base"}, "server_id"}
  → 403 {"code": "bad_id_token"}   untrusted issuer, wrong audience, expired, bad signature
  → 403 {"code": "not_enrolled"}   a valid sign-in for an account not allowed here
  → 404 {"code": "no_accounts"}    this server takes no account sign-ins
  → 429 {"code": "rate_limited"}   as /pair (failures count the same way)
```

The app's flow: `GET /enrol` (open) → `{"accounts": [{"issuer",
"authorization_endpoint", "client_id", "scopes"}]}` says where to sign in
(`[]` when off); the app signs in there with PKCE, and sends `POST /enrol
{"code", "code_verifier", "redirect_uri", "device"}` — the server swaps the
code for the ID token at the issuer's token endpoint (a public client, no
secret) and then goes on as for an `id_token`.

`GET /me` (a device token) → `{"ok", "device_id", "name", "enrol",
"account", "username", "picture", "issuer", "lookup", "server_id",
"server_name"}` (`server_id` and `server_name` as `POST /pair` gives them,
20 Oct 2026): this device as the server knows it, `account` the
email of the account that enrolled it (null for a code-paired device) — the
app's "Signed in as" (29 Sep 2026). `picture` is the account's picture (the
OIDC `picture` claim, an https URL only; null when there is none) for the
app's top-right corner. `lookup` (19 Oct 2026) is `server.lookup` as
`POST /pair` gives it (asked over this server's quick tunnel, publishing
on), else null: a device paired before the lookup existed (16 Oct 2026)
keeps it from here, so it finds the server after the tunnel's next name
without pairing again. 401 for anything but a device token.
An account's details stay current: the refresh token from the sign-in is
kept on the device row (never listed or returned), and a `/me` that finds
them over an hour old asks the issuer's userinfo again in the background, so
an email or name changed on cms.sasonica.com shows on the next `/me`. A
refusal leaves the details as they were; it does not sign the device out.
Allow accounts by `sub` (the issuer's user id, which does not change) rather
than by email.

`GET /me/account` and `POST /me/account {"action", …}` (a device token) — the
app's profile sheet: the account that enrolled this device as its issuer has
it (`{"ok", "username", "email", "picture", "edit"}`), and changes to it —
`username {username}`, `picture {data: base64}` (the body cap is 64 KB: the
app sends a small JPEG), `remove_picture`, `email {email}` (a confirmation
link to the new address; nothing changes until it is used; the old address
then gets an undo link), `password {current, password}`, `password_link`
(the issuer's set-password email), `delete` (an emailed link that deletes
the account after a button press; refused for the site owner, `site_owner`). Passed to the issuer's `/api/account`
with the account's own access token, got through the refresh token kept at
sign-in (one refresh at a time, the access token reused while it lasts);
the issuer's answer and status come back as they are, and the device row
follows a change. 409 `sign_in_again` for a device without an account
token (paired by code, or signed in before this); 502 when the issuer
cannot be reached.

`POST /me/signout` (a device token) → `{"ok", "id"}`: sign out — this device
forgets itself, as `devices --revoke` would (its token stops working, its
row and any account refresh token go). Any device may; 401 for anything
but a device token. The app's profile menu → Sign out.

Configuration: `MEDIA_OIDC_ISSUERS` (exact `iss`, comma-separated),
`MEDIA_OIDC_CLIENTS` (the `aud` an ID token must carry), `MEDIA_OIDC_ALLOW`
(`<iss>|<sub>` or `<iss>|email=<addr>`, a verified email only: who may have a
device here; nothing by default) and `MEDIA_OIDC_ENROL` (the same form: whose
device gets the enrol bit). RS256 only, checked with the standard library; the
discovery document and JWKS are cached an hour and an unreachable issuer is a
refusal. The device row gains `iss`, `sub` and `account` (the email);
`media-visual-canvas devices --revoke-account <sub|email>` drops every device
an account enrolled. The issuer decided on is Drupal (cms.sasonica.com).

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
| `snapshot` | `{"ok": true}`, the `/conversation/log?session=` envelope — `messages` (the newest **30**, or `?limit=`), `older`, `lines` (deprecated; only those of the page, below), `pending`, `working`, `approval`, `suggestion`, `recap` — plus `{"state", "session_live": bool, "live": bool (deprecated alias of `session_live`), "pane", "resumable"}`, and (22 Sep 2026) `"agents": {"running", "total"}` (§6.12), `"project"`, `"cwd"` (§6.1), and (19 Oct 2026) `"move_pending"?`, `"move_failed"?` (§6.15, only when there is one), `"title"` (the name `/targets` would give it, `""` when none), (20 Oct 2026) `"background"` (`{"agents", "commands"}` or null, §6.1 `/sessions/state`'s, on a `waiting` thread only), and `"restart_pending"?` (§6.1, a headless thread's, only while a restart of the session host is waiting), and (20 Oct 2026) `"quota"?` (§6.1, `{"resets_at", "state"}`, only while the thread waits for the usage limit's reset) | first frame on every connection |
| `message` | `{"op": "append" \| "replace", "message": <message>}` (§6.2.2) | a message appears, or one (matched by `id`) changes: grows a part, a tool finishes, its turn ends, it gains or loses `spoken` |
| `live` | `{"id", "at", "sentences", "sentence", "offsets", "elapsed", "server_time", "delay", "paused"}` — the message being spoken, by `id` — or `null` when nothing is | a new reply starts, the sentence changes, pause or resume, or the clock jumps (more than 1 s from where it should be: a skip) — **not** at 1 Hz; the client runs the clock between frames |
| `working` | the `working` object, or `null` | a step starts, or the turn ends |
| `approval` | the `approval` object, or `null` | a dialog appears, changes or goes |
| `suggestion` | `{"text": "…"}` | the ghost or follow-up arrives or clears |
| `state` | `{"state": "working" \| "waiting" \| "approval" \| "ended", "session_live": bool, "live": bool (deprecated alias), "pane", "background": {"agents", "commands"} \| null}` — `background` (20 Oct 2026): work still running once the turn is over, as on §6.1's `/sessions/state` rows; null unless `waiting` | the session changes state, or its background counts change; `ended` when its pane goes |
| `pending` | `{"pending": bool}` | it changes — the log's rule: a live session whose last message is the listener's, the turn working (a step running, or a headless session `working`), or the last line the listener's. Set from the transcript as soon as a prompt lands; cleared by the 1 s / 3 s re-read |
| `recap` | `{"text", "at", "source"}` or `null` | a newer recap is written |
| `context` | `{"used", "window"}` or `null` (7 Oct 2026) | a reply changes it (checked on the 1 s / 3 s re-read) |
| `agents` | `{"running", "total"}` — the thread's background agents (§6.12) | an agent starts, ends or resumes (checked on the 1 s / 3 s re-read) |
| `move` | `{"move_pending", "move_failed", "project", "cwd"}` (19 Oct 2026) — a move waiting for the session to be free, one that failed when it ran (each `null` when none), and where the thread is now (§6.15) | a move is asked for, starts waiting on something else, lands, fails or is cancelled (checked on the 1 s / 3 s re-read) |
| `title` | `{"title"}` (19 Oct 2026) — the thread's name, so an open thread's header follows a rename made elsewhere | a live thread's name changes (the name it gives itself after its first turn, `/rename` at the terminal; checked on the 1 s / 3 s re-read, from the cached `/sessions/state` sweep), or any thread is renamed through `POST /rename` (at once) |
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
- A `recap` event was added, and a `context` event (7 Oct 2026).
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
- **A message held behind a long command.** A message sent mid-turn joins
  it at the next tool boundary, so a foreground command (a blocking Bash
  call, a subagent) holds it unread for as long as it runs. Once such a
  command has run `MEDIA_SESSIOND_BACKGROUND_AFTER` seconds (30; 0 never)
  with a message queued behind it, sessiond backgrounds it — the
  `background_tasks` control request, Ctrl+B's own: the call returns "manually
  backgrounded", the message is read, and the agent is told when the command
  ends. Marked in the events as `{"type": "sessiond", "subtype":
  "backgrounded"}`. Verified on 2.1.299, 14 Oct 2026.
- **Parking.** Waiting for `MEDIA_SESSIOND_IDLE` (1800 s) → stdin closed,
  process gone, record `parked`; the next message resumes it with
  `--resume`. Never parked while working, on an approval, with queued
  messages, or with a task in flight — a background subagent or Bash call
  outlives its turn: kept from `task_started` (refreshed by `task_progress`)
  until `task_notification`, a final `task_updated`, or the process exits,
  and no longer counted once unheard of for `MEDIA_SESSIOND_TASK_STALE_S`
  (3600 s), so a lost notification cannot pin a session. Idle time counts
  from the last event or task activity, whichever is later. The session
  record carries `tasks`, how many count now. At most `MEDIA_SESSIOND_MAX`
  (6) live, and none more while the host has under `MEDIA_SESSIOND_MIN_FREE_MB` (1024) available and one is
  already running; one more parks the least recently used idle one (no
  tasks in flight either), else 503 `busy` (its sentence says when memory
  was the reason).
- **Restarts.** A sessiond restart ends its children. A permission request
  pending then is not re-sent by the CLI on `--resume`, so it is recorded
  under `lost`: the thread shows no approval, `/session/answer` for it is
  409 `code: "lost"`, and the next message resumes the session (the model
  sees its tool call interrupted and asks again if it still wants it). At
  start-up any child a crashed instance left running is terminated. To restart it from inside one
  of its own sessions, use `media restart-services --only sessiond --after
  10` (docs/reference/version-skew-monitoring.md). It waits until no other
  session is busy, and the asking turn ends first.
- **Draining** (19 Oct 2026). That wait is a drain: while the restart is
  pending (`$XDG_STATE_HOME/agent-media/restart-pending.json`, written by
  `restart_services`), sessiond holds every new message (`send` answers
  `held: true`; the message is kept in the session's record, so it
  outlives the restart) and refuses new chats (`restart_pending`), so no
  session becomes busy again; each busy session is told once, by its
  context hook, to end its turn. A session whose only business is a
  question or permission card does not hold the restart up: the agent
  cannot finish it, only David can, and the card is lost with the restart
  and asked again (next item). Answering a card is not held. The restart
  fires the moment nothing is busy, or gives up after 30 min
  (`MEDIA_RESTART_DRAIN_MAX_S`), and either way the held messages are then
  delivered in order. A pending restart whose detached run stops checking
  in (90 s) holds nothing.
- **Resuming** (19 Oct 2026). Before a stop parks anything, sessiond notes
  on each live Claude session what it was doing (`cut_off`: a turn under
  way and its last message, a card up, its background tasks by
  description, messages the CLI had queued); a crashed instance's records
  say the same, and a child the stop's cgroup kill took first counts too.
  At the next start each session cut off is resumed, once, with a message
  from `[From the session host]` saying what was running and to pick up
  where it left off (a card-only session: to ask again). Each background
  agent the stop cut off is named with its id, and the session is told to
  continue it with SendMessage to that id — Claude Code resumes a subagent
  from its own transcript (`agent-<id>.jsonl`), so its work is not redone —
  and to tell David; a background command cannot be resumed and is listed
  to re-run if still needed (David, 19 Oct 2026: "It should be able to
  manage background agents too"). Idle sessions are not woken. Its held
  messages follow.
- **The usage limit** (20 Oct 2026: "We hit a quota again and sessions got
  stopped mid-turn … automatically pick them up when we're back on"). All
  Claude sessions on a host share one subscription. A turn the limit ends
  is a `result` with `api_error: "usage_limit_reached"` (HTTP 429, "You've
  hit your session limit · resets 2am (Australia/Melbourne)"), after a
  `rate_limit_event` whose `rate_limit_info.status` is `rejected` and whose
  `resetsAt` is the reset. sessiond records that session's `cut_off` (why
  `quota`, `resets_at`, the same fields as a restart's) and puts it in the
  host's one line, `$XDG_STATE_HOME/agent-media/quota.json` (core
  `quota.py`: `resets_at`, `since`, `state`, `waiting`). So is a background
  agent the limit ended by itself (its `task_updated`/`task_notification`
  failed with the limit's words), against its session, even one whose own
  turn was over or carried on. A **desk** session (a pane) is put in the
  same line by its `StopFailure` hook (matcher `rate_limit`; Claude Code
  fires it instead of `Stop` when an API error ended the turn), with its
  pane, its last message and any background agent whose transcript under
  `<session>/subagents/` ends on the limit. The reset is `resetsAt`, else
  read from the sentence, else a backoff (15 min, doubling to 2 h) whose
  first resume is the probe. **While the account is limited**, a message to
  a Claude thread is **held** (`held: true`, as for a restart, plus
  `quota: {"resets_at", "state"}`; §6.2.2), since a turn would only fail,
  and a new Claude chat is refused 503 `code: "quota_limited"` with a
  sentence saying when it resets (and `quota`). **At the reset** plus
  `MEDIA_QUOTA_RESUME_MARGIN_S` (60 s) the waiting sessions are resumed
  oldest first, one every `MEDIA_QUOTA_RESUME_GAP_S` (20 s), each once,
  with "[From the session host] You were stopped by the usage limit at
  <time>; it has reset. Pick up where you left off (the last message in
  was: …)." and, for each stopped agent, its id to continue with
  SendMessage and to tell David (an agents-only one: "A background agent of
  yours was stopped by the usage limit …"); background commands that
  finished meanwhile are named with their output file. A headless one gets
  it as a message (its held messages follow); a desk one has it typed into
  its pane, never over a draft (retried), and not at all if its pane is
  gone or its transcript no longer ends on the limit (it was picked up
  already). One that hits the limit again moves the reset and the rest
  wait for it. A message to a thread still in line after the reset resumes
  it at once, out of turn. A request that goes through while the line says
  limited (`rate_limit_event` allowed, a minute in) ends the wait early. The
  line is on disk, so it outlives a sessiond restart; at start a session
  whose last turn ended on the limit before this existed (its last
  `result`, its last rejected `rate_limit_event`, its agents' `task_*`
  events) is put in line too, once (`quota_resumed` in its record).
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

**Cloudflare swaps 502 and 504 (13 Oct 2026).** Through a Cloudflare tunnel,
an origin's 502 or 504 is replaced by Cloudflare's own HTML page, which has
no `Access-Control-Allow-Origin`, so a browser or WebView client's fetch
throws and the app says "Could not reach <server>" instead of showing the
server's error. 400, 408, 409, 422, 500, 501 and 503 pass through untouched
(tested with a quick tunnel). So the server answers 500 for 502 and 503
for 504 when the request carries `Cf-Ray` (`app._status`), leaving tailnet
and LAN clients the real codes. (The fix was lost from main on 13 Oct and
restored on 18 Oct.) When "Could not reach" appears only on the public route
while the canvas log shows the request arriving, check this first.

### C. Tailnet only

What ships today, and the shape to offer **first** to anyone who already has
a mesh (David, 23 Sep 2026): it is the least work and the least exposure.
It is simply not one a stranger can adopt, so it cannot be the only one.

**Correction to §9 (23 Sep 2026).** §9 says Sasonica Next's[^next-rename] network security
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

### Thread links (18 Oct 2026)

A link to one thread is `sasonica://t/<session>` (any server) or
`https://<server>/app/t/<session>`: the web app's own thread page
(web_app.py), which a browser opens there. On a phone with the app, the
https form is an Android App Link for the hosts the app's manifest claims
(red5.sasonica.com today): Android checks `GET /.well-known/assetlinks.json`
(app_links.py — no credential, every listener, the public one included),
which names `com.sasonica.app` and `com.sasonica.app.debug` with their
signing keys (`MEDIA_APP_LINKS_EXTRA` adds others). Tapped inside the app,
either form opens the thread in place (sasonica-app lib/threadLinks.ts).

Agents are told their own: the context hook (`media-hook-context`,
intake/context.py) adds "This thread's link is …" to a session's context on
its first prompt and again after a compaction, built from
`MEDIA_PUBLIC_URL` (an https address in `~/.config/agent-media.env`; without
one, the `sasonica://` form), with the ask to link any session it names the
same way. `MEDIA_THREAD_LINK=0` leaves it out.

The same hook also tells each session, once (and again after a compaction),
how to write for a reader who mostly listens on a phone: the rules in
`agent_media_core/agent_notes.md` — spoken replies kept short, things to
copy in code blocks, full https links, a deadline on any background wait;
for Claude Code, a plain description on every Bash call and questions as
AskUserQuestion cards; with the canvas on, the `[[visual:]]` markers.
`MEDIA_AGENT_NOTES=0` leaves them out, `MEDIA_AGENT_NOTES_FILE` replaces them.

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

[^next-rename]: Sasonica Next became Sasonica (`com.sasonica.app`) on 26 Sep
    2026, and the older Sasonica app became Sasonica ABS (`com.sasonica.abs`).
    Their speech targets followed on 27 Sep: `next` → `sasonica`, `app` → `abs`.
    Dated text keeps the names it was written with.
