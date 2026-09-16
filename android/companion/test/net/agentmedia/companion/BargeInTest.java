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
        f += aBlinkingRecordingIsStillAConversation();
        f += theBaselineCannotStealAConversationsGap();
        f += aConversationEndsOnceItsRecordingStaysGone();
        f += theOtherSideSpeakingExtendsTheGrace();
        f += afterAConversationDictationIsJudgedFresh();
        f += aDictationInsideASessionIsStillADictation();
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

    /**
     * The 2026-09-17 trace: Live's recording (one riid) went quiet and came
     * back every two to five seconds, and every quiet used to end the session
     * — which released the held reply into the conversation and paused Live.
     */
    private static int aBlinkingRecordingIsStillAConversation() {
        BargeIn b = new BargeIn();
        b.onMic(true, BargeIn.VOICE_COMMUNICATION, 1000);
        int f = is(true, b.voiceSession(), "a conversation, at the first poll");
        long t = 1000;
        for (int i = 0; i < 12; i++) {
            b.onMic(false, -1, t + 2000);      // the blink
            b.onTick(t + 2500);
            f += is(true, b.voiceSession(), "still a conversation while it blinks");
            b.onMic(true, BargeIn.VOICE_COMMUNICATION, t + 3000);
            t += 3000;
        }
        return f + is(true, b.voiceSession(), "and after a dozen of them");
    }

    /**
     * com.google.android.as opens VOICE_RECOGNITION constantly on p8a, so it
     * lands in the gaps between Live's recordings. Letting that reclassify the
     * episode is the element-zero bug arriving by another door.
     */
    private static int theBaselineCannotStealAConversationsGap() {
        BargeIn b = new BargeIn();
        b.onMic(true, BargeIn.VOICE_COMMUNICATION, 1000);
        b.onMic(false, -1, 3000);
        b.onMic(true, DICTATION, 3500);        // the recogniser, mid-conversation
        int f = is(true, b.voiceSession(), "the baseline does not end the session");
        f += is(false, b.holding(3600), "and does not start a dictation hold");
        b.onMic(false, -1, 4000);
        b.onMic(true, BargeIn.VOICE_COMMUNICATION, 4200);
        return f + is(true, b.voiceSession(), "Live comes back to the same episode");
    }

    /** It does have to end, or the hold outlives the conversation. */
    private static int aConversationEndsOnceItsRecordingStaysGone() {
        BargeIn b = new BargeIn();
        b.onMic(true, BargeIn.VOICE_COMMUNICATION, 1000);
        b.onMic(false, -1, 5000);
        b.onTick(5000 + BargeIn.SESSION_GRACE_MS - 1);
        int f = is(true, b.voiceSession(), "not a millisecond early");
        b.onTick(5000 + BargeIn.SESSION_GRACE_MS);
        return f + is(false, b.voiceSession(), "and over once the grace passes");
    }

    /**
     * David's report: Sam "continued to speak over Cece". With the mic shut
     * and another app audible, the conversation is at its clearest — that is
     * the other side talking — so the grace restarts rather than running out.
     */
    private static int theOtherSideSpeakingExtendsTheGrace() {
        BargeIn b = new BargeIn();
        b.onMic(true, BargeIn.VOICE_COMMUNICATION, 1000);
        b.onMic(false, -1, 2000);
        b.onFocus(FocusPolicy.LOSS_TRANSIENT, 2500);       // Cece starts talking
        b.onTick(2500 + BargeIn.SESSION_GRACE_MS * 2);
        int f = is(true, b.voiceSession(), "a long turn of hers is not the end");
        b.onFocus(FocusPolicy.GAIN, 2500 + BargeIn.SESSION_GRACE_MS * 2);
        b.onTick(2500 + BargeIn.SESSION_GRACE_MS * 3);
        return f + is(false, b.voiceSession(),
                      "but the silence after it does run out");
    }

    /** And the grace must not leak into whatever holds the mic next. */
    private static int afterAConversationDictationIsJudgedFresh() {
        BargeIn b = new BargeIn();
        b.onMic(true, BargeIn.VOICE_COMMUNICATION, 1000);
        b.onMic(false, -1, 2000);
        b.onTick(2000 + BargeIn.SESSION_GRACE_MS);
        int f = is(false, b.voiceSession(), "the session is over");
        b.onMic(true, DICTATION, 60_000);
        return f + is(true, b.holding(60_000),
                      "so a later dictation holds the audio down again");
    }

    /**
     * David, 2026-09-17: a reply arrived while he was using Gboard voice
     * typing during a Live session. The grace was right — the conversation was
     * still on — but the microphone was his dictation's, and the dictation
     * hold needs that answer, not the latch's.
     */
    private static int aDictationInsideASessionIsStillADictation() {
        BargeIn b = new BargeIn();
        b.onMic(true, BargeIn.VOICE_COMMUNICATION, 1000);
        int f = is(true, b.conversationMic(), "Live has the mic");
        b.onMic(false, -1, 2000);
        f += is(false, b.conversationMic(), "nothing has it in the gap");
        f += is(true, b.voiceSession(), "though the conversation is still on");
        b.onMic(true, DICTATION, 2500);        // he starts voice typing
        f += is(false, b.conversationMic(),
                "voice typing in the gap is not the conversation's mic");
        f += is(true, b.voiceSession(), "and still does not end the session");
        b.onMic(false, -1, 6000);
        b.onMic(true, BargeIn.VOICE_COMMUNICATION, 6200);
        return f + is(true, b.conversationMic(), "Live takes it back");
    }

    private static int is(boolean want, boolean got, String what) {
        if (want == got) return 0;
        System.out.println("FAIL " + what + ": want " + want + " got " + got);
        return 1;
    }
}
