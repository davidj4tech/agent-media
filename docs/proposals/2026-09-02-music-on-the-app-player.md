# Music on the app player — step 2 of the player on the phone

Status: proposal, nothing built. 2026-09-02. The sequel to
[2026-08-18-player-on-the-phone.md](2026-08-18-player-on-the-phone.md), which
sequenced this as step 2 and said it was "only worth it once speech has been
living in the app long enough to trust". Speech has been living there since
mid-August. Written the evening `mpv-music` was found deaf: a runit service
with no `XDG_RUNTIME_DIR`, so pulse refused it, and a YouTube queue burned
through a track every twenty seconds while every component reported healthy.

## Recommendation in one line

Yes — and it is **smaller than the August proposal assumed**, because the phone
music path is already fetch-then-play from local files, which is the thing that
made `MediaPlayer` risky for everything else.

## Why it is smaller than it looked

August's worry was streaming: `MediaPlayer.prepare()` measured 8.5–9.8s against
red5's clip server, so `BuiltinSpeech` fetches each clip to disk and plays from
there. Music needs no such invention. `sinks/music_local` **already** downloads
on the phone — it must, because a datacenter IP gets 403 from the YouTube CDN —
into `~/.cache/music-offline`, and hands mpv a local path. The player only ever
sees a file that is already on the disk it is playing from.

So the two hard parts of an in-app player are both already solved for this
channel: acquisition (yt-dlp on the residential IP) and locality (the cache).

## The shape

Exactly the shape speech took, which is the argument for it — it is a pattern
now, not an experiment:

1. `BuiltinMusic implements MpvServer.Player`. The interface is already
   channel-agnostic: load/stop/playlist/seek/pause/mute/volume/speed and the
   readbacks. Nothing in `MpvServer` is speech-shaped.
2. Bind it on a **new port beside 6601** — `BUILTIN_MUSIC_PORT`, next to
   `BUILTIN_SPEECH_PORT = 6612`. The Termux mpv keeps answering on 6601 the
   whole time.
3. Move the channel with **one environment variable**:
   `MEDIA_MUSIC_LOCAL_ENDPOINT=tcp://<phone>:6611`. Move it back by unsetting.
   Same reversibility speech has, for the same reason.
4. Only once it has lived there: retire `mpv-music`, `mpv-music-bridge-local`,
   and the legacy `$PREFIX/tmp/mpv-music.sock` symlink.

`Server.BUILTIN` — the playback mode the settings screen names and refuses —
stays refused until the book moves too. Its docstring says why in as many
words: it "would move music and the book too, and those have no player here
yet". This proposal removes half of that sentence.

## Vocabulary audit — what music actually sends

Read off `sinks/music_local` and `cli._music_*`, against what `MpvServer`
already dispatches:

| Verb / property | Sent by | Status in `MpvServer` |
| --- | --- | --- |
| `loadfile <path> replace\|append-play` | `play` | implemented |
| `loadfile … -1 force-media-title=…` (4-arg) | `play`, when the title is known | **argv[3]/[4] ignored today** — the title would be silently dropped |
| `playlist-next weak` / `playlist-prev` | `next`/`prev` | implemented (the `weak` flag is ignored; check that is right at the end of a queue) |
| `cycle pause` | `toggle` | implemented |
| `seek <n> relative` | `seek` | implemented (arithmetic lives in the protocol layer, not the player) |
| `volume` get/set | volume keys, duck | implemented — but see the scale note below |
| `speed` get/set | speed keys | implemented; `MediaPlayer` holds 1.6× exactly (measured, `mediaplayer-holds-16x`) |
| `idle-active`, `path`, `time-pos`, `duration`, `playlist-pos/count`, `pause`, `mute` | status, popup, MPRIS | implemented |
| `chapter-list`, `chapter` | `media music chapters` | **not implemented, and see below** |

Two real gaps, then, and one of them is smaller than it looks.

## The gaps, measured rather than guessed

**Chapters are already gone on this path.** `_music_mpv_chapters` reads
`chapter-list` off the live player, and the popup gives it a key. But of the
first twelve files in `~/.cache/music-offline`, **zero carry chapters** —
ffprobe finds none. The fetch writes `.mka` (Opus in Matroska) without
`--embed-chapters`, so the feature that a builtin player would "lose" is not
working on the phone endpoint today. It survives on the rooms/Mopidy-Mpv
endpoint, which this proposal does not touch.

That turns a blocker into an opportunity: the fetch helper already writes a
`.title` sidecar per video (80 of them in the cache) and yt-dlp already knows
the chapters (`--write-info-json` leaves them in `.info.json`; one is in the
cache already). A `.chapters.json` sidecar beside the audio would give the
builtin player *better* chapter support than mpv has there now.

**Loudness.** `mpv-music` runs `--af=dynaudnorm=targetrms=0.9:maxgain=15` and
`--volume=130 --volume-max=170` — software gain above nominal, because tracks
arrive at wildly different levels. `MediaPlayer` has neither a filter graph nor
gain above 1.0. Android's `DynamicsProcessing` (API 28+) and `LoudnessEnhancer`
are the candidates, and this is the one part of the port that is genuinely
unproven. It is also the one the ear will notice.

**Format coverage.** The cache is Opus-in-Matroska (`.mka`). Android has
decoded Opus since 5.0 and Matroska since forever, but `.mka` specifically —
audio-only Matroska through `MediaExtractor` — deserves a five-minute test on
p8a before anything else is built. It is the cheapest possible disqualifier.

## What it buys

- **Focus becomes honest on the last channel that fakes it.** The app currently
  holds Android audio focus *on mpv's behalf* for music and ducks it by hand;
  `call_guard`'s comments are an archaeology of that arrangement, including the
  evening three duckers lost the restore between them and left the music at 10
  for two hours. A player inside the app is ducked by Android because it is
  Android's stream.
- **A whole class of silent failure goes away.** Tonight's bug was `mpv-music`
  running with no `XDG_RUNTIME_DIR`, so pulse refused the connection and every
  track played with "Audio: no audio" while reporting success. That failure
  needs a Termux service, a session-scoped daemon, and an environment variable
  that runsv does not pass. Remove the first and the other two cannot bite.
- **Three moving parts retire**: the `mpv-music` service, the socat bridge, and
  the legacy socket symlink — and with them the phone-only run scripts that are
  not in git and therefore missed the August fix (see
  `runit-services-lack-xdg-runtime-dir`).
- **The addressed-player slot**, which the app can only hold properly while it
  is the thing making the noise.

## What it costs

- The battle-tested path. Music playout on the phone has had a year of failures
  found and fixed one at a time; some will have to be found again. This is the
  argument for coexistence rather than replacement, and for A/B by env var.
- dynaudnorm, until `DynamicsProcessing` is proven to stand in for it.
- Whatever mpv quietly handles that nobody has written down — the reason step 4
  (retiring the service) is deliberately last.

## Sequencing

1. **Five-minute test**: play a cached `.mka` through `MediaPlayer` on p8a. If
   Opus-in-Matroska does not decode, everything below changes shape.
2. `BuiltinMusic` against `MpvServer`, on `test/run.sh` first — the server is
   `android.*`-free, so the byte sequences `music_local` sends can be replayed
   on the build host before an APK is built.
3. Handle 4-argument `loadfile` in `MpvServer` (the title option). Small, and
   needed by the existing speech path's sibling verbs too.
4. Sideload, point `MEDIA_MUSIC_LOCAL_ENDPOINT` at the new port, live with it.
5. Loudness: measure a quiet track and a loud one against the mpv baseline,
   then decide whether `DynamicsProcessing` earns its place.
6. `.chapters.json` sidecar from the fetch helper, and chapters become better
   than they are now rather than worse.
7. Only then: retire the service, the bridge and the symlink — and move the
   book, at which point `Server.BUILTIN` can stop being refused.

## Open questions

- Does `MediaPlayer` decode `.mka` on this device? (Step 1 answers it.)
- Does `setNextMediaPlayer` gapless handoff behave on hour-long files, or is
  the join only proven at clip length?
- Where does position live across an app restart mid-set? mpv keeps it in the
  playlist; the app would need to persist it, which the book will need anyway.
- Should the fetch helper move into the app eventually, or stay in Termux? It
  needs yt-dlp and the residential IP; only the first is a Termux dependency,
  and it is the harder one to remove.
