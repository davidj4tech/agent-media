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
lisp/adapters/am-adapter-empv.el   empv.el as a front-end
tests/am-control-test.el        ERT; `media` is stubbed, no player is touched
```

Adapters call `am-control-*` and nothing else. That single rule is what makes
an adapter reviewable in isolation and removable without a trace.

## Install

```elisp
(add-to-list 'load-path "~/projects/agent-media/packages/control-surface/lisp")
(add-to-list 'load-path "~/projects/agent-media/packages/control-surface/lisp/adapters")
(require 'am-control)
(require 'am-control-hold)

(setq am-control-adapter 'empv)
(am-control-setup)
```

### Per-host configuration

Dispatch is **per-action, not per-host**. The same files run unmodified on
both machines; only these prefixes differ.

On **red5** (everything is local, `media-call-guard` lives in the venv):

```elisp
(setq am-control-local-command  '("media")
      am-control-remote-command '("media")
      am-control-local-hold-command
        '("/home/ryer/projects/agent-media/.venv/bin/media-call-guard")
      am-control-remote-hold-command
        '("/home/ryer/projects/agent-media/.venv/bin/media-call-guard"))
```

On the **phone** (Termux — `media` and `media-call-guard` are both on PATH;
note this is Termux's Emacs, *not* the Android Emacs app, which lives in a
separate sandbox and cannot invoke `media`):

```elisp
(setq am-control-local-command  '("media")
      am-control-remote-command '("ssh" "red5" "media")
      am-control-local-hold-command '("media-call-guard")
      am-control-remote-hold-command
        '("ssh" "red5" "/home/ryer/projects/agent-media/.venv/bin/media-call-guard"))
```

Which actions go where is `am-control-local-actions`, default
`(toggle next prev seek hold release status)` — the actions that only touch
the local player. `play` and `queue-add` go remote because they need the
library, content-type policy and history. Set it to nil to force everything
remote, which reproduces a pure red5 surface exactly.

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
