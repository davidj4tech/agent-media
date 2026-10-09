# Handover: phone speech, the lost playlist count and the slow first sentence (9 Oct 2026)

## Done
- a3327d2 (agent-media, server): Open 1's root cause fixed. Frame seqs now start from the clock in ms (`_Hub.seq = int(time.time()*1000)`), and `Model.report` keeps what a report cuts (`Model.cut`) and puts it back when a later report counts or plays past it; a reported `pos` also raises the count to `pos+1`. Tests: `test_a_report_that_empties_a_reply_the_phone_plays_is_undone`, `test_frame_seqs_outrun_an_earlier_run_of_the_server`. **Live only after the canvas (agent-media-visual-canvas) restarts**: not restarted by this session.
- sasonica-app 4b649b7 (pushed to origin/main, so CI builds the APK): `SpeechFrames.apply` takes each frame's seq as it comes (`n != seq`), not as a high-water mark. `Media3Speech.extra()` reports `start` (index, id, wait_ms, queued_ms, cached, failed) and `made` (the last 4 sentences: voice microsoft/google-fallback/google, ms, tries, microsoft_ms, why). It uses the `extra` plumbing that origin's handoff commit 0a3ff99 already added. whatsNew entry 2026-10-09.
- 0d7066f (agent-media): frames-timing.log's "duration" line carries that `extra` as `phone`. Also needs the canvas restart.
- f3af135: `cmd_skip` (cli.py) treats more than one `clip_durations_s` on a tcp:// player as a playlist, so ← and ⏮ in the app set `playlist-pos` even when the phone's reported count is 0. Test: `test_skip_phone_playlist_steps_even_when_its_count_is_lost`.

## Open 1, now explained: Sasonica reported an empty playlist while playing
Root cause: the phone persists the last frame seq it applied (`frames_seq` pref) and kept it as a high-water mark (`if (n > seq)`). The server's hub counted from 0 again after each canvas restart (09:56 today), so the phone's seq stayed ahead of every new frame, every report it sent carried that old high seq, and `report()` treated each one as `current`. A report built before the reply's frames reached the phone (count 0, pos -1, idle: the player as it was after the previous stop) arrived after frames 201-210 had been sent. The server took it as current and truncated its entries to 0. `Model.report` only truncates and never re-extends, so the server's count stayed 0 while the phone played 0,1,2,3. The 14:39 reply (seq 307) still showed it: "reported" fired 2.33 s after "sent" with pos -1.
Either fix alone is enough: the server's clock-based seq (an old app build sees new frames above its stale seq) or the app's take-as-it-comes seq. Both are in. Not checked on the phone: p8a was offline on the tailnet (last seen 3 h earlier), so adb and logcat were unavailable.

The earlier notes:
Observed through an `observe_property` watcher on the frames port (127.0.0.1:16624, speech_frames.py, served by the canvas):
- 10:09:25.43–.54: the frames Model's playlist-count goes 2 → 15 (claim-play, then appended loads, seqs 201–210 delivered within 20 ms).
- 10:09:26.111: the phone's report (`POST /speech/state`, seq 210 = current, pos -1, time_pos -1) arrives, and the Model goes count=0, pos=-1, idle=True. `Model.report` truncates entries to the reported `count`, so the phone sent count 0 (or a count below 15).
- After that the phone reports pos 0 (10:09:27) and pos 1 (10:09:40.9), idle False, and advances through sentences. In an earlier replay it advanced 0→1→2→3 with count 0, so Media3Speech's playlist does hold entries: `queueAhead` needs `index < playlistCount()`.
- Phone code: sasonica-app `android/.../speech/{SpeechFrames,MpvServer,Media3Speech}.java`. `MpvServer.state()` reports `player.playlistCount()` = `playlist.size()`. Frames are applied in order on `exec`, and `seq` is bumped after the ops.
- Not yet checked: the phone's logcat. `agent-phone-adb connect` did not connect at 10:15. Also check whether anything still dials p8a:6614 directly (the old MpvServer socket is still bound) and sends stop or playlist-clear. And check whether SpeechService rebinds or recreates the player (`bind()`, `SpeechFrames.attach`). A fresh player reports count 0, pos -1, idle.
- A server-side guard to consider: `Model.report` should not drop entries when the phone reports a count below ours but a pos at or beyond that count.

## Open 2: a replay's first sentence starts 10–16 s after the push (still open, now instrumented)
Not found from the code alone, and there was no logcat. The trace shows pos 0 at about 27 s and pos 1 at 40.9 s for a clip of about 3 s. So the first clip's file took about 11 s to be ready after `startAt(0)`, which means the wait is in rendering the `tts:` clip, not in delivery. A replay's URIs are deterministic (`tts:<stem>?text=…&voice=…&fallback=…`), so a clip the phone already rendered would be a cache hit. These ones were misses.
Candidates, in order of fit:
1. Microsoft's first audio arrived but the stream stalled. `EdgeVoice.once` then waits up to ALL_MS (10 s) for turn.end, and only then do the retry or fallback run. 3 s + 10 s, or about 0.7 s + 10 s plus a Google render, fits 11-13 s.
2. Microsoft failed (about 3-5 s), and then the Google fallback is a *network* voice (`en-au-x-aua-network`, device.py FALLBACK_VOICE). On a poor link that can take seconds more, and it shares the engine's serial queue with the second warmer's sentence.
3. Burst contention on a replay: all 15 `load`s arrive in 20 ms, so the 2 warmer threads and the fetchers render at once. Live replies trickle in.
Next: after the canvas restart and the new APK, one device-voiced replay's frames-timing.log "duration" line will say which (`phone.start.wait_ms`, `phone.made[].voice/ms/microsoft_ms/why`). Proposed fixes by outcome: for (1), give a *waited-on* sentence a short ALL_MS (for example 4 s), or play what has streamed so far. For (2), make the fallback a local voice (`en-au-x-aua-local`) while playback is waiting on the sentence.

The earlier notes:
frames-timing.log: "duration" arrives 12–13 s after "sent" on replays of device-voiced (`.tts`, engine device) replies. On a live reply it was 2.45 s. It looks like the phone rendering the first `.tts` clip (Natasha/Microsoft with a Google fallback, sasonica-app 2cdcead, 7 Oct). It was already noted as #56 (replays 12939, 12950, 12957, 12960).

## Also seen
- The 10:09:39 stop mid-reply was a `read` speech-cut (`session_reply_read`), recorded when a message was sent in the session. That is by design, if David sent one at that moment.
- `media replay` unmutes the player itself. Never test on p8a by replaying (memory: replay-unmutes).
