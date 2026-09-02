# The growing item — measured against stock Audiobookshelf

Status: experiment, run and cleaned up. 2026-09-02, against the live instance
(ABS **2.35.1**, `Conversations` library). Written to answer one question
before a fork is considered: **does a library item that grows work on stock
ABS?** If it does, clips-as-tracks needs no fork, no concatenation, and no
format argument — see [[abs-fork-direction]].

## Answer

**Yes, with one wrinkle that is fixable from outside ABS.**

A book item whose folder gains a file behaves as an append. Everything that
was there stays exactly as it was; the new file lands at the end with the
right offset; and the listener's position — which is stored in *seconds*, not
as a fraction — survives untouched.

The wrinkle is `isFinished`, and it is the one case that matters for a
conversation.

## What was run

`~/conversations/zz-growing-test/Growing Item Test/`, two 10-second tones,
scan, then a third, scan, then a fourth. Item deleted and the library returned
to how it was found.

| | 2 files | +1 file | +1 more |
| --- | --- | --- | --- |
| item id | `db0e58be…` | **same** | **same** |
| duration | 20 | 30 | 40 |
| track offsets | 0, 10 | 0, 10, **20** | 0, 10, 20, **30** |
| existing files' `ino` / index / mtime | — | **unchanged** | **unchanged** |

Stable inodes and unchanged offsets are the important half: a client that has
already downloaded tracks 1 and 2 is not holding anything the server now
disagrees with.

Progress across an append, set to 15s (mid-track-2) before the third file
landed:

```
after append:  currentTime=15   duration=20 (stale)   progress=0.75 (stale)
```

The position is intact. The `duration`/`progress` fields on the *progress
record* still describe the old length until the client next pushes — so a
percentage or a progress bar reads wrong for one session, while the actual
resume point is correct.

## The wrinkle: finished stays finished

The realistic case for a conversation is that you listen to the end of what
exists, and then a new turn lands. Measured:

```
mark finished (30s of 30s)  ->  isFinished=true
append a 4th track          ->  duration=40, isFinished STILL true, progress 1.0
                                item is NOT in Continue Listening
```

So the new turn is on the server, correctly placed, and **invisible**: nothing
brings the item back into the listener's path. That is the "media is finished,
a conversation is not" mismatch, located precisely — not in the file format,
not in the concatenation, but in one boolean and a stale progress row.

It can be repaired through the public API, which is the point:

```
PATCH /api/me/progress/<itemId>  {"isFinished": false, ...}   # clears the flag
PATCH /api/me/progress/<itemId>  {"currentTime": 30, "duration": 40, ...}
-> isFinished=false, currentTime=30, progress=0.75, back in Continue Listening
```

**Two calls, in that order.** Clearing `isFinished` in the same body as a
position *resets `currentTime` to 0* — ABS treats un-finishing as starting
over. The second PATCH puts the listener back at the head of the new turn.

## What this means for the fork

The direction's core claim — that clips are already tracks and concatenation
is a workaround — holds, and stock ABS supports it today. What was assumed to
need a fork needs a **publisher that appends a file and then re-opens the
item**. That is `media feed` work, not Audiobookshelf work.

That does not settle the app fork, which was always the more expensive half
(GPL-3 against our Apache-2, Capacitor/Gradle against our javac build). It
does mean the server side can stop being a reason for it.

## Still unknown

- **The Android app's offline copy.** Whether a downloaded item picks up an
  appended track incrementally, re-downloads everything, or ignores it until
  re-downloaded by hand. Stable inodes make the good outcome *possible*; they
  do not make it true. This is the one remaining question that could still
  justify touching the app, and it needs the app, not the API.
- Whether the item's socket push updates an open player, or only the next load.
- Whether a scan is needed at all, or the folder watcher suffices (the scans
  here were explicit).
