# Follow-along is lost for the rest of a reply (23 Sep 2026)

**Status:** three holes found and fixed (`test_follow_after_barge_in.py`); one
idea from the original note deliberately left open, at the bottom.

## What David saw

A reply in session `1bb1add7-…` (23 Sep, ~12:07) played to the end on the
phone, but the thread never highlighted a sentence. (He also saw no title in
the player; that one is expected — the title is hidden when you are already
on that thread.)

## Step 1: where the submitting process logs

`~/.cache/agent-media/hook-play.log`. The Stop hook forks a detached child
(`hook_claude_code._play_detached`) and points its stdout/stderr at that file,
so logging's lastResort handler carries **WARNING and above** there. Nothing
configures a handler, so every `log.info` in `submit.py` goes on the floor —
which is why `journalctl` showed nothing *and* why the follow loop's own
bail-out lines were invisible: the two most interesting ones (`lost
follow-along`, and the silent `break`s) were INFO or nothing at all.

The loop now logs one line on every exit, at WARNING:

```
intake: follow ended (<reason>) at sentence 7/13, finished=False
```

The reasons are: `superseded`, `cut`, `player unreadable for ~5s`, `snapshots
kept coming back incomplete`, `skipped past the last clip`, `playlist emptied
under us`, `playlist replaced under us`, `playlist finished`, `no playback
progress for ~8s`.

## The timeline of the incident (`speech-events.jsonl`)

```
12:07:05  start  1bb1add7  "Typed tools are built and live…"   (116.7s of audio)
12:07:16  start  ac7078de  "Question 1. How should the app…"   ← barge-in
12:07:49  end    ac7078de
12:10:18  end    1bb1add7                                      ← 193s wall
```

193s of wall for 117s of audio, minus 33s of question, leaves ~43s the reply
spent neither playing nor interrupted — the pause David made at 76 s of 116.
`now_playing` was empty from 12:10:18, while the phone still held the reply
paused. Resuming it from the phone then played the rest with nothing bolded.

## What was wrong

Three separate ways the reply stopped saying where it was. Each is enough on
its own to produce exactly what David saw, and all three are on the path a
barge-in takes.

1. **An incomplete snapshot was read as an answer.** `_mpv_ipc.get_properties`
   returns only the names that succeeded and leaves the rest out — its own
   docstring says so, and `SinkSpeech.snapshot`'s warns about "a tick under
   load … missing the very field the loop ends on". The loop asked `.get`,
   so an absent `pause` read as *not paused* and an absent `idle-active` as
   *still playing*. A paused reply whose snapshot lost `pause` therefore
   looked like a wedged clip: `time-pos` stood still, the stall guard fired
   after ~8 s, and the follow ended while the audio was fine. An incomplete
   answer is now no answer — it counts as a miss, which is the bounded,
   resume-safe way of not knowing. A missing `time-pos` no longer counts as
   "no progress" either: not knowing where the player is is not evidence that
   it is stuck.

2. **A reply resumed *paused* marked nothing.** After a yield the loop resets
   `i = -1` so the next reading re-shows the sentence. The pause branch marked
   off that same `i`, so a reply that came back from a barge-in paused never
   marked again: `paused_at` was never stamped, `elapsed` ran on through the
   silence, and the app's bold walked away from the voice for the rest of the
   reply. The row's index is now tracked separately (`mark_i`), and survives
   the reset.

3. **The blind hold was blind in both directions.** When the loop gives up but
   the audio is probably still playing, it holds the speech token for the
   reply's remaining length — but it said nothing about where the reply was,
   and ignored the snapshots that did come back. One unreadable stretch of link
   therefore cost the listener the highlight for the whole rest of an audible
   reply. The hold now follows whatever it can read (a degraded follow, not an
   absence of one), and does not spend the reply's own length while the player
   reports `pause` — capped at `_BLIND_PAUSE_CAP_S` (600 s, the speech lock's
   own give-up window) so a player stuck at `pause=true` cannot hold the token
   for ever.

## Ruled out

- The pause path in the normal loop: `if snap.get("pause")` re-marks and resets
  the stall counter, so a paused reply keeps its live row — *when the snapshot
  carries `pause` at all*, which is hole 1.
- The resume path: it stops the player, reloads the whole playlist, seeks back
  to `resume_i`, restores a pause and re-arms the follow. Reproduced live
  (long reply + `media say --urgent` from another session): the reply came back
  and followed correctly, with a ~5 s window where `now_playing` was empty
  between the interrupter clearing it and the reply re-marking.
- The progress-aware give-up in `_SpeechPlaybackLock`: its timeout is 600 s, an
  order of magnitude more than this reply's whole life.
- `transcript.py` / `sessions.py`: they do not touch speech rows.

## Left open

The original note's third idea — have `conversation_log` put
`clip_sentences` + `clip_durations_s` on a line **whenever the player still
holds that reply**, not only while `now_playing` names it — is not done. It is
still the only fix that survives *any* way of losing the live row, but it is
not purely server-side:

- the ended history row keeps `clip_sentences` and `clip_durations_s` but
  **not** `clip_starts_s` (`_archive` builds its own extras), so the offsets
  would be apportioned, not measured;
- the server cannot cheaply say "the player still holds that reply" — the only
  authority is a ~1.3 s snapshot of the phone, on a route the app polls;
- but the app *is* the player on this lane, and knows its own position. So the
  shape that works is: the log hands the line its sentences and offsets with no
  `live`, and the app bolds from its own clock. That is a contract addition
  (§ "The live line") plus app work, and it is dead payload until the app uses
  it.
