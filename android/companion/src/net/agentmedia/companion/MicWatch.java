package net.agentmedia.companion;

import android.media.AudioManager;
import android.media.AudioRecordingConfiguration;
import android.os.Handler;
import android.os.SystemClock;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

/**
 * Is anything on this phone recording right now?
 *
 * <h4>Why this exists</h4>
 *
 * It is the last job Automate does for us. A voice-typing hold pauses Sam so
 * David can talk over him, and the only thing that can currently see the mic
 * come alive is an Automate flow writing a flag file on shared storage —
 * load-bearing, invisible when it dies, and the reason barge-in was quietly
 * broken for two days in August 2026. See
 * {@code docs/proposals} and the call_guard external-hold contract.
 *
 * <h4>What is actually knowable</h4>
 *
 * An ordinary app cannot register interest in <em>another</em> app's recording.
 * There are two ways round that and they cost very different things:
 *
 * <ol>
 *   <li><b>Read the list</b> — {@link AudioManager#getActiveRecordingConfigurations()}
 *       returns the device's active recordings. Post-Android-10 the entries
 *       belonging to other apps are anonymised for a non-privileged caller, but
 *       anonymised is not absent, and "how many" is the whole question here.
 *       Costs no permission and lights no indicator.</li>
 *   <li><b>Hold the mic ourselves</b> — open a silent {@code AudioRecord} and
 *       watch {@code isClientSilenced()}: when Gboard takes the mic, Android
 *       silences us, and that edge is the signal. Certain to work, and it
 *       costs {@code RECORD_AUDIO} and a permanently lit mic indicator.</li>
 * </ol>
 *
 * This class is (1), and it is deliberately a <b>probe</b> first: it watches,
 * publishes, and writes a line into the log for every change, and it drives
 * nothing. The one thing worth knowing before building anything on top is
 * whether the list is populated at all on this device — a question the docs
 * answer ambiguously and the phone answers exactly. Dictate into anything, then
 * read {@code /state} or {@code /log}.
 *
 * If the list turns out to be empty for other apps' recordings, (2) is the
 * fallback and this class keeps its shape: only {@link #active()} matters to
 * anything downstream.
 */
final class MicWatch {

    /** Told when the answer changes, on the main thread. */
    interface Watcher {
        void onMicChanged(boolean active);
    }

    /** How many change lines to keep for the readout. */
    private static final int HISTORY = 12;

    private final AudioManager audio;
    private final Handler main;
    private final Watcher watcher;

    private volatile boolean active = false;
    private volatile int count = 0;
    /** The last thing we could say about what is recording, for the readout. */
    private volatile String detail = "(nothing seen yet)";
    /**
     * The audio source of the first active recording, or -1 when nothing is.
     *
     * This turned out to be the whole answer. On 2026-08-15 a Gboard dictation
     * reported {@code src=6} (VOICE_RECOGNITION) and a Claude Live session
     * {@code src=7} (VOICE_COMMUNICATION) — the API naming the difference we
     * had been trying to infer from duration and from what else was audible.
     * Not redacted for a non-privileged caller on this device.
     */
    private volatile int source = -1;
    private final Deque<String> history = new ArrayDeque<String>();

    private AudioManager.AudioRecordingCallback callback;

    MicWatch(AudioManager audio, Handler main, Watcher watcher) {
        this.audio = audio;
        this.main = main;
        this.watcher = watcher;
    }

    /** True while something on the phone appears to be recording. */
    boolean active() { return active; }

    /**
     * Is the watch itself alive — as against "is something recording"?
     *
     * The callback being registered is the whole of it: without one, the two
     * ways this fails (no AudioManager at all, and a registration that threw)
     * both look exactly like a quiet microphone, which is the failure the home
     * screen's first pill exists to name.
     */
    boolean watching() { return callback != null; }

    int count() { return count; }

    String detail() { return detail; }

    /** The active recording's audio source; -1 when nothing is recording. */
    int source() { return source; }

    List<String> history() {
        synchronized (history) {
            return new ArrayList<String>(history);
        }
    }

    /**
     * Start watching. The callback is the fast path; {@link #poll()} is the
     * backstop, because whether the callback fires for another app's recording
     * is precisely the thing under test — and a probe that can only be woken by
     * the event it is trying to observe would report "never happens" either way.
     */
    void start() {
        if (audio == null) {
            CompanionService.log("mic: no AudioManager — not watching");
            return;
        }
        try {
            callback = new AudioManager.AudioRecordingCallback() {
                @Override
                public void onRecordingConfigChanged(List<AudioRecordingConfiguration> configs) {
                    main.post(() -> apply(configs, "callback"));
                }
            };
            audio.registerAudioRecordingCallback(callback, main);
            CompanionService.log("mic: watching (callback + poll)");
            poll();
        } catch (Throwable e) {
            // A probe is never worth the process. Same rule as the cards.
            CompanionService.log("mic: could not watch, carrying on: " + e);
            callback = null;
        }
    }

    void stop() {
        if (audio != null && callback != null) {
            try {
                audio.unregisterAudioRecordingCallback(callback);
            } catch (Throwable ignored) { }
        }
        callback = null;
    }

    /** Read the list now. Cheap, in-process; called on the existing 5s tick. */
    void poll() {
        if (audio == null) return;
        try {
            apply(audio.getActiveRecordingConfigurations(), "poll");
        } catch (Throwable e) {
            CompanionService.log("mic: poll failed: " + e);
        }
    }

    private final MicSteady debounce = new MicSteady();
    private Runnable settle;

    /**
     * Recordings that are actually receiving audio.
     *
     * Android silences a recorder rather than refusing it when its microphone
     * access is denied by app-ops — the session opens, the stream is zeros. On
     * p8a that is what {@code com.google.android.as} now is, permanently, and
     * counting it would leave the baseline exactly where it was:
     *
     * <pre>
     *   rec update … src:VOICE_RECOGNITION silenced pack:com.google.android.as
     * </pre>
     *
     * It is also the answer to the question this watch could not previously
     * ask. A silenced recording is not somebody listening, whoever owns it, so
     * this is worth doing on any phone rather than as a workaround for this
     * one.
     */
    private static int hearing(List<AudioRecordingConfiguration> configs) {
        if (configs == null) return 0;
        int n = 0;
        for (AudioRecordingConfiguration c : configs) {
            boolean silenced;
            try {
                silenced = c.isClientSilenced();
            } catch (Throwable e) {
                // Never assume silence on a phone that will not say: a missed
                // hold is worse than a spurious one, now that the spurious
                // ones have somewhere else to be caught.
                silenced = false;
            }
            if (!silenced) n++;
        }
        return n;
    }

    private void apply(List<AudioRecordingConfiguration> configs, String via) {
        int n = hearing(configs);
        String d = describe(configs);
        source = sourceOf(configs);
        long now = SystemClock.uptimeMillis();
        // What is open right now, and what the rest of the app should believe:
        // not the same thing while something samples the mic several times a
        // second. See MicSteady.
        boolean nowActive = debounce.update(n, now);
        if (settle != null) main.removeCallbacks(settle);
        long again = debounce.pendingInMs(now);
        if (again >= 0) {
            // The events stop; a burst with no successor would leave the
            // change pending forever.
            settle = () -> poll();
            main.postDelayed(settle, again + 20);
        }
        if (nowActive == active && n == count && d.equals(detail)) return;

        active = nowActive;
        count = n;
        detail = d;
        String line = stamp() + " " + (nowActive ? "recording" : "quiet")
                + " (" + n + ", via " + via + ") " + d;
        synchronized (history) {
            history.addLast(line);
            while (history.size() > HISTORY) history.removeFirst();
        }
        CompanionService.log("mic: " + line);
        if (watcher != null) watcher.onMicChanged(nowActive);
    }

    /**
     * What we can say about each recording. Every getter here is one Android may
     * redact for a non-privileged caller, so each is asked for separately and a
     * refusal is recorded rather than thrown — "source unavailable" is itself an
     * answer to the question this probe exists to settle.
     */
    private static String describe(List<AudioRecordingConfiguration> configs) {
        if (configs == null || configs.isEmpty()) return "-";
        StringBuilder sb = new StringBuilder();
        for (AudioRecordingConfiguration c : configs) {
            if (sb.length() > 0) sb.append("; ");
            try {
                sb.append("src=").append(c.getClientAudioSource());
            } catch (Throwable e) {
                sb.append("src=?");
            }
            try {
                sb.append(" silenced=").append(c.isClientSilenced());
            } catch (Throwable e) {
                sb.append(" silenced=?");
            }
            try {
                sb.append(" id=").append(c.getClientAudioSessionId());
            } catch (Throwable e) {
                sb.append(" id=?");
            }
        }
        return sb.toString();
    }

    /** The first active recording's source, or -1. See the field. */
    private static int sourceOf(List<AudioRecordingConfiguration> configs) {
        if (configs == null || configs.isEmpty()) return -1;
        try {
            return configs.get(0).getClientAudioSource();
        } catch (Throwable e) {
            return -1;     // redacted or refused: no worse than not asking
        }
    }

    /** Uptime, not wall clock: this is a log of intervals, not of times of day. */
    private static String stamp() {
        long s = SystemClock.uptimeMillis() / 1000;
        return String.format("%d:%02d:%02d", s / 3600, (s / 60) % 60, s % 60);
    }
}
