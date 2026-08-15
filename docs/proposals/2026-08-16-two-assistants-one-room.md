# Two assistants, one room

How Sam and Cece take turns out loud, when neither can be told what the other
is doing.

Written 2026-08-16, from an evening of experiments on p8a. The hold tier is
built (`companion`, `/live`); the rest is a plan.

## The constraint everything else follows from

**Nothing can push into a model's turn.** No notification, no interrupt, no
event reaches an assistant that is not currently thinking. Cece cannot be told
that Sam has started speaking — she can only ask, or hear it.

So there are exactly three ways one of them can learn about the other:

1. **Asking** — a tool call at the top of a turn. Cheap, but it only happens at
   turn boundaries, which is not when interruptions happen.
2. **Hearing** — Sam's voice reaches Cece's open microphone as sound in the
   room, like David's does.
3. **Android telling her** — an audio-focus claim. The framework interrupts her
   *app*, which is the one channel that does not need her to be listening.

Everything below is built out of (3), with (2) as a curiosity and (1) as the
fallback.

## What the phone actually does (measured, not assumed)

- A Gboard dictation records with `VOICE_RECOGNITION` (src 6). A Claude Live
  session records with `VOICE_COMMUNICATION` (src 7). Both are visible to a
  non-privileged app, unredacted, at the first poll. The companion uses this to
  tell "David is talking to me" from "David is in a conversation".
- Claiming ordinary transient focus makes Live **pause itself** and post
  *"Paused while another app is using audio. Tap to resume."* It does not come
  back on its own.
- Claiming `GAIN_TRANSIENT_MAY_DUCK` with `setWillPauseWhenDucked(false)` leaves
  Live **listening**, no tap — and Cece then talks straight over Sam, because
  nothing has told her he started.
- While Live holds the mic and we hold no focus, **no focus callbacks arrive at
  all**. We only hear about focus while we hold some, so "what else is audible"
  is silent in exactly the case where nothing of ours is playing.

## The trade, stated plainly

| | Cece knows | Costs a tap | Talks over Sam |
|---|---|---|---|
| yield (transient claim) | yes | yes | no |
| duck (may-duck claim) | no | no | yes |
| share (no claim) | only by ear | no | probably |
| **hold (wait)** | n/a | no | no |

The first three all pay something. The fourth pays nothing because **the wait
ends by itself**: a voice session closes the microphone when it finishes, and
the reply is delivered then. Waiting is only expensive when nothing ends it.

## Tiers

Urgency decides how much interruption a reply is worth.

- **Quiet — hold.** The default. Sam's reply is paused while the session runs
  and delivered when it ends. Nothing is lost and nobody is interrupted.
- **Normal — ask David.** A notification card, *"Sam has something to say"*,
  with **Speak now** and **Later**. David can see both sides of the
  conversation; neither assistant can. Saying yes releases the hold for the rest
  of the session, because being asked again three sentences later is the
  interruption we are avoiding.
- **Urgent — take the room.** The transient claim. Live pauses, the banner
  appears, Cece knows because Android told her, and the tap is a fair price for
  something that could not wait.

Only the tier changes; the machinery underneath is the same claim in three
strengths, plus one card.

## What is built

`/live` on the companion's loopback readout selects the mode and remembers it:

```sh
ssh p8a curl -s '127.0.0.1:8770/live?set=hold'    # hold | yield | duck | share
```

`hold` is the default and carries the notification tier: the card, the two
buttons, the automatic delivery when the session ends. The other three remain
because they are the experiment this came out of, and because `yield` is what
the urgent tier will use.

Only speech is affected. Music is the phone's player, and a call or a voice
session ducking it is exactly what the focus policy is for.

## Open

- **Urgency has no route in yet.** `media say --urgent` already means something
  on the speech side; the companion cannot see it. The clean fix is the
  coordinator setting a property on the speech mpv the way it already sets
  `speaking`, and the app reading it — no new transport, one more observed
  property.
- **"Speak now" resumes, it does not restart.** The reply picks up where it was
  paused. For a clip held from its first word that is right; for one paused
  mid-sentence, replaying the sentence would be kinder.
- **Cece asking once per turn** is worth having underneath all of this, since
  she has the tools. It should be a habit at the top of a reply, not a timer —
  and it is a fallback, not the mechanism.
- **Does Cece hear Sam in `share` mode?** Untested as of writing. If she does,
  and handles it gracefully, that is turn-taking with no protocol at all — the
  human solution rather than the API one.
