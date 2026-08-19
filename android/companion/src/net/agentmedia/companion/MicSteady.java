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

    /**
     * How many recordings count, given what the phone says about each.
     *
     * A silenced recording is not somebody listening — Android opens the
     * session and feeds it zeros — so on its own it counts for nothing. That
     * is what lets an app-op-blocked sampler sit there permanently without
     * reading as a person holding the microphone forever.
     *
     * <b>But it is still somebody else at the microphone, and that is exactly
     * the signal {@link #CROWD} is waiting for.</b> Android silences whichever
     * recorder loses priority, so when David presses Gboard's mic button the
     * background sampler goes silent and the heard count never leaves one:
     *
     * <pre>
     *   22:11:40.171 rec start  … not silenced pack:…inputmethod.latin
     *   22:11:40.670 rec update … silenced     pack:…inputmethod.latin
     *   22:11:40.693 rec update … not silenced pack:com.google.android.as
     * </pre>
     *
     * On a cycling phone the duration rule is off and the crowd is the only
     * way in, so that dictation was invisible and Sam talked straight through
     * it — 2026-08-19, David: "you're meant to pause when I press the mic
     * button on the gboard". Counted as company only alongside something
     * heard, never on its own.
     */
    static int counting(int heard, int silenced) {
        return heard == 0 ? 0 : heard + silenced;
    }

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
            sawRunStart(nowMs);
        }
        anyOpen = open;
        crowded = count >= CROWD;
        if (crowded) lastCrowdAt = nowMs;
        if (!open) lastZeroAt = nowMs;

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
        // Still them, even though the count just fell back to one.
        //
        // The count does not stay at two for as long as a person talks: the
        // baseline recogniser cycles underneath the dictation, so what the
        // watch sees is 2, 1, 2, 1 about once a second — and a hold that
        // followed that literally engaged and released at the same rate.
        // David's words: "when audio is paused for mic detection it keeps on
        // cutting in and cutting out."
        //
        // What tells the two apart is the microphone actually closing.
        // Dictation holds it open for as long as it runs; the baseline lets
        // it go every cycle. So while the mic has not closed since we last
        // saw company, the company is still there.
        if (lastCrowdAt > Long.MIN_VALUE / 2) {
            if (nowMs - lastCrowdAt <= CROWD_GRACE_MS) return true;
            if (anyOpen && lastZeroAt < lastCrowdAt) return true;
        }
        if (cycling(nowMs)) return false;
        return anyOpen && nowMs - openedAt >= engageMs(nowMs);
    }

    /**
     * How long a lone recording must run before it is a person, here, now.
     *
     * {@link #ENGAGE_MS} is sized to clear a sampler's longest hold, and on a
     * phone that has never shown one it is 1.5s of Sam talking into David's
     * dictation for no reason — "it was also a bit slow to stop and got some
     * of your texts in my dictation" (2026-08-19). With the sampler blocked
     * there is nothing to out-wait, so the floor drops to the shortest run
     * that is plainly deliberate.
     */
    private long engageMs(long nowMs) {
        return everCycled ? ENGAGE_MS : QUIET_ENGAGE_MS;
    }

    /**
     * The floor on a phone whose microphone nothing samples.
     *
     * Not zero: a recording that opens and shuts inside a couple of hundred
     * milliseconds is an app checking, not a person speaking, and barge-in
     * that fires on those would pause Sam at every notification-adjacent
     * flicker.
     */
    static final long QUIET_ENGAGE_MS = 400;

    /**
     * How long a crowd is remembered after the count falls back.
     *
     * Covers the recogniser's own gap — about a second on p8a — so a hold
     * survives the moment when the baseline half of the crowd lets go, without
     * outliving a person by anything a listener would notice.
     */
    static final long CROWD_GRACE_MS = 1500;

    private long lastCrowdAt = Long.MIN_VALUE / 2;
    private long lastZeroAt = Long.MIN_VALUE / 2;

    /**
     * Does this phone have something sampling the microphone on its own?
     *
     * Measured rather than configured, because the answer differs per device
     * and per settings screen: Now Playing, hotword detection and whatever
     * else ships in the assistant all behave this way, and any of them can be
     * turned off tomorrow.
     *
     * <b>A burst, not a tally.</b> This used to be "three recordings inside a
     * minute", and three recordings inside a minute is something DAVID does —
     * dictate, read the reply, dictate again. On 2026-08-19, with the sampler
     * blocked and nothing else at the microphone, his own third dictation
     * taught this class that the phone cycles; the duration rule switched off,
     * no company was ever coming, and barge-in stopped working for the rest of
     * the evening: "pressing the mic paused your speech the first few times
     * but failed to work after that."
     *
     * What a sampler does and a person cannot is repeat immediately: p8a's
     * recogniser leaves the microphone alone for 300–900ms between holds,
     * where a person leaves it alone for as long as it takes to read an
     * answer. So the evidence is three runs inside {@link #BURST_MS}, and it
     * is remembered for {@link #BASELINE_WINDOW_MS} afterwards — a phone that
     * stops sampling gets the duration rule back within the minute.
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
        return nowMs - cycledAt <= BASELINE_WINDOW_MS;
    }

    /** Called as each run opens: is this phone sampling its own microphone? */
    private void sawRunStart(long nowMs) {
        runStarts[runIndex] = nowMs;
        runIndex = (runIndex + 1) % runStarts.length;
        long oldest = Long.MAX_VALUE;
        for (long start : runStarts) {
            if (start <= 0) return;             // fewer than BASELINE_RUNS yet
            if (start < oldest) oldest = start;
        }
        if (nowMs - oldest <= BURST_MS) {
            cycledAt = nowMs;
            everCycled = true;
        }
    }

    /** How long a cycling verdict is remembered once the bursts stop. */
    static final long BASELINE_WINDOW_MS = 60000;
    /** How many separate recordings make a burst. */
    static final int BASELINE_RUNS = 3;
    /**
     * How close together those runs must be.
     *
     * p8a's recogniser cycles about once a second, so three of its runs span
     * five or six seconds. Three dictations span a conversation.
     */
    static final long BURST_MS = 15000;

    /** When a burst was last seen, and whether one ever was. */
    private long cycledAt = Long.MIN_VALUE / 2;
    private boolean everCycled = false;

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
            long left = openedAt + engageMs(nowMs) - nowMs;
            return Math.max(0, left);
        }
        if (steady && !person(nowMs)) {
            return Math.max(0, disagreedAt + RELEASE_MS - nowMs);
        }
        return -1;
    }
}
