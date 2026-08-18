package net.agentmedia.speedspike;

import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.media.PlaybackParams;
import android.os.SystemClock;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * The spike itself: play one real rendered clip at several speeds and measure
 * what the clock actually does.
 *
 * <h4>The question</h4>
 *
 * Does {@code android.media.MediaPlayer} play speech at 1.6x with pitch
 * correction well enough to ship? If yes the companion's no-Gradle build
 * survives and speech-in-app is a couple of days of platform APIs. If no,
 * Media3/ExoPlayer becomes mandatory and the first real decision is how to get
 * AndroidX into a build that has no Maven — which is a bigger question than the
 * player it would buy.
 *
 * <h4>The trials, and why each one is here</h4>
 *
 * <ol>
 *   <li><b>1.0x over HTTP</b> — the control. If this does not measure 1.0 the
 *       measurement is wrong and nothing below it means anything.</li>
 *   <li><b>1.6x set before start</b> — the shipping case. Speech is rendered
 *       ahead and played at David's reading speed; the speed is known before a
 *       byte plays.</li>
 *   <li><b>1.6x set mid-play</b> — the mpv trap, reproduced deliberately. A
 *       pinned {@code scaletempo2} filter never sees a new speed: mpv reported
 *       1.6 and the audio advanced at 1.18, silently. If MediaPlayer has the
 *       same shape of bug this is where it shows.</li>
 *   <li><b>2.0x</b> — headroom. Past the requirement, so a failure here is
 *       information rather than a blocker.</li>
 *   <li><b>1.6x from a local file</b> — separates the player from the
 *       transport. If streaming is what breaks the rate, the fix is a cache,
 *       not a different player.</li>
 * </ol>
 *
 * <h4>What this cannot decide</h4>
 *
 * Whether it <em>sounds</em> right. Pitch preservation and time-stretch
 * artefacts need an ear, and there is no microphone loop here — hence the
 * listen mode in the activity. This half rules out the failure that an ear
 * would miss, which is the rate being quietly wrong.
 */
final class SpeedTrials {

    /** Let the player settle after a speed change before sampling position. */
    private static final long SETTLE_MS = 1200;
    /** How long each measurement window runs. Long enough that jitter is dust. */
    private static final long WINDOW_MS = 8000;
    /** How long trial 3 plays at 1.0 before the speed is changed underneath it. */
    private static final long BEFORE_CHANGE_MS = 3000;

    /**
     * Where a finished run is posted, so the numbers outlive the app.
     *
     * Learned the hard way on the first device run: the readout server lives in
     * the process, and closing the app took the whole table with it. A minute
     * of playing out loud is too expensive to lose to a swipe, so the run
     * ships itself to red5 the moment it finishes and the loopback port becomes
     * the live view rather than the only copy.
     */
    private static final String POST_TO = "http://100.103.43.93:8782/";

    interface Log {
        void line(String text);
    }

    private final Log log;
    private final File cacheDir;
    private final List<Measure> results =
            Collections.synchronizedList(new ArrayList<Measure>());

    private volatile MediaPlayer listening;
    private volatile boolean cancelled;

    SpeedTrials(File cacheDir, Log log) {
        this.cacheDir = cacheDir;
        this.log = log;
    }

    List<Measure> results() {
        synchronized (results) {
            return new ArrayList<Measure>(results);
        }
    }

    void cancel() {
        cancelled = true;
    }

    /**
     * Blocking; call on a worker thread.
     *
     * @param report the whole on-screen log, read once at the end and posted
     */
    void runAll(String url, java.util.function.Supplier<String> reportText) {
        cancelled = false;
        results.clear();
        log.line("clip: " + url);

        File local = null;
        try {
            local = download(url);
            log.line("cached " + local.length() + " bytes in "
                    + local.getName() + " for the local-file trial");
        } catch (Exception e) {
            log.line("download for the local trial failed: " + describe(e)
                    + " (the HTTP trials do not need it)");
        }

        trial("1  http 1.0x", url, 1.0f, 0);
        if (local != null) {
            // The control the first device run lacked. 1.0x over HTTP measured
            // 0.904 there while every faster trial was exact, which is the
            // signature of a rebuffer rather than a speed the player could not
            // hold — but a spike should demonstrate that, not deduce it.
            trial("1b file 1.0x", local.toURI().toString(), 1.0f, 0);
        }
        trial("2  http 1.6x from the start", url, 1.6f, 0);
        trial("3  http 1.0x -> 1.6x mid-play", url, 1.6f, BEFORE_CHANGE_MS);
        trial("4  http 2.0x", url, 2.0f, 0);
        if (local != null) {
            trial("5  file 1.6x", local.toURI().toString(), 1.6f, 0);
        }

        log.line("");
        log.line(Measure.verdict(results()));
        log.line("Now use LISTEN at 1.6x and judge the pitch by ear — the rate "
                + "being right is necessary, not sufficient.");
        post(reportText.get());
    }

    /** Hand the finished table to whoever is waiting on red5. Best effort. */
    private void post(String report) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) URI.create(POST_TO).toURL().openConnection();
            c.setConnectTimeout(5000);
            c.setReadTimeout(10000);
            c.setDoOutput(true);
            c.setRequestMethod("POST");
            c.setRequestProperty("Content-Type", "text/plain; charset=utf-8");
            byte[] body = report.getBytes("UTF-8");
            c.setFixedLengthStreamingMode(body.length);
            try (OutputStream os = c.getOutputStream()) {
                os.write(body);
            }
            log.line("posted to " + POST_TO + " -> " + c.getResponseCode());
        } catch (Exception e) {
            log.line("posting the results failed: " + describe(e)
                    + " (they are still on 127.0.0.1:" + Readout.PORT + ")");
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /**
     * One trial.
     *
     * @param changeAfterMs 0 to set the speed before playback starts; otherwise
     *                      start at 1.0x and change to {@code speed} after this
     *                      many milliseconds of playing.
     */
    private void trial(String name, String source, float speed, long changeAfterMs) {
        if (cancelled) return;
        log.line("");
        log.line("--- " + name);
        MediaPlayer mp = null;
        String error = null;
        final int[] stalls = new int[1];
        double reported = -1;
        long posDelta = -1, wallDelta = 0;
        try {
            mp = fresh();
            // The framework says when it stops for bytes, so the spike no
            // longer has to infer it from a low rate.
            mp.setOnInfoListener((player, what, extra) -> {
                if (what == MediaPlayer.MEDIA_INFO_BUFFERING_START) {
                    stalls[0]++;
                    log.line("rebuffering at " + player.getCurrentPosition() + "ms");
                }
                return false;
            });
            long t0 = SystemClock.elapsedRealtime();
            mp.setDataSource(source);
            mp.prepare();
            log.line("prepared in " + (SystemClock.elapsedRealtime() - t0)
                    + "ms, duration " + mp.getDuration() + "ms");

            if (changeAfterMs == 0) {
                // Set before start. Note that setPlaybackParams on a prepared
                // player with a non-zero speed starts playback itself; start()
                // after it is harmless and keeps the two paths symmetrical.
                apply(mp, speed);
                mp.start();
            } else {
                apply(mp, 1.0f);
                mp.start();
                sleep(changeAfterMs);
                log.line("changing speed mid-play, position "
                        + mp.getCurrentPosition() + "ms");
                apply(mp, speed);
            }

            sleep(SETTLE_MS);
            PlaybackParams got = mp.getPlaybackParams();
            reported = got.getSpeed();
            log.line("player reports speed " + got.getSpeed()
                    + ", pitch " + got.getPitch());

            long p0 = mp.getCurrentPosition();
            long w0 = SystemClock.elapsedRealtime();
            sleep(WINDOW_MS);
            long p1 = mp.getCurrentPosition();
            long w1 = SystemClock.elapsedRealtime();
            posDelta = p1 - p0;
            wallDelta = w1 - w0;

            if (!mp.isPlaying()) {
                error = "player stopped during the window";
            }
        } catch (Throwable t) {
            // A fallback mode of FAIL means an unsupportable speed throws here
            // rather than silently resampling, which is the whole reason it is
            // set. Catching Throwable because the interesting failures on this
            // API are unchecked and vendor-specific.
            error = describe(t);
            log.line("threw: " + error);
        } finally {
            release(mp);
        }
        Measure m = new Measure(name, speed, reported, posDelta, wallDelta,
                                error, stalls[0]);
        results.add(m);
        log.line(m.line());
    }

    /**
     * Ask for a speed, with pitch held at 1.0 and no silent fallback.
     *
     * {@code AUDIO_FALLBACK_MODE_FAIL} is the point: the default lets the
     * framework mute or resample when it cannot time-stretch, and a resampled
     * 1.6x is a chipmunk, not speech. Failing loudly here is what makes the
     * measurement below trustworthy.
     */
    private void apply(MediaPlayer mp, float speed) {
        PlaybackParams p = new PlaybackParams();
        p.setAudioFallbackMode(PlaybackParams.AUDIO_FALLBACK_MODE_FAIL);
        p.setPitch(1.0f);
        p.setSpeed(speed);
        mp.setPlaybackParams(p);
    }

    private MediaPlayer fresh() {
        MediaPlayer mp = new MediaPlayer();
        // USAGE_MEDIA / CONTENT_TYPE_SPEECH: this is what the speech channel
        // would declare, and it is what the system's ducking decisions key off.
        mp.setAudioAttributes(new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build());
        return mp;
    }

    // ---- listen mode: the half a measurement cannot answer ----------------

    /** Start (or restart) continuous playback at {@code speed} for the ear. */
    void listen(String url, float speed) {
        stopListening();
        try {
            MediaPlayer mp = fresh();
            mp.setDataSource(url);
            mp.prepare();
            apply(mp, speed);
            mp.start();
            listening = mp;
            log.line("listening at " + speed + "x — judge the pitch and the "
                    + "artefacts, not the tempo");
        } catch (Throwable t) {
            log.line("listen at " + speed + "x failed: " + describe(t));
        }
    }

    /** Change speed under a clip already playing — the ear's version of trial 3. */
    void nudge(float speed) {
        MediaPlayer mp = listening;
        if (mp == null) {
            log.line("nothing playing");
            return;
        }
        try {
            apply(mp, speed);
            log.line("now " + mp.getPlaybackParams().getSpeed() + "x at "
                    + mp.getCurrentPosition() + "ms");
        } catch (Throwable t) {
            log.line("speed change to " + speed + "x failed: " + describe(t));
        }
    }

    void stopListening() {
        MediaPlayer mp = listening;
        listening = null;
        release(mp);
    }

    boolean isListening() {
        MediaPlayer mp = listening;
        try {
            return mp != null && mp.isPlaying();
        } catch (IllegalStateException e) {
            return false;
        }
    }

    // ---- plumbing ---------------------------------------------------------

    private File download(String url) throws Exception {
        File out = new File(cacheDir, "clip.mp3");
        HttpURLConnection c = (HttpURLConnection) URI.create(url).toURL().openConnection();
        c.setConnectTimeout(5000);
        c.setReadTimeout(15000);
        try (InputStream in = c.getInputStream();
             OutputStream os = new FileOutputStream(out)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
        } finally {
            c.disconnect();
        }
        return out;
    }

    private void release(MediaPlayer mp) {
        if (mp == null) return;
        try { mp.stop(); } catch (Throwable ignored) { }
        try { mp.release(); } catch (Throwable ignored) { }
    }

    private void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static String describe(Throwable t) {
        String m = t.getMessage();
        return t.getClass().getSimpleName() + (m == null ? "" : ": " + m);
    }
}
