package net.agentmedia.companion;

import java.util.ArrayDeque;
import java.util.Deque;

/**
 * How often the dictation hold engages, so a hold that is not dictation can
 * say so.
 *
 * <h4>What this is watching for</h4>
 *
 * {@link MicSteady} separates a person from a probe by duration, and on p8a
 * that works only while {@code com.google.android.as} is blocked from the
 * microphone: blocked, Android opens its session and feeds it zeros, and a
 * silenced recording counts for nothing on its own. Unblocked, the same
 * recogniser holds the mic for ten seconds at a time, every half minute or so,
 * around the clock — indistinguishable, by duration, from David dictating.
 * Every one of those pauses Sam mid-sentence and resumes him a few seconds
 * later:
 *
 * <pre>
 *   10:17:54  dictation: pausing the clip in flight
 *   10:18:01  focus: resume speech (in-app player)
 *   10:18:22  dictation: pausing the clip in flight
 * </pre>
 *
 * The block reverts on its own — twice now, hours later, with nothing said —
 * and both times the symptom reached David as "TTS keeps pausing" rather than
 * as anything about a microphone. Nothing in the stack disagreed: every
 * component was healthy and doing exactly what it was told.
 *
 * <h4>Why frequency, and not the app-op</h4>
 *
 * Reading the app-op back would name the cause precisely, and an ordinary app
 * cannot: {@code appops} needs a shell. What this app can see is its own
 * behaviour, and the giveaway is in the rate. A person dictates a handful of
 * times an hour. The recogniser fires every thirty to sixty seconds, which is
 * an order of magnitude more, and no amount of real dictation looks like that.
 *
 * So this counts engagements over a rolling hour and lets {@code /state} — and
 * through it {@code media doctor} — say the thing that was missing: the hold is
 * firing far too often to be a person, whatever the reason.
 *
 * {@code android.*}-free, so {@code test/run.sh} covers it.
 */
final class HoldRate {

    /** The window the rate is measured over. */
    static final long WINDOW_MS = 60 * 60 * 1000;

    /**
     * Engagements an hour past which this is not somebody talking.
     *
     * Set at 20 on the reasoning that the recogniser's every-40s cycle clears
     * it in under fifteen minutes. It does — but the recogniser does not cycle
     * evenly, and the hour David spent telling me speech was still stopping
     * mid-sentence measured **13**, sat quietly under the line, and said
     * nothing. A check that stays silent through the exact event it was
     * written for is not a check.
     *
     * So: ten. Dictating ten times in one hour is a lot of talking to a
     * keyboard and still leaves the line clear of an ordinary day, while the
     * cost of being wrong is one line in `media doctor` — against another
     * afternoon of "TTS is broken" for being wrong the other way.
     */
    static final int TOO_MANY = 10;

    private final Deque<Long> engagements = new ArrayDeque<Long>();

    /** The hold just engaged. */
    void engaged(long now) {
        engagements.addLast(Long.valueOf(now));
        prune(now);
    }

    /** Engagements within the last hour. */
    int recent(long now) {
        prune(now);
        return engagements.size();
    }

    /** True when the rate is past anything a person produces. */
    boolean suspicious(long now) {
        return recent(now) >= TOO_MANY;
    }

    /**
     * The line a human reads over ssh, or "" while the rate is ordinary.
     *
     * Names the likely cause without claiming certainty about it: this class
     * can see the rate and cannot see the app-op, and saying more than it knows
     * is how a health check starts being ignored.
     */
    String problem(long now) {
        int n = recent(now);
        if (n < TOO_MANY) return "";
        return "the dictation hold engaged " + n + " times in the last hour — "
                + "far more than dictation, so speech is being paused by the "
                + "phone's own recogniser holding the mic. Check whether "
                + "com.google.android.as is blocked from RECORD_AUDIO; the "
                + "block reverts on its own.";
    }

    private void prune(long now) {
        while (!engagements.isEmpty()
                && now - engagements.peekFirst().longValue() > WINDOW_MS) {
            engagements.removeFirst();
        }
    }
}
