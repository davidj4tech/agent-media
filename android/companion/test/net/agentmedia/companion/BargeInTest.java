package net.agentmedia.companion;

/**
 * Telling a dictation from a conversation.
 *
 * Android names it outright — VOICE_RECOGNITION against VOICE_COMMUNICATION —
 * and the rest of these cover the recording whose source we do not recognise,
 * where what else is audible is the only evidence there is.
 */
public final class BargeInTest {

    /** MediaRecorder.AudioSource.VOICE_RECOGNITION — what Gboard dictation uses. */
    private static final int DICTATION = 6;

    public static void main(String[] args) {
        int f = 0;
        f += aVoiceSessionNamesItselfAtTheFirstPoll();
        f += anUnknownSourceIsTreatedAsDictation();
        f += dictationHoldsTheAudioDown();
        f += aVoiceSessionDoesNot();
        f += aNotificationDingIsNotAConversation();
        f += aPauseForBreathDoesNotUndoIt();
        f += closingTheMicClearsTheLatch();
        f += focusWithTheMicShutDecidesNothing();
        if (f > 0) {
            System.out.println(f + " failure(s)");
            System.exit(1);
        }
        System.out.println("BargeInTest: ok");
    }

    /**
     * The signal that settled it: Android says which kind of recording this is.
     * VOICE_COMMUNICATION means a two-way conversation, and it arrives with the
     * very first poll — no timing floor, no waiting to hear the other side.
     */
    private static int aVoiceSessionNamesItselfAtTheFirstPoll() {
        BargeIn b = new BargeIn();
        b.onMic(true, BargeIn.VOICE_COMMUNICATION, 1000);
        return is(false, b.holding(1000), "a Live session never holds the audio")
             + is(false, b.holding(90_000), "and does not start to later");
    }

    /** A source we do not recognise pauses Sam: the cheap mistake, not the dear one. */
    private static int anUnknownSourceIsTreatedAsDictation() {
        BargeIn b = new BargeIn();
        b.onMic(true, 99, 1000);
        return is(true, b.holding(1000), "unknown reads as someone talking");
    }

    /** Push-to-talk: nothing else makes a sound, so the mic is what it says. */
    private static int dictationHoldsTheAudioDown() {
        BargeIn b = new BargeIn();
        b.onMic(true, DICTATION, 1000);
        return is(true, b.holding(1000), "an open mic holds by default")
             + is(true, b.holding(60_000), "and keeps holding while silent");
    }

    /** The other side of the conversation speaks, and the mic stops deciding. */
    private static int aVoiceSessionDoesNot() {
        BargeIn b = new BargeIn();
        b.onMic(true, DICTATION, 1000);
        b.onFocus(FocusPolicy.LOSS_TRANSIENT, 2000);       // Cece starts talking
        int f = is(true, b.holding(2500), "half a second in, still undecided");
        f += is(false, b.holding(2000 + BargeIn.FOREIGN_AUDIO_MIN_MS),
                "once she has spoken for long enough, this is a conversation");
        b.onFocus(FocusPolicy.GAIN, 6000);                 // she finishes
        return f + is(false, b.holding(9000),
                      "and stays one while the mic is open");
    }

    /** A message arriving mid-dictation must not un-pause Sam. */
    private static int aNotificationDingIsNotAConversation() {
        BargeIn b = new BargeIn();
        b.onMic(true, DICTATION, 1000);
        b.onFocus(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, 2000);
        b.onFocus(FocusPolicy.GAIN, 2000 + 400);           // a ding is short
        return is(true, b.holding(5000), "400ms of audio is not an utterance");
    }

    /** Two short dings still add up to nothing much; two utterances do not. */
    private static int aPauseForBreathDoesNotUndoIt() {
        BargeIn b = new BargeIn();
        b.onMic(true, DICTATION, 0);
        b.onFocus(FocusPolicy.LOSS_TRANSIENT, 1000);
        b.onFocus(FocusPolicy.GAIN, 1000 + 700);           // one utterance
        int f = is(true, b.holding(2000), "700ms alone is under the floor");
        b.onFocus(FocusPolicy.LOSS_TRANSIENT, 3000);
        b.onFocus(FocusPolicy.GAIN, 3000 + 700);           // and another
        return f + is(false, b.holding(4000),
                      "but they bank, so a conversation is recognised");
    }

    /** Push-to-talk closes the mic between utterances: every one starts fresh. */
    private static int closingTheMicClearsTheLatch() {
        BargeIn b = new BargeIn();
        b.onMic(true, DICTATION, 0);
        b.onFocus(FocusPolicy.LOSS_TRANSIENT, 100);
        b.onFocus(FocusPolicy.GAIN, 100 + 5000);
        int f = is(false, b.holding(6000), "a conversation, correctly");
        b.onMic(false, -1, 7000);
        b.onMic(true, DICTATION, 8000);                               // a new dictation
        return f + is(true, b.holding(8000),
                      "the next recording is judged on its own evidence");
    }

    private static int focusWithTheMicShutDecidesNothing() {
        BargeIn b = new BargeIn();
        b.onFocus(FocusPolicy.LOSS, 1000);
        b.onFocus(FocusPolicy.GAIN, 20_000);
        return is(false, b.holding(20_000), "nothing to hold for");
    }

    private static int is(boolean want, boolean got, String what) {
        if (want == got) return 0;
        System.out.println("FAIL " + what + ": want " + want + " got " + got);
        return 1;
    }
}
