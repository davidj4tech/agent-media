# Grade B — phone-rendered speech with red5's popup following

Status: **landed 2026-08-11** (commits `59532ee`, `ead33fd`), by a cheaper route
than this doc originally scoped. Goal was: 0-latency speech audio on the phone
AND the full popup (now_playing, sentence highlight, skip/replay), while Claude
stays on red5.

## The core idea

The popup lives wherever the Claude session/tmux runs — **red5**. The audio
doesn't have to. The phone renders and plays the reply locally, so only the
*text* crosses Germany→AU and playback is ~0-latency; red5 keeps the state every
surface reads.

What was missing was never the audio. It was `current_sentence`: the phone lane
sends the whole reply as ONE POST (`MEDIA_REMOTE_SAY_CMD_PHONE` → `say-http` on
`p8a:8790` → `deploy/phone/say.sh`), so red5's sentence loop never runs, and
follow-along highlight, `media current-sentence` and the popup's sentence view
all had nothing to read.

## What actually landed

**The renderer already knew where the sentences were.** edge-tts reports word
and sentence boundaries as it synthesises, and `--write-subtitles` returns them
in the *same request* as the audio — no second render, no extra latency. That
measurement was being thrown away.

So the wire protocol grew one line, and nothing else moved:

    CLIP <basename>          what was rendered, so it can be replayed there
    SENTENCE <idx> <offset>  where sentence idx starts, in seconds
    DURATION <seconds>       how long the clip is — sent LAST, it means "now"
    PAUSE <0|1>              during playback: this device's player was paused
                             or resumed by something that isn't the caller

- `deploy/phone/say.sh` renders **one** clip and plays it with **one**
  `loadfile`, exactly as before — the reliable part of this lane is untouched.
  It additionally reads its own SRT back through
  `render/subtitles.py:sentence_offsets`, mapping cues onto the same
  `_split_sentences` the caller applies to the same text, and prints the marks.
- `say-http.py` needed no change at all: it already relays every line say.sh
  writes as its own flushed chunk.
- `intake/submit.py:_watch_remote_progress` collects the marks and hands them to
  **`_SentenceFollower`**, a thread that walks the timeline on the clock —
  nothing polls the phone, because a poll costs ~600ms on a link that drops a
  quarter of its packets. It writes `current_sentence`, `current_sentence_idx`,
  `clip_sentences`, `clip_offsets_s`, `clip_durations_s` and fires the existing
  `_HighlightScheduler`.
- **No marks?** The duration is apportioned by share of characters
  (`_apportioned_offsets`). Drifts within a reply; worth far more than nothing.
  This is also the answer for a fallback engine (openai writes no subtitles) and
  for a phone on an older commit.
- **Marks that don't fit?** `_offsets_from_marks` requires exactly one per
  sentence, in order, inside the clip. A mismatch means the far side split the
  text differently — an older commit, most likely — and pointing confidently at
  the wrong words is worse than a smooth guess, so it approximates instead.
  `sentence_marks` on the row says which of the two you're looking at.
- `media skip` seeks this lane by sentence (`cli.py:cmd_skip`): one clip, no
  playlist, so it seeks to `clip_offsets_s[target]` and re-stamps
  `play_started_at` — which is why the follower re-reads that origin every tick
  instead of trusting the one it started with.

Alignment is forgiving on purpose: the cue stream is what the voice *said*, not
what we wrote (numbers come back expanded, punctuation is gone, and a cue may
span two of our sentences because the splitter merges short fragments the voice
still separates). It interpolates within a spanning cue and returns nothing at
all rather than boundaries it doesn't believe.

Verified live on the **default** target (`phone`, no env override): marks
measured on the device, `sentence_marks: True`, sentences stepping at 4.05s and
7.58s of a 10s reply, and `media current-sentence` following.

## The three options this doc used to weigh, and why none of them was needed

1. **Per-sentence POSTs.** `say-http` holds a global one-utterance lock, so a
   render cannot overlap the previous sentence's playback without a new
   endpoint — roughly a second of dead air per sentence on this link.
2. **Phone renders per sentence into an mpv playlist.** Exact, but it rewrites
   the one piece of this lane that has been reliable, and adds an idle race
   between "render N+1" and "N just finished".
3. **Local approximation only.** Shipped, as the fallback — not as the answer.

The subtitles route is option 2's accuracy at option 3's cost.

## What the marks feed

The sentence state this lane now carries is read by three surfaces — the
copy-mode highlight, the status rows and the follow pane — described in
[follow-along.md](follow-along.md). Two things that were open here have since
been closed there:

- **Pause** no longer drifts the clock: `_stamp_speech_pause` freezes the
  reading at `paused_at` and credits the pause's length back to
  `play_started_at` on resume.
- **Replay** follows along: history carries `clip_sentences` + `clip_offsets_s`,
  and the replay tracker drives a single-clip reply from that timeline instead
  of polling a player behind a 45s circuit breaker.

## Still open

- **First-sentence latency** is unchanged and still the dominant cost: ~9-16s
  from submit to audio, nearly all of it the render plus the link.
- **Phone asleep / unreachable mid-reply:** still an ungraceful degrade.
- **Shared sink-speech broker** with the phone's own `media say`: fine while
  sessions run only on red5, but unguarded.
- Nothing outstanding on the follow-along itself: an externally-issued pause
  (media keys, notification controls, a call) now comes back as a `PAUSE` line
  from say.sh, which is already polling that player locally.
