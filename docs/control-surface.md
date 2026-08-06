# Control surfaces — a swappable front-end slot for the music channel

Status: **built** (2026-08-06). First pass implemented — the contract, the
hold layer, the empv adapter, and the two below-the-line changes (§6.5, §7).
See `packages/control-surface/README.md` for install and usage, and §10 for
what is done vs. outstanding.

## 1. The idea

Today the music channel has one control surface: the `media music …` CLI (and
the equivalent `music_*` MCP tools, which the agent drives). Both are thin —
they parse a verb and call into `sinks/`. Everything underneath them —
acquisition, playout, ducking, state — is the *pipeline*, and it is the part
that took real work to get right.

`docs/channel-architecture.md` already names the intended shape:

> players are NOT rivals, they're control surfaces landing on different
> channels

This note makes that concrete: the front-end becomes a **slot**. empv.el,
listen.el and EMMS are candidate occupants. Each is an *adapter* — a small,
isolated module that maps its own commands onto one contract. The pipeline
does not learn that any of them exist.

```
  ┌──────────────── control surface (SWAPPABLE SLOT) ────────────────┐
  │                                                                   │
  │   adapters/empv.el     adapters/listen.el     adapters/emms.el    │
  │        │                     │                     │              │
  │        └─────────────────────┼─────────────────────┘              │
  │                              ▼                                    │
  │                    am-control.el  (the contract, once)            │
  └──────────────────────────────┼────────────────────────────────────┘
                                 │  emacsclient → `media music …`
  ═══════════════════════════════╪═══════════════════════ CONTRACT LINE
                                 ▼
  ┌──────────────────── pipeline (UNCHANGED, AUTHORITATIVE) ──────────┐
  │  cli.py cmd_music  →  SinkMusicRouter  →  SinkMusicLocal (phone   │
  │                                            mpv, tcp 6601)         │
  │                                        └→ SinkMusic (Mopidy/MPD)  │
  │  route/coordinator ducks whichever backend is live                │
  │  call_guard holds/ducks during calls;  state/store.py owns truth  │
  └───────────────────────────────────────────────────────────────────┘
```

The contract line is the whole design. Above it: taste, keybindings, queue
views, completion UI. Below it: yt-dlp on the phone's residential IP, mpv over
the TCP bridge, the duck/pause policy, the state store. **The surface
orchestrates; it never owns audio.**

## 2. The contract, defined once

Seven actions. This set is deliberately small — it is the intersection of what
all three candidate front-ends can express and what the pipeline already does
today, so adding a front-end never requires touching the pipeline.

| Action | Args | Pipeline call today | Notes |
|---|---|---|---|
| `play` | `uri`, `where?`, `as?` | `media music play <uri> [--where W] [--as T]` | replaces the queue |
| `queue-add` | `uri`, `where?` | `media music play --add <uri>` | appends; real mpv playlist append on the phone backend (`music_local.play(replace=False)` → `loadfile … append-play`) |
| `toggle` | — | `media music toggle` | pause/resume in one verb; `pause`/`resume` remain available but adapters should prefer `toggle` |
| `next` | — | `media music next` | |
| `prev` | `restart?` | `media music prev [--restart-first]` | |
| `seek` | `spec` (`+90`, `-5:00`, `1:23:45`) | `media music seek <spec>` | |
| `status` | — | `media music status --json` | **the one additive change** — see §6 |

Plus one action that is not transport, and is the reason this design is worth
doing at all:

| Action | Args | Pipeline call | Notes |
|---|---|---|---|
| `hold` / `release` | — | `media-call-guard --hold` / `--release` | duck-and-hold for a Claude voice chat |

### 2.1 Why `hold` is the interesting one

`call_guard.py` already implements exactly the semantics wanted, and it is
*not* MPD-shaped — it went socket-shaped some time ago. The external-hold flag
file (`call-guard.hold`, `_DEFAULT_HOLD_FLAG_NAME`) is a documented,
supported, idempotent trigger: any external actor may set it, and the guard
debounces (engage/release windows), pauses speech, **ducks** phone-local music
rather than pausing it, and **auto-resumes on release**.

So the duck action needs no new mechanism. `hold` is `media-call-guard
--hold`; `release` is `--release`. The elisp side gets:

```elisp
(am-control-hold)     ; ducks music, holds it down, auto-resumes on release
(am-control-release)
(am-control-with-hold BODY…)   ; macro: unwind-protect around the pair
```

`am-control-with-hold` is the clean replacement for an MPD-style
`mpc pause … mpc play`: the release is in an `unwind-protect`, so a C-g or an
error during a voice chat cannot leave music stuck quiet. The guard's own
release debounce means rapid hold/release flicker (utterance boundaries) does
not thrash the volume.

Note the asymmetry, and preserve it: **hold is not pause.** Music ducks, speech
pauses. That split is the pipeline's policy (`route/policy.py`,
`route/coordinator.py`), and the control surface must not second-guess it — an
adapter that "helpfully" pauses music on hold has broken the contract.

### 2.2 What is deliberately NOT in the contract

- **Volume.** The coordinator and call-guard own music volume; a front-end
  setting it directly races the duck/unduck restore. If a surface wants a
  volume slider, that is a follow-up that goes through the coordinator, not
  around it.
- **Anything book-channel.** The book has its own transport shape (speed,
  ±30s, playlists, bookmarks) and its own front-end story. Out of scope.
- **Search / library browse.** Each front-end has its own good UI for this
  (empv's YouTube search, EMMS's browsers). A surface may search however it
  likes; the contract only receives the resulting URI.
- **Playout target selection beyond `where`.** `--where auto|phone|rooms|local`
  is passed through opaquely; adapters do not reason about Snapcast.

### 2.3 Contract invariants

1. Every action is a **fire-and-forget shell call** returning an exit code, or
   (for `status`) a JSON object on stdout. No adapter holds a connection to
   the pipeline.
2. Actions are **idempotent or safe to repeat**. `hold` twice is one hold.
3. An adapter **must not spawn a media process.** If elisp starts an mpv, the
   contract is violated — see §4.
4. The pipeline is **authoritative for state**. `status` is a read, never a
   cache to diverge from. Adapters poll it (default 2s, configurable); they do
   not maintain their own now-playing model.

## 3. Proposed file layout

Elisp is new to this repo, so it gets its own package, parallel to
`packages/visual/`:

```
packages/control-surface/
  README.md                  # install, flag, swap, remove
  lisp/
    am-control.el            # THE CONTRACT — the only file that shells out
    am-control-hold.el       # hold/release + the with-hold macro
    adapters/
      am-adapter-empv.el     # first concrete adapter
      am-adapter-listen.el   # (later)
      am-adapter-emms.el     # (later)
  tests/
    am-control-test.el       # ERT; the contract is tested with `media` stubbed
```

`am-control.el` is the only file permitted to invoke `media` / `media-call-guard`.
Adapters call `am-control-*` functions and nothing else. That single rule is
what makes an adapter reviewable in isolation and removable without trace.

### 3.1 Feature flag

One variable, in `am-control.el`:

```elisp
(defcustom am-control-adapter nil
  "Active agent-media control surface: nil, `empv', `listen', or `emms'.
nil means no front-end is loaded — the CLI and MCP tools remain the only
control surfaces, exactly as before this package existed."
  :type '(choice (const :tag "None (CLI/MCP only)" nil)
                 (const empv) (const listen) (const emms)))
```

`am-control-setup` loads exactly the one adapter named and installs its
keymap. Nothing is autoloaded; an unselected adapter's file is never read.
Setting the flag to nil and restarting Emacs returns the system to the current
state by construction, because no adapter ever mutated anything outside Emacs.

Environment override for headless/agent contexts:
`MEDIA_CONTROL_SURFACE=empv|listen|emms|none`.

## 4. The adapter pattern — and the one hard problem

empv.el, listen.el and EMMS are not UIs. They are **players**: each expects to
own an mpv (or MPD) process and drive it. Pointing them at agent-media means
persuading each to be a controller only. That is the real work in every
adapter, and it is why each adapter is its own file rather than a config
table.

Two modes are available. Recommended default is **A**.

### Mode A — command interception (recommended)

The adapter does not let the front-end start a backend at all. It binds the
front-end's *user-facing commands* — the keymap entries, the transient menu,
the completion-driven pickers — to `am-control-*` calls, and uses the
front-end only for the parts that are genuinely UI: candidate selection,
queue display, keybinding ergonomics.

- **Pro:** every call goes through the contract, so ducking, call-guard,
  content-type policy, bookmarks and the state store all work unmodified. The
  phone path is untouched.
- **Pro:** removal is deleting one file.
- **Con:** the front-end's own queue view can drift from the pipeline's real
  queue. Mitigation: the adapter refreshes its display from
  `am-control-status`, and treats its local list as a *view*, not a source.
- **Con:** front-end features that assume a live mpv handle (waveform
  scrubbing, per-track EQ, gapless-specific behaviour) will not work. Accepted
  — those are output concerns, and output is the pipeline's.

### Mode B — direct IPC attach (documented, not recommended)

empv talks mpv JSON-IPC over `empv-socket-file`. The phone's mpv is reachable
at `MEDIA_MUSIC_LOCAL_ENDPOINT=tcp://…:6601`, so a local socket→TCP relay
would let empv drive the *actual* player, with real seek bars and live
property observation.

Rejected as the default because it goes **around** the contract: the
coordinator would not know a track changed, `state/store.py` would not see it,
and — most sharply — empv's own volume writes would fight `call_guard`'s duck
and its automatic restore. It also bypasses `--where auto` routing entirely.
Documented here so the option is a decision rather than an oversight; if it is
ever wanted, it should be scoped to *read-only* property observation for
display, with all writes still going through Mode A.

### Mode B is also the honest answer for the ~0.8s question

The phone bridge round-trip from red5 is ~0.8s per call. A Mode-A adapter that
polls `status` at 2s and batches its reads (the pipeline already batches via
`_mpv_ipc.get_properties`) is fine. An adapter that issues a shell call per
keystroke will feel laggy. Adapters should therefore be **optimistic in the
UI** (show the intent immediately, reconcile on the next status poll) rather
than blocking on the call — that is a per-adapter concern and is the second
reason adapters are not a config table.

## 5. empv adapter — concrete surface

First adapter, because empv's separation between "pick something" and "play
it" is the cleanest of the three, and its YouTube-search UI matches how the
music channel is actually fed (yt-dlp URIs).

### 5.1 emacsclient command surface

Every entry below is callable non-interactively, so the whole surface is also
scriptable from red5 — which means the agent, tmux keybindings, and a phone
shortcut can all drive the same commands the human uses:

```sh
emacsclient -e '(am-control-play "yt:https://youtu.be/…")'
emacsclient -e '(am-control-queue-add "yt:https://youtu.be/…")'
emacsclient -e '(am-control-toggle)'
emacsclient -e '(am-control-next)'
emacsclient -e '(am-control-prev)'
emacsclient -e '(am-control-seek "+90")'
emacsclient -e '(am-control-status)'          ; → plist
emacsclient -e '(am-control-hold)'
emacsclient -e '(am-control-release)'
```

Adapter-level (empv-specific, interactive):

```
M-x am-empv-search        ; empv's YouTube completion UI → am-control-play
M-x am-empv-search-queue  ; same picker → am-control-queue-add
M-x am-empv-queue         ; empv queue buffer, populated from am-control-status
```

Proposed keymap under a prefix (default `C-c m`, fully rebindable):

| Key | Command | Contract action |
|---|---|---|
| `C-c m SPC` | `am-control-toggle` | toggle |
| `C-c m n` / `p` | `am-control-next` / `-prev` | next / prev |
| `C-c m s` | `am-empv-search` | play |
| `C-c m a` | `am-empv-search-queue` | queue-add |
| `C-c m f` / `b` | seek `+30` / `-30` | seek |
| `C-c m h` | `am-control-hold` (toggles) | hold / release |
| `C-c m q` | `am-empv-queue` | status |

### 5.2 How empv is prevented from starting mpv

The adapter sets `empv-mpv-binary` to nil-equivalent and never calls
`empv-start` / `empv--send-command`; it binds only empv's *selection*
machinery (its completing-read wrappers and YouTube search) and routes the
chosen candidate to `am-control-play`. If a future empv version makes that
separation harder, the fallback is to depend on empv purely for its search
functions and provide our own keymap — a change confined to
`am-adapter-empv.el`.

This is the checkable invariant for review: **after loading the adapter, no
mpv/mpd process exists on the Emacs host that the adapter created.**

## 6. Where the surface runs — dispatch is per-action, not per-host

Decided 2026-08-06, replacing the earlier "red5 first, phone later" framing.
The phone is not a later port of a red5 design; it is where most of the
contract belongs, because it is where everything else already is.

### 6.1 Ground truth

Verified on p8ar, 2026-08-06:

- **Termux already has `emacs` and `emacsclient`**, in the same sandbox as
  the phone's `media` binary and the music mpv. The Android Emacs *app* is
  the wrong tool: a separate app sandbox cannot invoke Termux's `media`
  without an ssh-to-localhost hop, which discards the only advantage the
  phone has. Use Termux Emacs; ignore the app.
- The phone's `media` is **not** a stub — `music`, `book`, `focus`,
  `channels`, `speech-hold` are all present.
- `call_guard` runs **on the phone** (it polls `termux-notification-list`),
  so the hold trigger's natural home is the phone too.
- The phone keeps its **own** `state.db`
  (`~/.local/state/agent-media/state.db`), separate from red5's.

### 6.2 The state-store question, and why it is narrower than it looks

Two state stores means a phone-side action that writes a durable row is
invisible to red5's popup, canvas and history. That is the real cost of
moving control to the phone.

It is bounded, though, because **`SinkMusicRouter` probes the live player
rather than trusting the DB** — `now_playing_uri()` / `position()` ask mpv
directly. So a phone-side `toggle`, `next` or `seek` is picked up by red5's
next poll with no synchronisation at all. Only actions that write durable
rows — bookmarks, history, `resume_pos` — actually need to originate on the
host that owns the store.

That draws the line cleanly, and it does not fall on a host boundary.

### 6.3 The dispatch table

`am-control.el` routes each action to a command prefix. Two prefixes, chosen
per action rather than per host:

| Action | Dispatch | Why |
|---|---|---|
| `toggle`, `next`, `prev`, `seek` | **local** | touches mpv only; live-probed by red5, no durable write |
| `hold`, `release` | **local** | call_guard is phone-side; this is the latency-critical one |
| `status` | **local** | a read; the live player is the truth |
| `play`, `queue-add` | **red5** | needs library, content-type policy, bookmarks, history — a round trip regardless |

```elisp
(defcustom am-control-remote-command '("ssh" "red5" "media")
  "Command prefix for actions that must originate where the state store is.")
(defcustom am-control-local-command '("media")
  "Command prefix for actions that only touch the local player.")
(defcustom am-control-local-actions '(toggle next prev seek hold release status)
  "Actions dispatched locally. Set to nil to force everything remote —
which reproduces a pure red5 surface exactly.")
```

On red5, `am-control-local-command` is *also* `("media")` and everything
still works — the local actions simply cross the bridge as they do today.
The same elisp runs unmodified on both hosts; only the two defcustoms differ.
Setting `am-control-local-actions` to nil is the escape hatch if the split
ever proves troublesome.

### 6.4 What this buys, and the honest caveat

The win is not a nicer picker — it is **latency on the duck path**. A
phone-local `hold` removes the ~0.8s bridge round-trip, which is the same
saving as the standing barge-in TODO (`git log`: "local barge-in trigger to
remove the ~1s relay hop on duck"). The control surface and the barge-in
trigger are the same problem wearing two hats, and this design lets one
solve both.

Caveat worth stating plainly: **on the phone, Emacs may not be the right
interactive UI.** The canvas popup already provides touch transport there,
and empv's completion-driven picker is a keyboard idiom. So the phone slice
should be scoped as a *latency and trigger* surface (hold, toggle, next,
seek), with browsing and queueing staying on red5 where the keyboard is.

### 6.5 Prerequisite — the phone's missing music config

**This is currently broken and must be fixed before phone-local dispatch
works at all.** The phone's `~/.config/agent-media.env` contains no music
configuration — no `MEDIA_MUSIC_LOCAL_ENDPOINT`. So `_local_configured()`
is false there, `SinkMusicRouter` degenerates to Mopidy, and a phone-local
`media music toggle` would reach across the tailnet to red5's Mopidy instead
of the mpv running three inches away.

Fix is a config addition on the phone pointing the music-local endpoint at
its own mpv (loopback, not the tailnet address red5 uses). No code change —
but it is real, currently-missing work, and it is the first task if this
design is accepted.

### 6.6 Spacemacs on the phone: no

The adapter has no spacemacs dependency. On Termux it wants a minimal
`init.el` that loads `am-control.el` and the adapter, nothing else.
Spacemacs on Termux is a slow-start, high-maintenance liability for what is
essentially a remote control. The full config stays on red5.

## 7. The one change below the contract line

`media music status --json` does not exist. Today's `status` emits a
human-formatted status-bar line (`_music_status_line`, width/bar flags) and
`now-status` a display label. Adapters need structured data.

Proposed: add a `--json` flag to `media music status` that prints the fields
the status line is already computed from (`uri`, `title`, `chapter`, `pos_ms`,
`dur_ms`, `paused`, `backend` (`phone`/`mopidy`), `volume`, `held`). Purely
additive: a new flag, existing output paths untouched, no behaviour change
when the flag is absent. `channels_status` in `mcp_server.py` already
assembles most of this, so it is largely a re-emit rather than new logic.

Nothing else below the line changes. Specifically **unchanged**:
`sinks/music_local.py`, `sinks/music.py`, `sinks/music_router.py`,
`route/coordinator.py`, `route/policy.py`, `call_guard.py`,
`state/store.py`, and the phone-side `~/bin/play-local` helper.

## 8. Independence guarantee (supersedes "reversibility")

**Revised 2026-08-06.** The original spec asked for reversibility: that
removing a front-end return the system to exactly its prior state. That was
the wrong requirement — it constrained *how* things get built in order to buy
a property nobody actually needs. Irreversible is fine.

What matters is narrower and stronger:

> **The agent-media popup — and the CLI, MCP tools and speech coordinator
> behind it — must keep working with no reliance on a third-party control
> surface.** Emacs uninstalled, dead, or wedged must be indistinguishable from
> Emacs absent.

So the constraint is on the **direction of the dependency**, not on undo:

```
   control surface  ──calls──▶  media CLI / call-guard  ──▶  players
   (empv/listen/EMMS)
                    ◀──never────
```

### 8.1 What is actually forbidden

1. **Nothing in `packages/core` may reference a front-end** — no import, no
   `emacsclient` subprocess, no adapter by name. Enforced by
   `packages/core/tests/test_control_surface_independence.py`, which greps
   the package for `emacsclient` / `am-control` / `am-adapter` / `empv` and
   fails on a hit. A test, not a promise: a promise cannot fail CI.
2. **The popup's redraw path may never wait on Emacs.** It redraws on every
   keypress; an Emacs round-trip there would be felt immediately. Covered by
   its own case in the same test.
3. **No capability may become reachable only through the surface.** In
   particular `media-call-guard --hold` stays the primary duck trigger, with
   `am-control-hold` as one caller among several (Tasker, the CLI, the agent).
   The day ducking works *only* from Emacs, the guarantee is broken.

### 8.2 What this now permits (and previously did not)

Relaxing to independence frees several things the reversibility spec ruled
out. None are required; they are simply no longer disqualified:

- **Adapters may mutate their own package's globals** — e.g. setting
  `empv-mpv-binary` to stop empv spawning mpv. Previously refused as a
  side effect on another package; now just a local implementation choice.
  (The current adapter still doesn't need to, since it never calls empv's
  playback functions.)
- **Adapters may keep persistent state** — their own queues, playlists,
  history, ratings. The pipeline does not read it, so it cannot be corrupted
  by it.
- **Teardown and hot-swap are convenience, not contract.**
  `am-adapter-*-teardown` and `am-control-setup`'s unload step stay because
  they are genuinely useful for iterating, not because anything depends on
  them. Restarting Emacs is an acceptable swap mechanism.
- **Mode B (direct IPC attach, §4) is no longer disqualified on these
  grounds.** Its real objection stands on its own merits — a front-end
  writing mpv volume directly races `call_guard`'s duck and its automatic
  restore — but that is a correctness argument about the duck path, not a
  reversibility one, and it applies only to volume writes.
- **Volume could move into the surface**, if the race above is solved by
  routing through the coordinator rather than around it.

### 8.3 What is unchanged

The built code already satisfies the new spec — it was written to the
stricter one, and independence is implied by it. Verified: core contains zero
references to the elisp layer. In particular `am-control-adapter` = nil
remains a real, fully-functional state, because the CLI and MCP tools were
never demoted to second-class citizens by any of this.

### 8.4 How to swap

```elisp
(setq am-control-adapter 'listen)   ; was 'empv
(am-control-setup)                  ; unloads the old keymap, loads the new
```

The previously active adapter's file is not reloaded; its keymap is removed.
Playback continues uninterrupted through the swap — the pipeline never knew.

### 8.5 How to remove entirely

```elisp
(setq am-control-adapter nil)
```

…and, for a full uninstall, delete `packages/control-surface/`. Optionally
revert the `--json` flag from §6, though leaving it costs nothing — it is
additive and useful to any consumer (a tmux status line, the popup, the
canvas).

No pipeline file needs reverting, because none was changed.

## 9. Open questions for review

1. **Queue semantics on the phone backend.** `queue-add` appends to the phone
   mpv's playlist, but there is no `queue-list` in the contract — reading the
   phone's playlist is another bridge round-trip. Should `status --json`
   include the playlist (costlier read, richer UI), or should the queue view
   be append-only-optimistic?
2. **Is empv the right first adapter**, or would listen.el be faster to land
   given it is already the "preferred/default" surface named in
   `channel-architecture.md`? (Counterpoint: that note also records listen.el
   is not installed on red5 as of 2026-08-04, so empv may be less setup.)
3. **Do the two state stores ever need reconciling?** §6.2 argues they mostly
   do not, because the router live-probes the player. But bookmarks taken on
   the phone would land in the phone's `state.db` and never reach red5's
   history. Options: keep bookmarks red5-dispatched (current proposal), or
   treat the split stores as a separate problem worth solving on its own
   terms. The latter is likely the honest answer eventually.
4. **Should the `hold` action be exposed to the agent** as an MCP tool too, so
   a Claude voice chat can hold music without going through the phone's
   call-guard poller? That would be genuinely new capability rather than a
   re-wrapping, and belongs in a separate decision. Note this overlaps
   directly with the standing barge-in TODO — worth deciding together.

## 10. Status — built 2026-08-06

Delivered, first pass. Uncommitted, pending review.

### Below the contract line (two changes, both additive)

- **`media music status --json`** (§7) — new flag on `cli.py`'s music status
  action, emitting `backend/uri/title/chapter/pos_ms/dur_ms/paused/speed/
  volume/held`. Follows the same live-backend rule as the popup, so it cannot
  disagree with what is on screen. The formatted status line is byte-for-byte
  unchanged when the flag is absent. `_phone_music_props` gained `volume` and
  `path` to the batch it already issues — same single round-trip, no new cost.
  `held` reads call-guard's flag file; it never writes it.
- **The phone's missing music config** (§6.5) — **fixed.** `p8ar`'s
  `~/.config/agent-media.env` now sets
  `MEDIA_MUSIC_LOCAL_ENDPOINT=…/.local/state/agent-media/mpv-music.sock`. Note
  this is the phone's own **Unix socket**, not the `tcp://…:6601` bridge red5
  uses — phone-local control now reaches the mpv on the same device with zero
  network hops. Verified: `configured() → True`, socket reachable,
  `idle-active`/`volume` read back. A `.bak-precontrolsurface` copy of the
  env file sits beside it.

- **`tests/test_control_surface_independence.py`** (§8) — 6 tests enforcing
  the one-way dependency: core may not reference `emacsclient` / `am-control`
  / `am-adapter` / `empv`, and the popup's redraw path may not mention Emacs
  at all. Added when the spec moved from reversibility to independence.

Nothing else below the line changed. Still untouched: `sinks/music_local.py`,
`sinks/music.py`, `sinks/music_router.py`, `route/coordinator.py`,
`route/policy.py`, `call_guard.py`, `state/store.py`, `~/bin/play-local`.
Full Python suite: **584 pass**.

### Above the line

`packages/control-surface/` — `am-control.el` (contract + per-action
dispatch), `am-control-hold.el` (hold/release + the `with-hold` macro),
`lisp/adapters/am-adapter-empv.el`, and 13 ERT tests (**13 pass**). Byte
compiles clean. Verified end-to-end on red5: elisp → `media music status
--json` → plist; adapter setup installs the keymap, the status buffer
renders, and teardown leaves no binding behind.

The empv adapter was written against a **real empv install** (MELPA build
2026-08-02) rather than a guessed API, using `empv--youtube-search`,
`empv--completing-read-object`, `empv--format-yt-item` and
`empv--youtube-item-extract-link`. empv's playback functions are never
called.

### Direct mpv fast path — added 2026-08-06

Deploying the phone daemon revealed the next bottleneck by measurement: with
the tailnet hop gone, `media`'s ~650ms of Python startup *was* the latency.
`lisp/am-control-mpv.el` removes it where it is safe.

Measured on p8ar:

| | via `media` | direct |
|---|---|---|
| `status` | ~650 ms | ~7 ms (3-property batch over the socket) |
| `hold` + `release` | ~1300 ms | **0.08 ms** |
| same, called via `emacsclient` from a shell | — | ~45–110 ms (emacsclient startup is now the floor) |

So in-daemon the cost is essentially gone; a *shell* caller now pays for
spawning `emacsclient`, not for the work. For barge-in that means the trigger
should ideally already be resident, not a shell hook.

The boundary of what goes direct is the important part, and it is narrow:
transport and status yes (cli.py implements them as a bare backend call with
no state write, and agent-media live-probes mpv, so red5 still sees them);
**volume never**, because call-guard owns it during a duck and restores it
after — the standing objection to §4's Mode B, unchanged. Ducking goes through
call-guard's flag file, which is its documented external trigger, so this uses
the supported interface rather than reaching around it. Restricted to Unix
sockets; a `tcp://` endpoint keeps the CLI path.

### Outstanding

1. **Deploy to the phone** — `git pull` in `~/projects/agent-media` on p8ar,
   since `status --json` is not in the phone's checkout yet. Until then, only
   red5 can serve the structured read.
2. **empv search is unexercised** — `empv-invidious-instance` is nil and empv
   is not installed in the real Emacs (only in a scratch dir for API
   verification). `am-empv-play-url` works without either.
3. **A live play/duck test has not been run.** Doing so would start audio on
   the phone; left for you to trigger.
4. **listen.el and EMMS adapters** — not written. Same shape: one file under
   `lisp/adapters/` with `-setup`/`-teardown`, calling only `am-control-*`.
5. The §9 open questions still stand, particularly (3) on the two state
   stores.
