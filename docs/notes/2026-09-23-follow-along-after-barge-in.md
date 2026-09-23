# Follow-along is lost for the rest of a reply (23 Sep 2026)

**Status:** open, diagnosed only as far as below. Handover note for a fresh
session — the hunt is log-heavy and the code is intricate, so it wants its
own context rather than the tail of a long one.

## What David saw

A reply in session `1bb1add7-…` (23 Sep, ~12:07) played to the end on the
phone, but the thread never highlighted a sentence. (He also saw no title in
the player; that one is expected — the title is hidden when you are already
on that thread.)

## What is true on the server

- The history row (`id 9865`) has everything follow-along needs: 13
  `clip_uris`, 13 `clip_sentences`, 13 `clip_durations_s` — and **12**
  `clip_starts_s`, one short of the sentences.
- `now_playing` for the speech sink was **empty** afterwards, while the
  canvas still reported the player holding that reply *paused at 76 s of
  116*. So the row that says "this is being spoken" was gone while the audio
  was not.
- `book_tracks.conversation_log` therefore returns that line with
  `sentences: []` and no `live` — the app has nothing to follow. The
  sentences are in the row's extras; the line simply does not carry them
  once the turn is no longer live.
- At 12:06–12:07 **another session** (`ac7078de-…`) spoke a question into
  the same player. A question is HIGH/URGENT, so it barges in across
  sessions by design: the reply steps aside at a clip boundary and resumes
  afterwards.

## What has been ruled out

- **The pause path is not it on its own.** In the remote-playlist follow
  loop, `if snap.get("pause")` re-marks `now_playing` and resets the stall
  counter, so a paused reply keeps its live row.
- **The resume path looks complete**: `yield_to_higher()` blocks until the
  token is back, then the loop stops the player, reloads the whole playlist,
  seeks back to `resume_i`, restores a pause, and re-arms the follow state.
- Not today's `transcript.py` or `sessions.py` work: those do not touch
  speech rows.

## Where to look next

1. **Which bail-out ended the loop.** In
   `packages/core/src/agent_media_core/intake/submit.py`, the remote-playlist
   follow loop (~4040–4300) leaves by: `should_abort`/`_speech_cut`;
   `misses > 50` (~5 s of unreadable snapshots); `stall > 80`;
   `idle-active` with `playlist-count == 0` (`_player_gone`, `finished`).
   Each logs. **Find where that process's log goes first** — the submitting
   process is the Stop hook inside a headless session (sessiond), and
   `journalctl --user` for 12:06–12:12 showed none of those strings, so the
   output is somewhere else. That is step one; everything after it is
   guesswork without it.
2. **The 12 starts against 13 sentences** is a thread worth pulling: it says
   the loop stopped marking one sentence before the end.
3. **The better fix may not be in the loop at all.** An ended row still
   carries `clip_sentences` + `clip_starts_s` + `clip_durations_s`. If
   `conversation_log` put those on the line whenever the player still holds
   that reply (not only while `now_playing` names it), follow-along would
   survive *any* way of losing the live row — which is the same thing a
   replay already does. Compare `book_tracks._live_line` (~770) and the
   live-merge in `conversation_log` (~960).

## How to reproduce

Two sessions: start a long reply speaking in one, then make the other ask a
question (`AskUserQuestion`, or `media say --urgent`). Watch the thread on
the phone, and watch `now_playing` (`StateStore().get_now_playing('speech')`)
across the barge-in and the resume.
