# What a chat can be pointed at

2026-09-21. Sasonica's "New chat" list currently offers *projects*, read by the
app from Audiobookshelf: the series of a library that must be named
`Conversations`, whose series names must be the `p-<dir>` tmux session names
this host happens to use. David's question: that is his setup, not a product —
should the list be **running sessions** instead, and should sessions hosted by
**herdr** or **VS Code** appear in it?

## What the investigation found

**1. The app is re-deriving something the server already knows.**
`reply.project_target()` (packages/visual/.../reply.py:990) already turns a
project name into `(tmux session, cwd)` using agent-media's own manifests plus
the transcript's cwd. The ABS series list adds nothing but a second, weaker
copy of that knowledge — one that needs a library name, a folder convention and
a series ordering the app has to guess at. Everything the picker wants is
server-side already.

**2. A live session's directory is free.** Every Claude process carries its cwd
at `/proc/<pid>/cwd`. Sampled today: eight live Claude processes, cwds
`projects/agent-media` ×5, `projects/runlet` ×2, `.meridian` ×1. No library, no
naming convention, no `p-` prefix.

**3. Discovery is tmux-shaped, and that is the real lock-in.**
`reply.live_sessions()` walks `/proc`, keeps processes named `claude`, and
**skips any without `TMUX_PANE` in their environment**. Everything downstream
addresses a session as a tmux pane id (`%562`): capture is `capture-pane`,
sending is `send-keys`. So the multiplexer is not an implementation detail of
the list — it is the address.

**4. herdr is a peer of tmux, not a lesser source.** `herdr pane` has `list`,
`read`, `send-text`, `send-keys`; `herdr agent` has `list`, `read`, `prompt`,
`wait`, and panes can `report-agent-session` — an agent identity of its own.
The CLI answers JSON (`herdr agent list` → `{"result":{"agents":[]}}`). So
herdr offers all three verbs a destination needs: **list, capture, send**. It
is empty right now (no agents running under it), which is why nothing of his is
missing from the phone today — not because it cannot be supported. A herdr-
hosted Claude is invisible to `live_sessions()` purely because it has no
`TMUX_PANE`; that is the "pane-less process" seen in the August cleanup.

**5. VS Code does not have a send path here.** Claude Code's IDE integration
advertises itself through a lock file in `~/.claude/ide/` (empty on red5: no
IDE session has run). It is per-host and it is a websocket, not a terminal —
nothing `send-keys` can reach. An editor session can be *read* from its
transcript; it cannot be replied to without a second, different channel.

**6. Transcripts are history, not destinations.** `~/.claude/projects/*.jsonl`
has far more recent sessions than there are panes — a dozen in `.meridian`
inside an hour, from headless and sub-agent runs with no terminal at all. A
list built from transcripts would offer destinations that silently swallow
messages.

## Proposal

One endpoint on the canvas, `GET /targets`, answering the whole question:

```json
{"sessions": [{"id": "...", "title": "...", "state": "waiting",
               "source": "tmux", "cwd": "/home/ryer/projects/runlet"}],
 "places":   [{"name": "agent-media", "path": "/home/ryer/projects/agent-media",
               "last_used": 1789...}]}
```

- **sessions**: what is running and can be typed into, whatever hosts it.
- **places**: directories sessions have actually run in, newest first — the
  "new chat in X" rows. Derived from the manifests and `/proc`, so no library
  and no naming convention. The current six-project cap becomes the server's call.
- The app renders what it is given and stops knowing about Audiobookshelf
  series, library names or `p-` prefixes. Another person's canvas answers with
  their own directories and their own multiplexer, and their app is identical.

Behind it, a **source adapter** with exactly three verbs — `list`, `capture`,
`send` — because that is the set the reply path already uses:

| source | list | capture | send | status |
|---|---|---|---|---|
| tmux | `list-panes` + `/proc` | `capture-pane` | `send-keys` | shipped |
| herdr | `herdr agent list` | `herdr pane read --format ansi` | `herdr pane send-text` | cheap, all three exist |
| IDE (VS Code) | `~/.claude/ide/*.lock` | transcript | — | no send path; read-only at best |

Recommended order: pull the list server-side first (it removes the setup-
specific part and changes nothing the user sees), then add the herdr adapter,
which is a genuine second source for a generic product. Leave editor sessions
out of the picker until there is a send path — or show them visibly read-only,
never as something you can reply to.
