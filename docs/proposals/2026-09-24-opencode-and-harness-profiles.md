# Proposal: opencode as a fifth harness, profiles for every harness, and what to take from opencode-mobile (24 Sep 2026)

Status: **step 1 (opencode as a harness) built 24 Sep 2026, uncommitted;
the rest is a proposal.** Extends `server-contract.md` §6.6
(harness setup) and §6.16 (every harness's conversations). Touches
`agent_media_core/harnesses.py` (`HARNESSES`, `RECIPES`, the transcript
readers), `agent_media_server/harnesses.py`, and the app's Coding agents
page.

## What prompted it

David asked (24 Sep 2026) whether
[dzianisv/opencode-mobile](https://github.com/dzianisv/opencode-mobile) is
worth borrowing from or basing Sasonica Next[^next-rename] on, and raised two wants: an
installer, and multiple logins — for opencode, and for pi "if we go that
route".

## opencode-mobile, briefly

An Android client for the opencode agent. React Native + Expo, TypeScript,
MIT (VIBE TECHNOLOGIES, LLC), about 184 stars, v0.4.7 when looked at. A thin
client of one self-hosted `opencode serve`, over opencode's HTTP + SSE API:

- sessions (browse, create), token-by-token streamed chat
- a side-by-side diff viewer for the files a session changed
- tool-call approval (allow / deny a call before it runs)
- saved connections — LAN, Tailscale, Cloudflare Tunnel, ngrok — and a
  tunnelling wizard (beta)
- biometric lock, credentials in the Android Keystore
- an offline demo mode: a scripted ~30 s bug-fix session with no server
- shipped three ways: Google Play, **a self-hosted F-Droid repo on GitHub
  Pages**, and signed APKs on GitHub Releases
- no voice, no push notifications, no iOS

## Decision: do not base Next on it

It is built around the one thing Sasonica is deliberately not: a single
harness's own server API. Sasonica's seam is agent-media — four harnesses
behind one contract (§6.16), plus speech, the canvas, pairing (§9),
headless sessions (§17). Rebasing on opencode-mobile would trade that for
one harness with better screens, and its React Native stack shares no code
with the Capacitor web bundle `chat/` builds. The chat app stays the base.

Licence is not the obstacle: MIT code may be taken into the Apache-2.0
`chat/` with its copyright notice kept (add it to `NOTICE`). The obstacle is
that its components are React Native, so what transfers is mostly design,
not files.

## What to take

In the order they are worth doing:

1. **A diff viewer.** Sasonica has nothing that shows what a session changed,
   and "what did it do to my code" is the question most asked from a phone.
   Server side, a gated `GET /sessions/{id}/diff` (the session's cwd, `git
   diff` against where it started, or the harness's own record of edits —
   Claude's `Edit`/`Write` tool calls are in the transcript); app side, a
   unified view by default, side-by-side in landscape.
2. **Tool-call approval.** The waiting card (Sasonica `cf0624bd`) already
   answers a harness's questions. A permission prompt is the same shape — a
   call, its arguments, Allow / Deny / Always — and their screen is a good
   reference for laying out a long argument (a shell command, a patch) on a
   phone.
3. **An install channel that is not a store: an F-Droid repo on GitHub
   Pages.** This is the "installer" for the app itself. `fdroidserver`
   builds the index from the CI's signed APK; the release key already lives
   in dotfiles-secrets (see the Sasonica release-signing notes), so it is a
   CI job and a Pages branch. Users add one repo URL in F-Droid / Obtainium
   and get updates without `agent-phone-adb install`.

   **Parked until monetization is decided** (David, 26 Sep 2026). Where the
   app is distributed decides which ways of charging for it are open, so it
   waits on `docs/proposals/2026-08-20-monetization.md`:
   - F-Droid's **main** repository builds from source and will not ship
     proprietary pieces (Play Billing included), so a store-billed paid tier
     cannot be sold through it; a paid tier would be a licence key there,
     which that proposal calls honour-system revenue.
   - A **self-hosted** repo (the one proposed here) has no such rules, but it
     also has no billing: it can only hand out an APK that checks a key
     bought somewhere else.
   - Once an APK is public it stays public, so shipping one before the
     free/paid line is drawn gives away whatever it contains.
4. **Demo mode.** The chat app's mock server (`chat/mock/server.mjs`) is
   already most of it. Bundling a scripted session into the app, reachable
   from the pairing screen as "Try it without a server", gives screenshots,
   store review and e2e tests a server-less path.
5. **A list of servers, not one.** Pairing holds a single server today.
   Their connection list (name, address, which one is active) is the right
   shape once there is a second box (hpo, a hosted tier). Low priority until
   there is.

Not taken: biometric lock (the device token is already in Keystore-backed
storage; add the lock only if someone asks), the tunnelling wizard
(Tailscale is the answer here).

## opencode as the fifth harness

**Built 24 Sep 2026**, checked against opencode 1.18.32 installed into a
throwaway prefix on red5 (sessions run on its free `opencode/mimo-v2.6-flash-free`,
so no key was spent):

| | opencode 1.18.32, as found |
|---|---|
| install | `npm install -g opencode-ai` — a wrapper plus a native binary per platform (`opencode-linux-x64`, and a `-baseline` build for CPUs without AVX2) |
| update | `opencode upgrade` |
| login | `opencode auth login` — a provider picker, driven in the §6.6 window like Hermes's `setup` |
| logout | **not offered**: `opencode auth logout` with no provider named is a picker too, and `/harnesses/logout` runs blind |
| status | `opencode auth list` — a box saying "N credentials", then the providers whose key is in the environment. Either is "in". Neither is **"unknown", not "out"**: its free models need no sign-in, and "out" would make `POST /ask` refuse a chat that works |
| store | **one SQLite database**, `$XDG_DATA_HOME/opencode/opencode.db` — not files. `session` (id, `directory`, `title`, `parent_id` for a subagent's, `time_archived`), `message` (role etc. in JSON `data`, one row per model *step*), `part` (text / reasoning / tool — a tool part holds its own `state.status`, input and output). Times are epoch ms. `auth.json` sits beside it |
| ids | `ses_` + 26 base62, a third shape beside uuids and Hermes's |
| live | the process is `opencode` (`pane_current_command` says so) and holds the db open; its session is the `--session` argument, else the newest top-level session in its cwd started since the process |
| resume | `opencode --session <id>`; a fresh one cannot be given its id |
| screen | "esc interrupt" in the footer while working, "ctrl+p commands" always; its composer is the `┃` lines above the closing `╹▀▀▀` |

What that touched: `harnesses.py` (id shape, `opencode_rows`, `harness_of`,
`cwd_of`, `first_prompt`, `title_of` — the "New session - <iso>" placeholder
is no title — `stored`, `running`, resume, `RECIPES`, `auth_state`);
`transcript.py` (`OpencodeBuilder`: an opencode thread gets real messages
with steps from the db, and `file_state` counts parts so the per-thread
stream moves); `panes.py` (`AGENT_COMMANDS`, `classify`); `send.py` (the
composer check after a send); `sessions.py` (`_SESSION`, cwd);
`reap.py` (last message time); `activity.py` (tool aliases: `glob`, `list`,
`webfetch`, `todowrite`). Tests: `core/tests/test_harnesses_opencode.py`,
`server/tests/test_opencode_transcript.py`.

**Speech — built 24 Sep 2026.** opencode was installed on red5
(`npm i -g opencode-ai`; `opencode-ai` was already in the fleet's
`~/.npmrc` allow-scripts list). `packages/core/opencode/agent-media.js` is a
plugin, linked into `~/.config/opencode/plugins/` by `media-setup profile`
(the `opencode` extra row). It sends `chat.message` as UserPromptSubmit and
`tool.execute.before` as PreToolUse, and on `session.idle` sends Stop, then
runs `media-hook-opencode --session <id>`. opencode tells a plugin a session
went idle, not what it said, so the hook reads the reply back from the
database (`harnesses.opencode_last_reply`: every assistant text since the
last prompt), keeps the last step's id per session so a repeated idle is
spoken once, and skips a subagent's session. Tried for real: a run on the
free model was spoken (source `opencode`) and its prompt filed as a
"You:" turn. The test conftests now point `XDG_DATA_HOME` at a throwaway
dir, since opencode's real database is on red5 now.

**Not done yet, for opencode:**

- **Approval.** Its permission prompt has not been seen (the default
  permissions asked nothing in the test), so `classify` never says
  "approval" for it. The plugin API has a `permission.ask` hook, which
  could report it the way Claude's PreToolUse does.
- **Search and recaps.** The FTS index (`search._sources`) and the recap
  reader (`reap`) don't read it yet.
- **Rename.** No CLI to set a title from outside; like pi, the phone's name
  is kept by agent-media only.
- **npm allow-scripts.** `opencode-ai` has a `postinstall`; if the fleet's
  managed `~/.npmrc` allowlist (npm ≥ 11.16) gates it, add it there, or the
  install lands without its binary.
- The standard x64 build crashed once ("illegal hardware instruction", a
  bun panic) starting its TUI in a 34-column pane; the baseline build at
  110 columns ran fine. Not chased — worth knowing if a narrow phone-opened
  window dies at once.

The alternative — and the reason
opencode is interesting beyond being a fifth row — is `opencode serve`: an
HTTP + SSE API with sessions, messages and permission events. agent-media
could read and drive opencode through it instead of through tmux,
which would make it the first harness driven over an API. Worth a spike
later; it did not block the row.

## Multiple logins: a profile is a harness plus a config dir

Every harness already has exactly one place its credentials and sessions
live, and each can be moved with one variable:

| harness | where | moved by | today |
|---|---|---|---|
| claude | `~/.claude` | `CLAUDE_CONFIG_DIR` | `media-setup profile --config-dir` already writes one |
| codex | `~/.codex` | `CODEX_HOME` | |
| pi | `~/.pi/agent` | `PI_CODING_AGENT_DIR` | read by `_pi_dir()` already |
| hermes | `~/.hermes`, `profiles/<name>/` | Hermes's own profiles | **already multi-profile** (`meridian`, `venice` on red5); `hermes_stores()` reads them all |
| opencode | `~/.local/share/opencode` (db + `auth.json`) | `XDG_DATA_HOME` | moving it moves every XDG app's data too; opencode may also honour its own override — check before building profiles |

So "multiple logins" is not per-harness work. It is one concept in
agent-media:

```
profile = {harness, name, dir}          e.g. {pi, "work", ~/.config/sasonica/profiles/pi-work}
```

- **Storage:** a small `profiles.json` in agent-media's state dir. The
  default profile of each harness is implicit (its normal dir), so nothing
  changes for anyone who never adds one. Hermes profiles are discovered, not
  stored — they already exist.
- **Setup routes (§6.6):** every `/harnesses/*` call takes an optional
  `profile`. `run` / `logout` / status prepend the profile's env to the
  argv (`env CODEX_HOME=… codex login --device-auth`). `GET /harnesses`
  answers a row per profile, each with its own `auth` and `account`.
  `POST /harnesses/profiles {harness, name}` creates one (the dir is made
  under agent-media's state dir); `DELETE` removes the row and, behind a
  confirm, the dir.
- **Sessions (§6.16):** the readers take a list of dirs per harness, the way
  `hermes_stores()` already does, and each row carries `profile`. A resumed
  session is started with its profile's env; a fresh chat picks one (the
  agent chip gains a second line when a harness has more than one).
- **The app:** the Coding agents page groups rows by harness, a profile
  per row, "Add account" under each harness.

### What multiple logins means for pi specifically

`~/.pi/agent/auth.json` on red5 is empty: pi's Claude calls go through
Meridian (Claude Max via the Agent SDK), and pi has no login recipe in
`RECIPES` at all — `auth_state` answers `unknown`. So there are two
different things "multiple logins for pi" could mean:

1. **Several pi profiles**, each a `PI_CODING_AGENT_DIR` with its own
   `auth.json` (pi's own `/login` for Anthropic, OpenAI, Copilot …) and its
   own settings / model choice. This is the profile model above; it needs a
   pi login recipe first (start `pi` in the window and send `/login`), and a
   status check that reads the profile's `auth.json` for a non-empty entry.
2. **Several Claude Max accounts behind Meridian.** That is a Meridian
   concern, not pi's, and it switches the account for every client of the
   proxy at once (pi, crush, OpenClaw, the gateway lanes). If wanted, it
   should be a Meridian feature — one Meridian per account, on its own port,
   and a pi profile whose `models.json` points at that port — which the
   profile model then covers for free.

Recommendation: build (1) as part of the general profile work; leave (2)
until there is a second Max account to put behind it.

## Naming: profiles are not personas

David asked (24 Sep 2026) whether this overlaps `agent-personas` enough to
rename that repo `agent-profiles`. Proposed answer: no — they are two
layers, and the rename would make one word mean both.

- **A persona** (`~/projects/agent-personas`) is *who the agent is*: role,
  tone, voice, relationship — Sam. It is content, harness-neutral, rendered
  into a harness by `agent-persona-render`.
- **A profile** (this proposal) is *a slot a harness runs in*: a config dir,
  and the login and sessions that live in it. It has no voice of its own.

They meet cleanly: a profile's config dir is where a persona gets rendered
(its `CLAUDE.md` / `AGENTS.md` / Hermes `SOUL.md`), so a profile can
optionally name a persona — `{harness: pi, name: work, persona: sam}` — and
setup renders it there. Hermes profiles already bundle both, which is why
they look alike.

"Profile" is already busy here, so the doc and code should always say
**harness profile**: "the Sasonica profile" (`media-setup profile`) is
machine wiring, and agent-config has launch profiles. If that is still one
too many, `account` is the fallback name for this concept, at the cost of
under-describing it (the dir holds settings and sessions, not only a login).

## Order

1. **opencode row** — **built 24 Sep 2026** (above). Shows up on the Coding
   agents page with no app change once the canvas is restarted. Its speech
   plugin: built 24 Sep 2026. Left: search/recaps, approval.
2. **pi login recipe + auth status** — so pi stops answering `unknown`.
   Prerequisite for pi profiles meaning anything.
3. **Profiles** — `profiles.json`, the `profile` parameter on §6.6, per-dir
   session readers, the app's grouped rows. Claude, Codex, pi, opencode
   together; Hermes wired to its existing profiles.
4. **Diff viewer** (server route, then the screen).
5. **Tool-call approval** on the waiting card.
6. **F-Droid repo** on GitHub Pages from the release CI — **parked** until
   monetization is decided (item 3 above).
7. **Demo mode** from the mock server.
8. **`opencode serve` spike** — drive a harness over its API.

Items 1–3 are server-side and small; 4 onward are app work and join the
roadmap's queue behind what is already there.

## Open questions

- Should a profile's dir live under agent-media's state dir (so the app can
  make and delete them) or anywhere (so an existing `~/.codex-work` can be
  adopted)? Proposed: both — create under state, adopt by path.
- Does a fresh chat default to the most recently used profile of that
  harness, or the default one? Proposed: most recently used, shown on the
  chip.
- Headless sessions (§17) spawn harnesses directly; they need the profile
  env too. Check `headless` start paths when doing step 3.

[^next-rename]: Renamed since this was written: Sasonica Next is Sasonica
    (`com.sasonica.app`, 26 Sep 2026) and its speech target `next` is
    `sasonica` (27 Sep); the older Sasonica app is Sasonica ABS
    (`com.sasonica.abs`), target `abs` (was `app`). The text above keeps the
    names it was written with.
