package net.agentmedia.companion;

/**
 * Is the microphone <em>really</em> in use, or did something just blink?
 *
 * <h4>The evening this had to exist</h4>
 *
 * Android System Intelligence ({@code com.google.android.as}) opens a
 * {@code VOICE_RECOGNITION} recording and closes it again several times a
 * second, forever:
 *
 * <pre>
 *   17:49:09.144 rec update riid:14511 src:VOICE_RECOGNITION pack:com.google.android.as
 *   17:49:09.334 rec stop   riid:14511 src:VOICE_RECOGNITION pack:com.google.android.as
 * </pre>
 *
 * The mic watch reported every one of those, the dictation hold paused speech
 * for each, and resumed it when the burst ended. Against the Termux mpv that
 * was invisible — it pauses and resumes a broker in milliseconds and nobody
 * hears it. Against a player inside the app it was audible, and David's word
 * for it was "very jittery".
 *
 * <h4>Why a sustain rather than a package filter</h4>
 *
 * The obvious fix is to ignore that package, and it is not available:
 * {@code AudioRecordingConfiguration} does not tell an ordinary app who is
 * recording. What separates a person from a system probe is duration —
 * dictation and conversation hold the microphone, a recogniser samples it — so
 * that is what this measures.
 *
 * Asymmetric on purpose. Engaging costs latency on barge-in, which is the one
 * thing this watch exists to be fast at, so it is kept short. Releasing costs
 * nothing but a little extra quiet, and a hold that flickers off between two
 * words of dictation is worse than one that lingers.
 *
 * {@code android.*}-free, so {@code test/run.sh} covers it.
 */
final class MicSteady {

    /**
     * How long one recording must persist before it counts as a person.
     *
     * Measured rather than guessed. On p8a, {@code com.google.android.as}
     * holds the microphone for 560–700ms and releases it for 300–400ms,
     * around the clock, whether anything is playing or not:
     *
     * <pre>
     *   17:56:24.257 rec start … pack:com.google.android.as
     *   17:56:24.929 rec stop  … pack:com.google.android.as
     *   17:56:25.951 rec start …
     * </pre>
     *
     * 1500ms is comfortably past its longest hold and well short of any real
     * dictation, which runs for seconds. The first version of this class used
     * 350ms, which cleared the short bursts and not this.
     */
    static final long ENGAGE_MS = 1500;

    /**
     * How long it must fall back to the baseline before the hold lets go.
     *
     * Longer than the recogniser's gaps for the same reason, so a hold that
     * really did engage is not dropped between two of its cycles.
     */
    static final long RELEASE_MS = 800;

    /**
     * Concurrent recordings that mean somebody else has joined in.
     *
     * The faster half of the answer, and the one that keeps barge-in usable.
     * A phone whose baseline is one permanent recogniser tells you a great
     * deal by going to two: dictation, a call, or a voice session started
     * while the recogniser carries on underneath it. There is no waiting to
     * do in that case — the count itself is the evidence.
     */
    static final int CROWD = 2;

    /** When the current unbroken run of "something is recording" began. */
    private long openedAt = 0;
    private boolean anyOpen = false;
    private boolean crowded = false;

    private boolean steady = false;
    /** When the answer below last disagreed with {@link #steady}. */
    private long disagreedAt = 0;
    private boolean primed = false;

    /**
     * Fold in what the watch just saw.
     *
     * @param count how many recordings are active right now
     * @param nowMs a monotonic clock in milliseconds
     * @return the debounced answer: what the rest of the app should believe
     */
    boolean update(int count, long nowMs) {
        boolean open = count > 0;
        if (!open) {
            openedAt = nowMs;
        } else if (!anyOpen) {
            openedAt = nowMs;
            runStarts[runIndex] = nowMs;
            runIndex = (runIndex + 1) % runStarts.length;
        }
        anyOpen = open;
        crowded = count >= CROWD;

        if (!primed) watchingSince = nowMs;
        boolean person = person(nowMs);
        if (!primed) {
            primed = true;
            watchingSince = nowMs;
            steady = person;
            disagreedAt = nowMs;
            return steady;
        }
        if (person == steady) {
            disagreedAt = nowMs;
        } else if (person) {
            // Engaging is already gated by ENGAGE_MS (or immediate, for a
            // crowd), so there is nothing further to wait for.
            steady = true;
            disagreedAt = nowMs;
        } else if (nowMs - disagreedAt >= RELEASE_MS) {
            steady = false;
            disagreedAt = nowMs;
        }
        return steady;
    }

    /**
     * Is what is happening right now a person rather than the baseline?
     *
     * <b>Which rule applies depends on the phone.</b> The duration rule — a
     * single recording held past {@link #ENGAGE_MS} — is right for a device
     * whose microphone is normally idle, and useless on one where something
     * samples it constantly. p8a is the second kind, and worse than it first
     * looked: while our own speech plays, the recogniser holds the microphone
     * for about two seconds at a time, presumably trying to identify the
     * "music" it can hear. No duration threshold separates that from
     * dictation, because it is the same duration.
     *
     * What still separates them is arithmetic. A phone with a permanent
     * baseline says something by going from one recording to two: the baseline
     * plus somebody. So once this class has watched the microphone cycle —
     * see {@link #cycling} — it stops believing duration and waits for company.
     */
    private boolean person(long nowMs) {
        if (crowded) return true;
        if (cycling(nowMs)) return false;
        return anyOpen && nowMs - openedAt >= ENGAGE_MS;
    }

    /**
     * Does this phone have something sampling the microphone on its own?
     *
     * Measured rather than configured, because the answer differs per device
     * and per settings screen: Now Playing, hotword detection and whatever
     * else ships in the assistant all behave this way, and any of them can be
     * turned off tomorrow. Three separate recordings inside a minute is not
     * something a person does, and a phone that stops doing it gets the
     * duration rule back within the minute.
     */
    private boolean cycling(long nowMs) {
        // Not yet known, which must not read as "no". A restart empties this
        // history, and for the first minute afterwards the duration rule would
        // engage on the very cycling it is waiting to detect — which is
        // precisely what the first deployment did, twice, in the seconds after
        // the app came up. Assume company is required until the phone has had
        // time to show otherwise: a minute of barge-in waiting for a second
        // recording is a far smaller cost than a minute of speech in pieces.
        if (nowMs - watchingSince < BASELINE_WINDOW_MS) return true;
        int recent = 0;
        for (long start : runStarts) {
            if (start > 0 && nowMs - start <= BASELINE_WINDOW_MS) recent++;
        }
        return recent >= BASELINE_RUNS;
    }

    /** How far back to look for a cycling baseline. */
    static final long BASELINE_WINDOW_MS = 60000;
    /** How many separate recordings inside that window make it a baseline. */
    static final int BASELINE_RUNS = 3;

    private final long[] runStarts = new long[BASELINE_RUNS];
    private int runIndex = 0;
    /** When this watch started, for the "not yet known" case above. */
    private long watchingSince = Long.MIN_VALUE / 2;

    /** Convenience for callers with a boolean and no count. */
    boolean update(boolean recording, long nowMs) {
        return update(recording ? 1 : 0, nowMs);
    }

    /** The debounced answer, without folding in a new sample. */
    boolean steady() {
        return steady;
    }

    /**
     * When this should be asked again for a pending change to land, or -1.
     *
     * The watch is event-driven and the events stop: a recording that starts
     * and is never followed by another event would otherwise leave a hold
     * un-engaged, and one that ends in silence would leave it stuck on.
     */
    long pendingInMs(long nowMs) {
        if (!primed) return -1;
        if (anyOpen && !crowded && !steady) {
            long left = openedAt + ENGAGE_MS - nowMs;
            return Math.max(0, left);
        }
        if (steady && !person(nowMs)) {
            return Math.max(0, disagreedAt + RELEASE_MS - nowMs);
        }
        return -1;
    }
}
