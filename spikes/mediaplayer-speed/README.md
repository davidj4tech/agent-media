# MediaPlayer speed spike — does 1.6× hold?

Day one of `docs/handover/2026-08-18-player-on-the-phone.md`. One question,
and the toolchain for everything after it hangs off the answer:

> Does `android.media.MediaPlayer` play speech at 1.6× with pitch correction
> well enough to ship?

**Good enough** → the companion's no-Gradle build survives (aapt2 + javac + d8,
no AndroidX, no Maven) and speech-in-app is a couple of days on platform APIs.
**Not good enough** → Media3/ExoPlayer becomes mandatory, and the first real
decision becomes how to get AndroidX into this build at all.

## What it does

Five trials against a real rendered clip from red5's clip server
(`agent-media-clips.service`, `:8780`) — 72 seconds of mono 24 kHz TTS, which
is what the speech channel actually produces:

| # | Trial | What it isolates |
|---|---|---|
| 1 | 1.0× over HTTP | the control — if this is not 1.0 the measurement is wrong |
| 2 | 1.6× set before start | the shipping case |
| 3 | 1.0× → 1.6× mid-play | the mpv trap: a pinned `scaletempo2` never sees a new speed |
| 4 | 2.0× over HTTP | headroom |
| 5 | 1.6× from a local file | player vs transport |

Every trial asks for the speed with `AUDIO_FALLBACK_MODE_FAIL` and pitch pinned
at 1.0, so an unsupportable speed throws rather than quietly resampling into a
chipmunk.

**The rate is measured, not reported.** `getPlaybackParams()` says what was
accepted; the trials sample `getCurrentPosition()` against
`SystemClock.elapsedRealtime()` over an eight-second window and compare. This
is the whole point: mpv reported 1.6 while the audio advanced at 1.18, silently,
for long enough that the symptom was "replies feel slow". A spike that trusted
the player's own answer would have shipped that bug again. Tolerance is 4% —
inside what a listener hears, well outside a 26% error.

The listen row (1.0× / 1.6× / →1.6×) is the half a measurement cannot answer:
pitch preservation and time-stretch artefacts need an ear.

## Running it

```sh
./test/run.sh                      # the arithmetic and the readout, on the build host
./build.sh                         # -> build/mediaplayer-speed-spike.apk
scp build/mediaplayer-speed-spike.apk p8a:~/storage/downloads/
```

Then open it from Files on the phone, tap **Run the five trials**, and about a
minute later:

```sh
ssh p8a curl -s 127.0.0.1:8772
```

The readout is there because p8a has no adb and the previous spike's output was
"screenshot the activity", which made every result a retyping job. Loopback
only — 8770 is the companion's status server, 8771 is media-share.

## Known before the phone ran it

The clip fetch from p8a measured 17–45 KB/s across three runs against a stream
that needs ~9.7 KB/s at 1.6×. That is two to four times realtime, which is
thinner than "red5 already serves clips over HTTP" suggests. If trials 1–4 stall
where trial 5 does not, the finding is about the transport, and the answer is
that the app fetches a clip before playing it rather than streaming it — which
is one of the proposal's open questions, answered early.
