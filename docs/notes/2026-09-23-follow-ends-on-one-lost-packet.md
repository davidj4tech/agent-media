# The follow ends on one lost packet (23 Sep 2026)

**Status:** measured, not fixed. Found while fixing the barge-in note
(`2026-09-23-follow-along-after-barge-in.md`); David asked for it written down
rather than chased now.

## What is happening

Since the follow loop started logging its exits (23efc2d), half of the replies
lose sight of the player mid-way:

```
$ grep -o "follow ended ([^)]*)" ~/.cache/agent-media/hook-play.log | sort | uniq -c
      5 follow ended (player unreadable for ~5s)
      5 follow ended (playlist finished)
```

All ten are from one 75-minute stretch (12:22–13:33). The bails are mid-reply
— sentence 6/11, 6/13, 2/9, 11/13 — and each one drops the reply into the
blind hold for the rest of its length.

That is the visible symptom David reported twice today: no sentence bolded for
the rest of a reply, and (until 965f409) End of reply doing nothing.

## The link is fine. The budget is not

The `next` target[^next-rename] is Sasonica Next's own player on `tcp://p8a:6614`. Fifteen
consecutive snapshots from red5, just now:

```
ok=15 empty=0 median=1.40s max=2.48s
```

Nothing lost, but the margin is thin: `SinkSpeech.snapshot` uses
`timeout=3.0`, so a read half a second worse than today's worst fails.

What turns that one failure into a lost follow-along is the interaction of two
numbers that were each chosen sensibly on their own:

- `snapshot` passes `breaker_s=5`, so **one** failed read opens the endpoint's
  breaker for 5 seconds. While it is open, `_mpv_ipc._guard` raises
  *immediately* and `snapshot` returns `{}` — free, instantly, every time.
- the follow loop's miss budget is `misses > 50` with `time.sleep(0.1)` a tick.
  Its comment reads "~5s fully unreadable → bail".

Five seconds of instant empties is fifty ticks. **The miss budget is exactly
one breaker window**, so it can never survive a single lost packet: the read
that failed opens the breaker, the loop burns its whole budget inside that
window without ever attempting another read, and gives up. On a link that
loses a fifth of its packets, that is a coin flip per reply — which is what
the log shows.

## What would fix it

The loop is spending its budget on ticks where it *did not ask*. A skipped
tick is not evidence about the player; it is evidence about the breaker.

1. **Cheapest:** make the budget outlast the window — count misses in wall
   time and bail at, say, 3× `breaker_s`, so at least two real attempts happen
   before giving up.
2. **Better:** let `snapshot` distinguish "the breaker skipped this" from "the
   player did not answer", and don't spend the miss budget on the first. The
   information is already there — `_guard` raises a distinct message — it is
   just flattened to `{}` by `snapshot`'s `except`.
3. **Also worth considering:** `timeout=3.0` against a 1.4 s median and a
   2.5 s worst case is under a second of headroom on a link whose latency is
   known to be variable.

None of this is urgent now that the blind hold follows what it can read
(23efc2d) and obeys the listener (965f409) — the failure is degraded rather
than broken. But the loop is giving up roughly half the time, and it should
not be giving up at all.

[^next-rename]: Renamed since this was written: Sasonica Next is Sasonica
    (`com.sasonica.app`, 26 Sep 2026) and its speech target `next` is
    `sasonica` (27 Sep); the older Sasonica app is Sasonica ABS
    (`com.sasonica.abs`), target `abs` (was `app`). The text above keeps the
    names it was written with.
