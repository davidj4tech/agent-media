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

## Answered: the offline copy (2026-09-04)

Read off `audiobookshelf-app` master rather than guessed. The answer differs by
media type, and that is the whole finding.

**A book does not update.** Three facts, each in the source:

1. `pages/item/_id/index.vue` — `showDownload()` returns false when
   `hasLocal`. Once a local copy exists the download button is *gone*. There is
   no "update" or "re-sync" action beside it.
2. `AbsDownloader.startLibraryItemDownload` enqueues one `DownloadItemPart` per
   server track, unconditionally — nothing consults what is already on disk.
3. `FolderScanner.scanParts` builds its track list from *this download's* parts
   and finishes with `localItem.media.setAudioTracks(tracks)` — a replace, not
   a merge, with indexes and offsets recomputed from scratch.

So the only route to a grown book is: delete the local copy, download the whole
thing again. Nothing is corrupted — the local item is keyed `local_<itemId>`
and reused, so there are no duplicates — but "incremental" it is not. Note the
download URL is already per file (`/api/items/<id>/file/<ino>/download`), so
the *server* has always been able to serve exactly the missing track. It is the
client that asks for everything.

Meanwhile the item page still shows the *server* item's tracks with a LOCAL
badge, and playback prefers the local copy: the new turn is visible in the UI
and absent from the audio. That is the worst of the possible outcomes to
debug, and worth knowing before anyone reports it as a bug.

**A podcast does.** Downloads there are per episode, not per item:
`EpisodeRow.vue` shows a download control for any episode with no
`localEpisode`, and `FolderScanner` merges the result with
`podcast.addEpisode(track, episode)`. A new episode on an already-downloaded
podcast is one tap and one file. Genuinely incremental, today, unmodified.

**What that does to the direction.** The two media types now trade places
depending on which problem you weigh:

| | book (clips as tracks) | podcast (clips as episodes) |
| --- | --- | --- |
| one item per conversation | yes | yes (a feed) |
| appends on the server | yes (measured above) | yes |
| **offline gets the new turn** | **no — delete and re-download** | **yes, per episode** |
| resume across the whole conversation | one timeline | per episode |

If offline listening matters for conversations, the podcast shape is the one
whose story already works end to end. If the single-timeline book shape is
worth keeping, the app change it needs is *small and specific* — skip parts
whose file is already local, and show a "sync" action when the server item has
tracks the local copy lacks. That is a patch of the shape upstream accepts, not
a fork.

## Still unknown

- Whether the item's socket push updates an open player, or only the next load.
- Whether a scan is needed at all, or the folder watcher suffices (the scans
  here were explicit).
