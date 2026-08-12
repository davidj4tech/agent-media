# The input claim — who owns David's next utterance

`converse` decides that locally and well: the asker holds a unix socket open
for the life of its question, so a dead asker releases the claim by dying. See
`capture/rendezvous.py`.

This page is about the case that mechanism cannot cover. **Cece is Claude live
in the Claude Android app** — she runs in Anthropic's cloud, and the phone is
only her microphone. She has no process on red5 to hold a socket, so the one
thing that makes the local rendezvous safe has nothing to attach to.

## It is an exclusion, not a delivery channel

Nothing is routed to cece. While she is live she hears David *acoustically*,
through her own app, and his words never enter this system at all.

Verified 2026-08-12: HA Assist has no wake word (`wake_word_entity` is null on
all three pipelines) and no `assist_satellite` entity, so it cannot hear
anything unless Companion push-to-talk is physically pressed. A live cece
session additionally owns the mic, so push-to-talk could not capture even if
pressed. Two independent locks.

So the claim's only job is to stop *other* participants stepping on the
conversation:

- `converse` must not arm — it would claim an utterance that cannot arrive,
  block the next asker for the full timeout, and let sam speak a question into
  the middle of someone else's conversation.
- speech must hold — otherwise sam talks over her.

**If a wake word or an `assist_satellite` is ever added to HA, the first lock
is gone and this has to become real routing.** That is the trip-wire; there is
a matching comment in `Rendezvous.__enter__`.

## The signal comes from the mic, over the tailnet

A live session owns the phone's mic, and the phone can see that locally. Two
pieces, split along what each is actually good at:

- **Automate** toggles a flag file when the mic goes hot. It is the only thing
  that can observe the mic, and that is all it does. Documented in dotfiles
  `termux/automate/README.md`, which also holds the `.flo` and the rebuild
  steps.
- **`call_guard.ClaimHeartbeat`** watches that flag — it already polls it on a
  fast tick to drive the duck — and re-asserts the claim every 15s for as long
  as it is up:

```
POST http://red5:8675/input-claim
Content-Type: application/json

{"owner":"cece","ttl_s":45,"source":"phone-mic"}
```

The loop lives in Python rather than in the flow because the first version put
it in Automate and it did not survive contact: a loop drawn in a GUI parked
mid-cycle, claimed once and stopped, and could not be tested without dictating
into a phone. Off unless `MEDIA_INPUT_CLAIM_URL` is set, which is the whole
gate — every other call-guard behaviour is untouched either way.

The claim is raised on the **flag** edge specifically, not on call-guard's
combined hold. A phone call also engages that hold, and a call is not cece;
claiming her name for one would tell red5 something untrue.

Landing on red5: `speech-state-server.py` writes `capture/input_claim.py`'s
state file and sets a `media speech-hold` marker owned by `cece`. `converse`
then raises `Claimed` (a `Busy` subclass, so existing handlers back off
correctly) and speech is held by the marker — the existing authority, not a
second gate.

### Why the tailnet and not the relay

Measured 2026-08-12, p8a → red5: **~0.5s**. The same claim through tmux-relay
takes 5–10s, because `d1-runner` polls every 5s. That window is exactly when
sam speaks over the conversation, so it is the defect being fixed — routing the
claim through Cloudflare would preserve it.

This is also why the claim is not decided in D1 at all. See tmux-relay
`migrations/0008_floor.sql`: the D1 floor table is a *mirror*, written after the
fact and never read to decide anything, because a network-gated floor must
choose between failing open (authority still local, nothing gained) and failing
closed (a Cloudflare blip silences the house).

## Stopping the re-assert is the release

There is no release call in the normal path, and adding one would be a mistake.
A release is a thing that can fail — and a floor held by a holder that died
deadlocks everyone forever. Silence cannot fail.

`DELETE /input-claim?owner=cece` exists only for manual recovery, and
`MEDIA_INPUT_CLAIM=0` disables the exclusion entirely if something is
re-asserting a claim you cannot stop from here.

## The numbers

| | value | why |
|---|---|---|
| re-assert interval | 15s | frequent enough that the claim never lapses mid-session |
| `ttl_s` | 45s | three intervals — two posts can fail before the floor frees |
| speech hold | `ttl × 1.5` | rides past the claim so a late re-assert opens no gap |

The ratio is the point, not the absolute values: the TTL must exceed the
re-assert interval by enough to absorb a Wi-Fi blip, and stay short enough that
a vanished phone frees the floor before anyone notices it is stuck.

Every unclear path reads as *unclaimed* — missing file, unparseable JSON, no
owner, expired. A claim that fails open costs an overlap, which is the status
quo; one that fails closed leaves converse permanently unable to arm.

## Watching it

```bash
watch -n1 'curl -s http://red5:8675/input-claim; \
           ls ~/.local/state/agent-media/speech-hold.d/'
```

With a live session you should see `"held": true`, an `age_s` sawtoothing
between 0 and 15, and a `cece` marker. Both clear within 45s of the session
ending. If the claim holds but speech still talks over you, check the marker —
that is the actual gate; the JSON only feeds it.

The flow's own health is covered by call-guard's `last-external` heartbeat and
`media selfcheck` / `doctor`; a dead trigger and a quiet one are otherwise the
same observation, which is how barge-in stayed broken for two days in August
2026 while every service reported healthy.

## Regime B lives elsewhere

**Cece wanting to ask David something while *not* live** is a different
problem, and nothing on this page addresses it. A true remote asker has no
socket to hold and no hot mic to prove she is there, so liveness cannot be
inferred — it has to be asserted, and bounded, which is what a lease is for.

Built 2026-08-13 in tmux-relay as a one-row `lease` table
(`migrations/0009_lease.sql`, `relay-lease.sh`). It does **not** reach into
anything on this page. `relay-lease-watch.sh` polls the row on red5 and, while
a lease is live, sets an ordinary `speech-hold` marker with the owner
`lease-<holder>` — so from this side a cold asker is indistinguishable from any
other holder, and the gate is the same marker file it always was. An absent,
expired or unreadable lease all mean the same thing: no marker, speech plays.

Do not use it for a live cece. She is already excluded by the mic flag
documented above, in about half a second over the tailnet; a lease would be a
slower, weaker version of a signal that already exists.

## Related

- `capture/input_claim.py` — the landing pad
- `capture/rendezvous.py` — the `Claimed` exclusion
- `call_guard.py` — `ClaimHeartbeat`, and the duck that shares the same signal
- dotfiles `termux/automate/README.md` — the flow, the `.flo`, the flag contract
- dotfiles `packages/voice/.local/bin/speech-state-server.py` — the endpoint
- tmux-relay `migrations/0008_floor.sql` — why none of this is decided in D1
- tmux-relay `migrations/0009_lease.sql` — Regime B, and why a load-bearing
  table is admissible there without contradicting 0008
