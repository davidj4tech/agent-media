package net.agentmedia.companion;

/**
 * Which of the open recordings decides how an open microphone is treated.
 *
 * Android hands us a <em>list</em> of active recording configurations, and the
 * app long read element zero of it. On a phone whose microphone is never idle
 * that is the wrong element: {@code com.google.android.as} cycles
 * VOICE_RECOGNITION around the clock on p8a, so a Claude Live session opening
 * <em>second</em> never got a look in. The list led with {@code src=6},
 * {@link BargeIn} called the episode dictation, {@code voiceSession()} stayed
 * false, and the whole {@code live_mode=hold} tier — Sam waiting, the card,
 * the toast beside it — never ran once. What David heard instead was Cece
 * talking over a Live session, chopped by the dictation hold. Observed
 * 2026-09-16, fixed by this class.
 *
 * The rule: <b>a conversation wins, whoever opened first.</b>
 * VOICE_COMMUNICATION is the strongest claim anything can make on the mic — it
 * *means* a two-way conversation — so no amount of baseline noise alongside it
 * changes what the episode is. Anything else falls back to the first readable
 * source, which is all the old code was ever trying to say.
 *
 * android.*-free, so test/run.sh covers it. MicWatch owns the android.* half:
 * it turns the configuration list into sources and hands them here. The
 * companion of {@link MicSteady}, which does the same for the <em>count</em>.
 */
final class MicSource {

    /** Nothing is recording, or nothing readable is. */
    static final int NONE = -1;

    /**
     * A configuration whose source we could not read. Not the same as NONE: a
     * device that redacts the source still tells us a recording is open, and a
     * redacted entry must not become the answer for the ones we *can* read.
     */
    static final int UNREADABLE = -2;

    private MicSource() { }

    /**
     * The deciding source among those open, or {@link #NONE}.
     *
     * @param sources one entry per active recording, in the order Android
     *     listed them, {@link #UNREADABLE} where the source could not be read.
     */
    static int pick(int[] sources) {
        if (sources == null) return NONE;
        int first = NONE;
        for (int src : sources) {
            if (src == BargeIn.VOICE_COMMUNICATION) return src;
            if (src == UNREADABLE || src == NONE) continue;
            if (first == NONE) first = src;
        }
        return first;
    }
}
