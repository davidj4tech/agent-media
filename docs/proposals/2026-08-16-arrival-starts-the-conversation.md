# Arrival starts the conversation

A message for Sam waits until Sam happens to be alive. This is how it stops
doing that.

Written 2026-08-16, out of the same evening as
`2026-08-16-two-assistants-one-room.md`, and out of the same wall: **nothing can
push into a conversation that is not happening.** That proposal worked around
the wall for two assistants in a room. This one goes through it, by noticing
that while you cannot interrupt a conversation, you can start one.

## The hole

`relay-msg.sh --to sam "…"` parks a row in the relay's D1. It is collected by
`check_messages`, which only runs when a session calls it, which only happens
while a session is live. The relay's own README is honest about this — *"Still
not a push"* — and the mailbox watcher on red5 closes half of it: unread mail is
typed into the pane, edge-triggered, so a live session sees mail arrive.

The other half is open. With no session running, mail waits indefinitely, and
nothing tells David it is there. Tonight that was invisible because the session
happened to be live for six hours; on an ordinary day it is the normal case.

## Three states, three answers

The watcher already exists for the first. It should own all three.

| state | answer | exists? |
|---|---|---|
| session live in tmux | type it into the pane (edge-triggered) | yes |
| tmux up, no session | **spawn one, with the mail as its prompt** | no |
| nothing up | notification to David; leave the mail unread | no |

The middle row is the whole proposal. The third is a fallback that must never
be silent — an undelivered message that nobody is told about is the failure
this is fixing, and it would be embarrassing to reintroduce it one row down.

## Spawning

The recipe is known and verified (see the session-spawn notes): Claude Code's
TUI needs a tmux **client** at launch, so `amux start` — which does
new-session-detached-then-attach — dies from a tool call with no tty. What works
is a new window in the already-attached session:

```sh
tmux new-window -d -t p-agent-media -c ~/projects/agent-media \
    "zsh -ic 'cl \"$PROMPT\"'"
```

`cl` is a zsh alias, hence `zsh -ic`. `-d` so a spawn does not steal David's
current window.

The prompt should say what it is: mail arrived, from whom, and that the job is
to read it and decide — not to reply reflexively.

## The two guards

Both are the reason this is a proposal and not a patch.

### It must not become a volley

The relay caps hops (`RELAY_MAX_HOPS`, default 4) because two assistants can
otherwise talk to each other forever with no human in the loop. That cap was
written when a reply still needed a live session at the other end — meaning the
last real brake was **nobody being there**. Auto-waking removes that brake, so
the cap stops being a backstop and becomes the only thing standing between us
and a machine that talks to itself all night.

So:

- A spawned session is a **courier, not a correspondent**. Its prompt says: read
  this, act if it needs acting on, and reply only if the reply carries
  information the other end asked for.
- **Never spawn for a message that is itself a reply** (`in_reply_to` set) unless
  a human is in the thread. A reply arriving is the end of an exchange, not the
  start of one.
- Spawns are rate-limited — at most one per N minutes, and never more than a few
  an hour — and every spawn is logged with the message id that caused it, so a
  loop is visible in one place afterwards.

### It costs money

Every wake is a Claude session. The poll interval and the bar for "worth waking
for" both want deciding, not defaulting:

- Poll every 5 minutes. The mailbox is a cheap D1 read; the expensive thing is
  what happens after, so poll often and spawn rarely.
- Wake for a message addressed to `sam` from a human-driven end. Do not wake for
  anything a machine sent unprompted.
- Quiet hours: notify, do not spawn, between (say) 01:00 and 08:00. Tonight
  ended at 01:20, and a session waking at 03:00 to read mail is spending money
  on nobody.

## Shape

One long-lived thing on red5, alongside the existing watcher rather than beside
it — it should absorb the watcher, not race it. State it needs: last seen
message id, last spawn time, spawn count this hour. All three belong in a small
file under the state dir, not in memory, so a restart does not re-spawn what it
already handled.

## Open

- **Which box does it watch?** `sam` today. If the same machinery is wanted for
  other boxes, the guards multiply rather than generalise, so start with one.
- **Does a spawned session speak?** Its reply would be spoken aloud by the Stop
  hook, in a room where David may not be. The hold tier from the companion
  proposal is the right precedent: say it quietly, or not at all, until someone
  is there to hear it.
- **What wakes it when red5 is asleep?** Out of scope, and worth naming: this
  fixes "no session", not "no host".
