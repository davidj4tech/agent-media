# Follow-along: reading what is being spoken

One wish — *show me the words as you say them* — and one switch: `v` in the
control popup (`prefix a`), which flips the auto-highlight flag at
`$XDG_STATE_HOME/agent-media/auto-highlight`. Everything below is off while
that is off, and the popup's `○` glyph is tinted black-on-yellow while it is on,
so the state is answerable between replies as well as during them.

## The three surfaces

| | where | when |
|---|---|---|
| copy-mode highlight | the pane the reply was written in | it can find the sentence there |
| status rows | the tmux status bar | it can't |
| follow pane | a split of its own | opt-in, `MEDIA_FOLLOW_AUTO=1` |

**The copy-mode highlight** re-anchors the pane's copy-mode onto each sentence
as it is spoken (`_tmux_highlight_text`). It searches the scrollback of a normal
pane and the visible screen of an alternate-screen one, so it works while the
words are somewhere the pane can reach.

**The status rows** cover the case it cannot: Claude Code and every other
fullscreen TUI hold the alternate screen, where there is no scrollback and the
app redraws over anything painted into it. The signal is free — a failed search
*is* "these words are unreachable" — so `_tmux_highlight_text` returns whether
it found them, and a miss hands the sentence to the bar instead
(`_set_follow_rows`). Three heights, per session:

| height | when |
|---|---|
| 1 (`on`) | follow-along off — the rows render nothing, so more would be blank |
| 2 | on: one row, the sentence being spoken or "follow-along on" between replies |
| 1 + `MEDIA_FOLLOW_ROWS` (4) | on, and the words are not reachable in the pane |

The middle height matters more than it looks: the switch is usually thrown
*between* replies, where there is no sentence to fail to find — and a switch
with no visible effect is indistinguishable from a broken one.

The rows' **text** arrives as tmux options (`@am_follow_0…N`), set by
`publish_follow_text` as each sentence starts. It was `#(media current-sentence
…)` at first, which is the obvious way and the wrong one: a `#()` runs at most
once per `status-interval` and serves a cached result in between. Measured
against a sentence written at a known moment, the bar was **1 to 2 seconds**
behind the audio — for a thing whose only job is keeping pace, the whole point
missed — and it spawned a Python process per second per client to do it. An
option is read at draw time: same measurement, ~20ms.

The decision **latches for the reply**. Changing the status height resizes the
panes and makes a fullscreen app redraw, so a reply alternating visible and
scrolled-off sentences would strobe the window. Once the reader has been sent to
the rows, they stay.

Per *session*, never globally: another session's panes have no business resizing
because this one is speaking. And one row is spelled `on` — `status 1` is an
error in tmux, not a synonym.

**The follow pane** (`media follow`, `media-follow-pane`) renders the whole
reply, dims what has been spoken, marks the current sentence and scrolls itself.
It charges the conversation rows on every reply, so it is opt-in; where it is
opted in, `v` opens and closes it with everything else. It prefers a split
beside the conversation (window ≥ 90 columns), then a strip along the bottom
(≥ 24 rows), and opens a window of its own only when asked for by name.

## Where the sentence comes from

All three read the same two fields on the speech now-playing row:
`clip_sentences` and `current_sentence_idx`, plus `current_sentence` for the
one-line surfaces (`media current-sentence`).

- **Local lanes** render a clip per sentence, so the playlist position *is* the
  sentence index; the loop writes it as it plays each one.
- **The phone lane** renders the whole reply as one clip on the device, so
  there is no position to read. It reports `SENTENCE <idx> <offset>` marks
  instead (see [speech-phone-render-grade-b.md](speech-phone-render-grade-b.md))
  and `_SentenceFollower` walks that timeline on the clock. Absent marks, the
  duration is apportioned by share of characters.

**Nothing polls the player on that lane.** A read costs ~600ms over a link that
drops a quarter of its packets, and once it has been slow the circuit breaker
refuses the next ones outright — which is how a polling follower concluded that
playback had ended two seconds into a reply that was still going.

The cost of following a clock is that it cannot see the audio stop, so the
things that stop it write to the row instead:

- **pause** — `paused_at` freezes the reading; the resume adds the pause's
  length to `play_started_at` (`stamp_speech_pause`, `elapsed_from_row`).
- **skip** — `media skip` seeks the player and re-stamps `play_started_at`, so
  the follower re-bases instead of dragging the highlight back.

A pause *nobody here issued* — a media key, the notification controls, MPRIS, a
call — is reported by the renderer instead, as a `PAUSE <0|1>` line on the same
stream that carries the sentence marks. It is already polling its own player
locally at ~2ms a read and the stream is still open, so it costs nothing; the
alternative would be us polling the link that this whole design exists to
avoid.

## Replay

A replayed reply follows along too. History carries `clip_sentences` and
`clip_offsets_s`, so a single-clip reply can be followed even though it has no
playlist; `cmd_replay_track` drives it from the row exactly as the live follower
does, and goes through the same highlight scheduler, so a replay of something
scrolled out of view takes the status rows as a live reply would.

## The app's clock

The Sasonica app's bold, and the speech bar's sentence, follow a timeline
on the clock rather than the player. `book_tracks._live_turn` hands them
`elapsed` and `offsets` from the speech now-playing row:

- **`elapsed`** is wall time since `play_started_at`, frozen at `paused_at`
  (`elapsed_from_row`). `play_started_at` is stamped when the first sentence
  starts playing (`_stamp_start`). Falling back to `started_at`, which is
  stamped at submit, put the bold ~10 s ahead on a measured reply
  (19 Sep 2026).
- **`offsets`** are `clip_offsets_s` (positions inside one combined clip),
  else the measured `clip_starts_s` with the sentences still to come
  predicted from the last measured start plus clip lengths, else summed
  `clip_durations_s`. Summed durations leave out the gaps between clips;
  measured starts include them. Before a reply has measured its own gap,
  `CLIP_GAP_S` (0.3 s) is assumed.

Claude Code replies to the phone go through the per-clip lane (`_mark` and
`_stamp_start` in `intake/submit.py`), not `submit_stream`. Never write
`clip_offsets_s` for a per-clip lane: `media skip` reads it as positions
inside one clip.

### Two clocks

`/speech/now`'s `pos` on the phone lane is built from summed clip durations
plus `time-pos` (`cli._remote_snapshot`, `_as_response_timeline`): no
gaps. The live frame's `elapsed` is wall time. The app corrects one against
the other (`useElapsedSkew`) and reads `elapsed - pos` as lag, so anything
that puts wall time into `clip_starts_s` that the durations lack shows up
as skew. A large positive `delay` in the trace means skipped time, a
re-stamp or a wait became a hole in the starts; a forward skip makes the
opposite gap. (16 Oct 2026: presses of previous re-stamped the sentence at
"now", and the bold held back 54 s. A backward move now keeps the old start
and moves the origin on.) `wall_position` writes `live_pos_s` on the wall
clock for the same reason.

### Follow on the clock, check with an answer-only read

A phone read costs seconds and trips the shared breaker when polled (see
[speech-phone-lane.md](speech-phone-lane.md)). Code that follows phone
playback must use the row's clock (`play_started_at`, `elapsed_from_row`),
and must never treat a refused read as "ended": that cleared now_playing
while the phone kept talking, and the app lost its mini player and bold.

The clock alone cannot see the player go away (an app install kills it, and
the bold read on in silence). So the replay follower asks the player in a
side thread every `_REPLAY_ALIVE_EVERY_S` (1.5 s, faster when the read is
cheap): no answer is ignored, and only idle answers spanning that interval
end the follow and stamp where it stopped. Anything new that follows on the
clock needs the same answer-only check.

## Diagnosing

The server's side leaves a record: the now-playing row, and the replay
follower's `replay-anchor.log`. The app's corrections (transit, skew, its
per-device lead) do not, so the app posts each move of the bold to
`POST /follow/trace` (`agent_media_server/follow_trace.py`). Each batch is
written beside the row as it stood when the batch arrived, one JSON line per
batch, to `$XDG_STATE_HOME/agent-media/follow-trace.log` (newest
`KEEP_LINES` kept).

When someone says the bold is off, read that reply's lines first. Per event:
`idx`, `elapsed`, `delay`, `skew`, `lead`, `off`, `next_off`, `paused`, `src`;
per row: `play_started_at`, `paused_at`, `current_sentence_idx`,
`clip_offsets_s`, `clip_starts_s`, `clip_durations_s`, `live_pos_s`,
`live_pos_at`. To put the phone's times on the server's clock, join each
move to the row's `play_started_at` plus offsets.

To check the server side alone, sample the `now_playing` row during a reply.

## The transcript dump, removed 2026-08-11

There used to be a fourth answer to the fullscreen problem, opt-in behind
`MEDIA_HIGHLIGHT_DUMP=1`: type `Ctrl+O` then `[` into Claude Code to make it
print its conversation into the pane's real scrollback, so the copy-mode
highlight had something to search; `Escape` at the end put it back. It re-did
that on every sentence, because each of Claude's redraws staled the dump.

It went because the status rows answer the same question without typing into
somebody else's application. It also cleared the pane's history, and its keys
would have landed in the input box if Claude ever rebound `Ctrl+O`. Nothing on
the fleet had it enabled.

The one thing it could do that nothing now does: scroll-and-hold reading of a
*whole conversation*, not just the reply in flight. If that is ever wanted
again, it should be built as something that renders the transcript itself —
`media follow` reading history rather than the live row — not as remote control
of another program's keybindings.

## Configuration

| | |
|---|---|
| `MEDIA_FOLLOW_ROWS` | status rows for the sentence (default 4; 0 disables). Keep in step with the `status-format[N]` rows the tmux config lays out. |
| `MEDIA_FOLLOW_AUTO` | `1` lets the follow pane open with the flag. Unset = the pane only opens by hand. |
| `MEDIA_FOLLOW_HEIGHT` / `_WIDTH` | the pane's rows / columns (8 / 46). |
| `MEDIA_FOLLOW_MIN_SPLIT` / `_MIN_ROWS` | when the pane may split beside (90 cols) or below (24 rows). |
| `MEDIA_SPEECH_PLAYOUT_MS_<TARGET>` | how late the audio is heard, so the highlight lands with it rather than ahead. |
| `MEDIA_HIGHLIGHT_KEYSTROKE_S` | skip the in-pane highlight for a turn if you typed this recently (default 5; `prefix V` overrides for one turn). |
| `MEDIA_AUTO_HIGHLIGHT=1` | force the flag on for a host, ignoring the file. |
