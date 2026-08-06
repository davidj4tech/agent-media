# control-surface — a swappable front-end for the music channel

A small Emacs layer that drives agent-media's music channel. The design note
is [`docs/control-surface.md`](../../docs/control-surface.md); this file is
how to install, use, swap and remove it.

**The surface orchestrates; it never owns audio.** No mpv is started here, no
MPD is spoken to, no persistent state is written. Acquisition on the phone's
residential IP, playout, the duck/pause policy and the state store are all
untouched below the contract line.

## Layout

```
lisp/am-control.el              THE CONTRACT — the only file that shells out
lisp/am-control-hold.el         duck-and-hold for a voice chat
lisp/am-control-mpv.el          the direct JSON-IPC fast path
lisp/am-control-site.el         which end of each action THIS host is; the
                                one entry point every front door calls
lisp/adapters/am-adapter-empv.el   empv.el as a front-end
spacemacs/am-control/           Spacemacs layer — a thin caller of the above
phone/am-control-init.el        `emacs -Q' init for the phone's control daemon
services/am-control-emacs/      runit service that runs it
tests/am-control-test.el        ERT; `media` is stubbed, no player is touched
```

Adapters call `am-control-*` and nothing else. That single rule is what makes
an adapter reviewable in isolation and removable without a trace.

The three front doors — a vanilla init, the Spacemacs layer, the phone daemon
— all funnel through `am-control-site-setup`, so none of them can drift from
the others. Editors are taste; where a daemon runs is not.

## Install

### Vanilla Emacs

One `load` and one call. No package manager, no `load-path` lines: the file
puts `lisp/` and `lisp/adapters/` on the path itself, resolved from
`load-file-name`, so this works from any clone on any host.

```elisp
(load "~/projects/agent-media/packages/control-surface/lisp/am-control-site.el")
(am-control-site-setup 'empv)   ; or nil — contract only, scriptable, no UI
```

`empv` is a soft dependency, needed only for the picker (`s` / `a`). Every
transport binding works without it; install it from MELPA if you want search.

### Spacemacs

The layer ships from this repo — add the directory to the layer path and the
layer to the list:

```elisp
dotspacemacs-configuration-layer-path
  '("~/.spacemacs.d/private/"
    "~/projects/agent-media/packages/control-surface/spacemacs/")

dotspacemacs-configuration-layers '(… am-control)
```

That installs `empv` from MELPA and calls the same `am-control-site-setup`.
Leader keys are opt-in, because which prefix is free is a property of *your*
config: `(am-control/bind-leader "am")` puts everything under Spacemacs'
stock-but-empty `SPC a m` music prefix. `C-c m` is bound either way.

### evil

Mostly nothing to do: `C-c m` passes through evil untouched, and a Spacemacs
leader binding *is* a normal-state binding.

The exception is the status buffer, and it was a bug rather than a missing
nicety. `*am-control: music*` advertises `g refresh SPC toggle n/p next/prev
h hold q bury` in its own footer, and under evil every one of those was
shadowed — `n` search-next, `p` paste, `SPC` forward-char, `h` backward-char,
`q` record-macro, `g` a prefix. `am-control-evil.el` re-binds the mode map
into normal and motion states, reading the keys back out of the adapter's map
so the two cannot drift, and leaving `j`/`k` and the rest of evil's motions
intact. `am-control-site-setup` wires this up whenever evil is present, or
when it later loads.

For evil *without* Spacemacs there is an opt-in global prefix:

```elisp
(setq am-control-evil-prefix "SPC m")   ; before am-control-site-setup
```

It reports what it displaces (`SPC` is `evil-forward-char` in motion state, and
a key holding a command cannot also be a prefix), because silently eating a vim
motion is not something to discover a week later.

### The phone

Nothing to do — `phone/am-control-init.el` calls the same setup, and
`services/am-control-emacs` runs it as a named `emacs -Q` daemon. See
[§ The phone daemon](#the-phone-daemon).

### Per-host configuration

Dispatch is **per-action, not per-host**: the same files run unmodified
everywhere, and `am-control-site-configure` decides which end of each action
this host is. It is the one place that knows, so a change to where a daemon
runs is a one-line fix in the repo rather than an edit to every init file that
happens to drive the surface.

| Host | `toggle next prev seek status` | `play` `queue-add` | `hold` `release` |
|---|---|---|---|
| phone (Termux) | local — Unix socket, so the direct fast path engages | ssh hub | **local** |
| hub (`media` on PATH) | local | local | **ssh phone** |
| anywhere else | ssh hub | ssh hub | **ssh phone** |

`play` and `queue-add` go to the hub because they need the library,
content-type policy and history. Everything else is a bare backend call.

**Hold does not follow `media`.** `media-call-guard --hold` does nothing but
touch a flag file, and the process that *polls* that flag — `call_guard` — runs
on the phone (runit service `call-guard`), where mpv and the call notifications
are. A hold performed anywhere else lands in a state dir no daemon watches and
silently does nothing. So off-phone, `hold` and `release` are absent from
`am-control-local-actions`: that routes them over ssh **and** disables the
direct `write-region`, which is gated on exactly that (see
`am-control-hold--direct-p`), so the flag gets touched on the host that reads
it. Note this is `am-control-remote-hold-command`, not
`am-control-remote-command` — the two remotes are different hosts, which is the
whole reason hold has a prefix pair of its own.

Overriding any of it is a plain `setq` after setup: `am-control-site-hub-host`,
`am-control-site-phone-host`, `am-control-site-ssh`, or the
`am-control-{local,remote}-*` variables directly.

Termux note: this is Termux's Emacs, *not* the Android Emacs app, which lives
in a separate sandbox and cannot invoke `media`.

## The contract

| Function | Does |
|---|---|
| `am-control-play` URI &optional WHERE AS | play, replacing the queue |
| `am-control-queue-add` URI | append without clearing |
| `am-control-toggle` | pause/resume |
| `am-control-next` / `am-control-prev` | skip |
| `am-control-seek` SPEC | `"+90"`, `"-5:00"`, `"1:23:45"` |
| `am-control-seek-forward` / `-backward` | ±30s |
| `am-control-status` | plist: `:backend :uri :title :chapter :pos-ms :dur-ms :paused :speed :volume :held` |
| `am-control-now` | message what's playing |
| `am-control-hold` / `-release` / `-hold-toggle` | duck for a voice chat |
| `am-control-with-hold` BODY | macro; releases in `unwind-protect` |

All actions are fire-and-forget and asynchronous — a remote call crosses the
network, and blocking Emacs on it would make the surface feel worse than the
CLI it replaces. Adapters should be **optimistic**: show the intent
immediately, reconcile on the next `am-control-status` poll. `status` is the
one synchronous call.

### The direct fast path

Once the network hop was gone, `media`'s ~650ms Python startup was the whole
cost. `am-control-mpv.el` removes it by speaking mpv's JSON-IPC straight from
elisp, and `am-control-hold` touches call-guard's flag file itself.

| | via `media` | direct |
|---|---|---|
| `status` | ~650 ms | **~2 ms** |
| `toggle` / `seek` | ~650 ms | **~1 ms** |
| `hold` | ~650 ms | **~0 ms** (one `write-region`) |

What goes direct is deliberately narrow:

- **Yes** — `toggle`, `next`, `prev`, `seek` (simple ±seconds), `status`.
  `media music <verb>` implements these as a bare backend call with no state
  write, so going direct is equivalent; agent-media live-probes mpv, so red5
  still sees them on its next poll.
- **No** — anything touching **volume**. call-guard owns music volume during a
  duck and restores it after; a front-end writing volume races that restore.
  Hold therefore goes through the flag file, which is call-guard's documented
  external trigger — using the supported interface, not reaching around it.
- **No** — `play`, `queue-add`, `stop`, `prev --restart-first`, timecode seeks.
  These need the library, policy, history, or real parsing.

It only engages for a **Unix-socket** endpoint (a `tcp://` one is red5's
bridge to the phone — going direct there would still cross the tailnet). Every
case degrades to the CLI: no endpoint, unreachable socket, idle player, action
routed remote, or `am-control-prefer-direct` nil.

### Scriptable from anywhere

Every entry point is callable non-interactively, so the agent, a tmux binding
and a phone shortcut all drive the same commands you do:

```sh
emacsclient -e '(am-control-toggle)'
emacsclient -e '(am-control-play "yt:https://youtu.be/…")'
emacsclient -e '(am-control-status)'
emacsclient -e '(am-control-hold)'
```

## Hold is not pause

Music **ducks**, speech **pauses**. That split is the pipeline's policy
(`route/policy.py`, `route/coordinator.py`), and `call_guard` already
implements it — `am-control-hold` just touches the same external-hold flag
that `media-call-guard --hold` does. An adapter that "helpfully" pauses music
on hold has broken the contract.

`am-control-with-hold` is the clean replacement for an MPD-style
`mpc pause … mpc play`: the release sits in an `unwind-protect`, so a `C-g`
or an error during a voice chat cannot leave music stuck quiet.

**Holds nest, so they must balance.** `am-control-hold` bumps a depth counter
and only the release that returns it to zero actually un-ducks — that is what
makes a nested `with-hold` safe. The flip side: calling `hold` more often than
`release` strands a hold, and nothing will un-duck until the counter clears.
Prefer `am-control-with-hold` over bare pairs. If music is quiet and you
suspect a stranded hold, `am-control-now` shows `[held]`, and:

```elisp
(am-control-hold-reset)   ; force released, zero the counter
```

(I stranded one this way while benchmarking. The counter behaved correctly;
the caller did not.)

## empv adapter

| Key (under `C-c m`) | Command |
|---|---|
| `SPC` | toggle |
| `n` / `p` | next / prev |
| `s` | `am-empv-search` — empv's YouTube picker → play |
| `a` | `am-empv-search-queue` — same picker → queue |
| `u` | `am-empv-play-url` — paste a URL (needs no empv config) |
| `f` / `b` | seek ±30s |
| `h` | hold toggle |
| `q` | status buffer |
| `?` | what's playing |

empv is used for the part that is genuinely UI — its YouTube search and
completing-read machinery — and the *selected candidate* is routed to the
contract. empv's own playback functions (`empv-play`, `empv-play-or-enqueue`,
`empv-start`) are never called, so **no mpv is ever created here**. Verify
with `M-x am-adapter-empv-check`.

The adapter does not set `empv-mpv-binary` to neuter empv — it doesn't need
to, since it never calls empv's playback functions, and leaving empv's globals
alone means `M-x empv-play` still behaves like empv for anyone who wants it.
That's a local choice, not a rule: the spec is independence, not
reversibility.

**Prerequisite:** empv's search needs `empv-invidious-instance` set (nil by
default). Without it, `am-empv-play-url` still works.

## The one hard rule: the dependency points one way

```
control surface  ──calls──▶  media CLI / call-guard  ──▶  players
(empv/listen/EMMS)
                 ◀──never────
```

The popup, CLI, MCP tools and speech coordinator must keep working with Emacs
uninstalled, dead or wedged. So nothing in `packages/core` may reference a
front-end, and no capability may become reachable *only* through this layer —
`media-call-guard --hold` stays the primary duck trigger, with
`am-control-hold` as one caller among several.

Enforced by `packages/core/tests/test_control_surface_independence.py`, which
fails if core ever mentions `emacsclient` / `am-control` / `am-adapter` /
`empv`. See [docs §8](../../docs/control-surface.md) for what this permits
that a stricter reversibility spec did not.

## Swap

```elisp
(setq am-control-adapter 'listen)   ; was 'empv
(am-control-setup)                  ; tears the old one down, loads the new
```

Playback continues uninterrupted through a swap — the pipeline never knew.
Teardown is a convenience for iterating, not a guarantee anything relies on;
restarting Emacs works just as well. `am-control-adapter` = nil is a real,
fully-functional state: the CLI and MCP tools were never demoted.

## Tests

```sh
emacs -Q --batch -L lisp -l tests/am-control-test.el -f ert-run-tests-batch-and-exit
```

13 tests, `media` stubbed — safe to run while music is actually playing.
(The Python suite learned that lesson the hard way; see
`packages/core/tests/conftest.py`.)

## Status

`am-control.el`, `am-control-hold.el` and the empv adapter are built and
tested. The listen.el and EMMS adapters are not written yet — they are the
same shape: a file under `lisp/adapters/` exposing `-setup` and `-teardown`,
calling only `am-control-*`.
