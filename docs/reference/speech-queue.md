# The speech queue: order, priority and barge-in

Every session's hook speaks through one player, so only one may play at a
time. `_SpeechPlaybackLock` in
`packages/core/src/agent_media_core/intake/submit.py` serialises playback
across processes; its class docstring is the full reference. This page is
the design in brief and how to diagnose it.

## The token and the waiters

- **The token** is an exclusive `flock` on
  `$XDG_STATE_HOME/agent-media/speech-playback.lock`. It is released on fd
  close or process death, so a crashed holder frees it.
- **The waiters** are a lockless registry, one file per waiter in
  `speech-waiters/` (named `<pid>.<token>`), holding rank, submission time,
  session and whether it is still rendering. Dead pids are reaped on scan.
- **Rendering is outside the lock**, so sessions render in parallel; only
  the hand-off to the player is serialised.

## Priority is a rank, not a length

Four ranks: LOW 0, NORMAL 10, HIGH 20, URGENT 30. The intake hooks tag
replies NORMAL and questions, notifications and permission prompts HIGH. A
short notification cutting ahead of a long reply is an effect of that
tagging, not of length. A replay holds the token at `_REPLAY_RANK` (HIGH − 1):
above every reply, below a question.

Across **different sessions**:

| rank | when the token is held |
|---|---|
| HIGH / URGENT | preempt: the speaker steps aside at its next sentence boundary (`should_yield` → `yield_to_higher`) and resumes after |
| NORMAL | queue; never interrupt |
| LOW | give up rather than queue |

**Within one session**, speech never preempts itself: clips play in
submission order whatever their rank, so a session's HIGH question queues
behind its own longer NORMAL reply. "Session" is the tmux pane where there
is one (`_order_session`), because the producers disagree on session ids
(the hooks tag the payload's id, the `say` MCP tool none) but share the
pane. A clip with neither is its own session.

### Canonical order needs the submission time

The order stamp is the caller's submission time, `acquire(seq=...)`, not the
moment the token is asked for. Rendering happens first and a long reply
renders slower, so acquire-time order heard a short follow-up before the
reply it followed (30 Jul 2026). A sibling still rendering also announces
itself as a *pending* waiter (`announce()`) so a faster sibling defers to
it. Pending entries only order a session's own clips, never preempt, and
stop counting after `MEDIA_SPEECH_PENDING_TTL_S` (default 120 s) so a
wedged render cannot mute its session. If speech ever comes out of order,
suspect whatever runs between intake and `acquire()`.

### URGENT, keep or supersede

URGENT is the one same-session barge-in: it interrupts its session's clip at
the next boundary and jumps the queue. By default the interrupted message
**resumes** afterwards. Tagged `supersede`, it publishes a marker in
`speech-supersede/` at its seq, and `should_abort()` drops every earlier clip
of that session instead. Entry points: `media say --urgent`,
`media say --supersede` (implies urgent), and the MCP `say` tool's
`priority` and `supersede` arguments.

### Priority inheritance

A session's high clip waits behind its own earlier siblings, so those
siblings carry its rank (`_claim_rank`: the highest rank among my session's
live waiters). Without it a cycle formed: session A's NORMAL reply deferred
to session B's reply (bumped to NORMAL + 1 by a yield), B deferred to A's
HIGH question, and that waited for A's NORMAL reply. Eleven replies queued,
none spoke, and the token sat free (14 Oct 2026). The give-up deadline
(`MEDIA_SPEECH_LOCK_TIMEOUT_S`, 600 s) never fired, because it restarts
whenever the holder's progress changes.

### A paused holder steps aside

A reply whose player is paused keeps the token, so a queued reply never
overtakes it. Once paused for `MEDIA_SPEECH_PAUSED_HOLD_S` (default 90 s)
with another session waiting, it `step_aside()`s, waits until the other
reply has actually taken the token, and comes back still paused. Its own
session's later replies still queue behind it. 0 or less holds for as long
as it stays paused. Before then, `media skip` frees it.

### Duplicate question read-outs

An AskUserQuestion can be seen by more than one hook, and a dedup against
history races (a queued, unplayed clip has no history row yet).
`_claim_once` in `intake/hook_claude_code.py` is an `O_EXCL` claim at emit
time, keyed on the question.

## Diagnosing late or missing speech

Late speech is almost never rendering. It is a hook holding the token while
it believes audio is still playing, or a queue in which nobody may go.

1. **Who is waiting:**

   ```
   cat ~/.local/state/agent-media/speech-waiters/*
   ```

   Rank, seq, session, pending, speaker, kind for each.

2. **Who holds the token:** `/proc/locks` lists flocks by inode (verified on
   Linux 6.12, 18 Oct 2026). Nothing there with waiters queued means the
   token is free and the waiters are deferring to each other: look at their
   ranks and sessions for a cycle.

   ```
   grep ":$(stat -c %i ~/.local/state/agent-media/speech-playback.lock) " /proc/locks
   ```

   The fifth field is the holder's pid. (`lslocks` shows the same.)

3. **What the holder is doing:** with `py-spy` installed,
   `py-spy dump --pid <holder> --locals` shows the follow loop's `misses`,
   `stall` and `finished`. A waiter is in `_take`; a holder is not.

What has blinded a holder before:

- **The slow-endpoint breaker.** A read slower than `MEDIA_MPV_SLOW_MS`
  (default 1200 ms) trips the breaker, which then skips the endpoint for
  `MEDIA_MPV_BREAKER_S`. Its state is shared on disk
  (`breaker-mpv.json`), so one process tripping it blinds every hook. The
  follow loop's reads pass `slow_s=0, breaker_s=5` for this reason, and the
  installer's host defaults set `MEDIA_MPV_SLOW_MS=2500` for a far phone. If
  speech goes strange after a latency change, check this first.
- **A player that never reports idle** after a reply: the loop calls it
  stalled and holds the token for the reply's whole length.
- **A speech hold** (`media speech-hold`) silences every session. The clip
  lane waits for it outside the token, so the queue decides the order when
  it lifts; the whole-reply remote path still waits inside.

For a phone that says "speaking" over silence, see
[speech-phone-lane.md](speech-phone-lane.md). For a highlight that drifts
from the voice, see [follow-along.md](follow-along.md).
