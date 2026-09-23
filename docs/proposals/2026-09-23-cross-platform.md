# agent-media on macOS and Windows: the boundary is tmux, not fcntl

Status: proposal, nothing built.
Date: 2026-09-23

David: *"It would be nice if agent-media was cross-platform too."*

## Recommendation in one line

The thing that decides this is **tmux**, not the posix calls the earlier note
named — and the answer to tmux is **herdr**, which we already speak and which
already carries Windows shells. macOS is then a week of mechanical work;
Windows becomes arguable rather than out of the question.

## The earlier note named the wrong blocker

[cross-platform-notes.md](../cross-platform-notes.md) (20 Sep) put the cost on
`fcntl` (~35 refs) and posix (~86). Measured again today, that is not where
the weight sits:

| | refs | where |
|---|---|---|
| `fcntl` | 29 | **8 of them in one file**, `core/_lock.py`; 7 in `intake/submit.py`; the rest 2–3 each |
| `tmux` | 94 in core alone | `cli.py` 120, `intake/submit.py` 76, `hook_claude_code.py` 35, `layout.py` 33, `conversation.py` 28, and 16 more modules |
| service units | 8 files, two supervisors | `systemd --user` and Termux runit |
| `sys.platform` / `platform.system()` | **0** | there is not one platform check in the codebase |

`fcntl` is a lock. One file, one concept, and Windows has `msvcrt.locking`;
an hour's work behind an interface we would want anyway. It looked like the
blocker because it is easy to grep.

tmux is not a dependency, it is **the model**. A session *is* a pane: we type
into it with `send-keys`, read it with `capture-pane`, find it by
`TMUX_PANE` in `/proc/<pid>/environ`, and lay panes out with `layout.py`. A
platform without tmux does not need a shim; it needs another answer to "where
does the agent live and how do I reach it".

## What already exists (most of the argument)

Three seams are already cut, by work done for other reasons:

- **Addresses carry their multiplexer.** `server/panes.py` already dispatches
  three verbs — read, send-text, label — on an address that is either a bare
  tmux pane id (`%562`, kept bare so old history rows still resolve) or
  `herdr:w6:p1`. A second multiplexer is already in the tree and working.
  This is the seam a third one would use.
- **The supervisor is already chosen at runtime.** `setup.py
  _service_backend()` picks runit or `systemd --user` per host, and
  `media-setup profile` wires a machine end to end. launchd is a third
  backend in a place that already expects backends.
- **Hosts already have roles.** `~/.config/agent-media/config.toml` decides
  what runs where (observe / render / origin). A Mac that is a *render* host
  and not an *origin* needs a fraction of the daemon.

## herdr is the answer to the tmux problem (David's, 23 Sep)

The hard part above was "a platform without tmux needs another answer to
where the agent lives". We already have one, running, in this tree:

- `server/panes.py` dispatches read / send-text / label on `herdr:w6:p1`
  exactly as it does on `%562`, and discovery reads `HERDR_PANE_ID` from the
  process environment beside `TMUX_PANE`. **The abstraction is not a
  proposal; it is in production.**
- herdr is a single static binary (0.9.0 here, `~/.local/bin/herdr`) that
  calls itself a terminal workspace manager for AI coding agents, with
  `machine` and `--remote` subcommands — multi-machine is its premise, not
  an add-on.
- Its binary carries Windows shell handling (`windows_cmd`,
  `windows_powershell`), which suggests upstream builds for platforms we do
  not run. **Unverified** — the one question to put to the project before
  any of this is planned around it: are there macOS and Windows builds, and
  do `pane read` / `pane send-text` behave the same on them?

If the answer is yes, the port stops being a rewrite. The remaining Linux
assumptions are the small, nameable ones — the lock, `/proc/<pid>/environ`,
the service backend, the PulseAudio calls — and the pane model, which was
the expensive part, travels as it stands. That is the difference between a
platform layer we design and one we already shipped for another reason.

## The three platforms, honestly

**macOS.** Everything the core shells out to exists: tmux, ssh, mpv, rsync,
fcntl. What is missing is launchd (8 unit files plus the runit scripts), the
PulseAudio calls (`pactl`, `playerctl` — CoreAudio has neither), and
`/proc/<pid>/environ`, which is how we find which pane a process is in;
macOS needs `ps -E` or `libproc`. Nothing here is research. Call it a week,
and the honest version of "nearly free" is "nearly free *if* the Mac is a
render or observe host, not the origin".

**Windows.** No tmux, no `/proc`, no signals, no runit. With herdr as the
pane layer the first of those stops mattering and the rest are the nameable
ones, so this moves from "a different program" to "expensive but bounded" —
conditional entirely on herdr having a Windows build. Until that is
answered, the shipping answer stands: **Sasonica Shell runs natively there**
— one Node runner, `install.mjs`, tested on hpo and in CI — so a Windows
machine can join as a client without the daemon at all. Those two are not
rivals: the Shell is how Windows joins *now*, herdr is what would let the
daemon follow later.

**Android.** Already the second platform, via Termux, and the `termux-*` and
`/storage/emulated` refs are by design, not debt.

## The order, if we do it

0. **Ask herdr about macOS and Windows builds**, and about pane read/send
   parity there. Everything below is sized by that answer, and it costs one
   message.
1. **A platform module in `core`** — `_lock.py` behind it first, then
   `/proc/environ` reads. One file to import, no scattered conditionals.
   This is worth doing on its own merits: it makes the posix assumptions
   *nameable*, which today they are not.
2. **launchd as a third service backend** in `setup.py`, beside runit and
   systemd. Mechanical, and testable on red5 by generating the plists
   without installing them.
3. **An audio backend seam** — `pactl`/`playerctl` are Linux; the mpv calls
   are not. Small, and the sink code already varies by host.
4. **A Mac as a render host**, end to end, before anything is claimed. The
   phone taught us this: it compiled long before it spoke.
5. **Windows only if herdr is there**, and even then the Shell stays the way
   a Windows box joins without a daemon.

## What this costs if we never do it

Little, today — everything runs on red5 and a phone. The reason to do step 1
anyway is that a platform layer makes the posix assumptions explicit, and we
have just spent a morning discovering we did not know where they were.
