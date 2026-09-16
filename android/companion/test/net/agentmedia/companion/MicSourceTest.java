package net.agentmedia.companion;

/**
 * The list-picking that hid Claude Live from this app for a month.
 *
 * Every case is a shape p8a actually produces: the recogniser's endless
 * VOICE_RECOGNITION baseline, a real conversation arriving on top of it, and a
 * device that will not say what a recording is for.
 */
public class MicSourceTest {

    /** MediaRecorder.AudioSource.VOICE_RECOGNITION — the p8a baseline. */
    private static final int VOICE_RECOGNITION = 6;
    /** MediaRecorder.AudioSource.MIC — a plain recording. */
    private static final int MIC = 1;
    private static final int VOICE_COMMUNICATION = BargeIn.VOICE_COMMUNICATION;

    public static void main(String[] args) {
        int failures = 0;

        failures += is("nothing recording is nothing",
                MicSource.NONE, MicSource.pick(new int[0]));
        failures += is("and neither is no list at all",
                MicSource.NONE, MicSource.pick(null));

        failures += is("one dictation is a dictation",
                VOICE_RECOGNITION, MicSource.pick(new int[] { VOICE_RECOGNITION }));
        failures += is("one conversation is a conversation",
                VOICE_COMMUNICATION, MicSource.pick(new int[] { VOICE_COMMUNICATION }));

        // The bug, in one line. p8a's recogniser is always already in the list,
        // so the conversation is never element zero.
        failures += is("a conversation opening second still wins",
                VOICE_COMMUNICATION,
                MicSource.pick(new int[] { VOICE_RECOGNITION, VOICE_COMMUNICATION }));
        failures += is("and so does one buried under a baseline three deep",
                VOICE_COMMUNICATION,
                MicSource.pick(new int[] {
                        VOICE_RECOGNITION, MIC, VOICE_RECOGNITION, VOICE_COMMUNICATION }));
        failures += is("order is not the point — first also wins",
                VOICE_COMMUNICATION,
                MicSource.pick(new int[] { VOICE_COMMUNICATION, VOICE_RECOGNITION }));

        // Without a conversation, the old answer is the right answer: the first
        // recording, which is what the rest of the app has always been tuned to.
        failures += is("two dictations are still a dictation",
                VOICE_RECOGNITION,
                MicSource.pick(new int[] { VOICE_RECOGNITION, VOICE_RECOGNITION }));
        failures += is("and the first of a mixed pair decides",
                MIC, MicSource.pick(new int[] { MIC, VOICE_RECOGNITION }));

        // A device that redacts the source still says a recording is open. An
        // unreadable entry must not become the answer, and must not stop the
        // readable ones behind it from being it.
        failures += is("a redacted entry does not shadow what follows",
                VOICE_RECOGNITION,
                MicSource.pick(new int[] { MicSource.UNREADABLE, VOICE_RECOGNITION }));
        failures += is("and never hides a conversation",
                VOICE_COMMUNICATION,
                MicSource.pick(new int[] { MicSource.UNREADABLE, VOICE_COMMUNICATION }));
        failures += is("all redacted is nothing we can decide on",
                MicSource.NONE,
                MicSource.pick(new int[] { MicSource.UNREADABLE, MicSource.UNREADABLE }));

        System.out.println(failures == 0 ? "MicSourceTest ok"
                                         : "MicSourceTest FAILED (" + failures + ")");
        if (failures > 0) System.exit(1);
    }

    private static int is(String what, int want, int got) {
        boolean ok = want == got;
        System.out.println((ok ? "  ok   " : "  FAIL ") + what
                + (ok ? "" : " (wanted " + want + ", got " + got + ")"));
        return ok ? 0 : 1;
    }
}
