package net.agentmedia.companion;

/**
 * Is this open microphone someone talking over Sam, or something holding the
 * mic for a conversation?
 *
 * <h4>The problem</h4>
 *
 * Android tells us that a recording session is open. It does not tell us who
 * opened it, or whether anyone is speaking into it. Two very different things
 * look identical:
 *
 * <ul>
 *   <li><b>Push-to-talk.</b> The mic opens when David starts dictating and
 *       closes when he stops. Pausing speech for the whole of it is exactly
 *       right.</li>
 *   <li><b>A voice session.</b> The mic opens once and stays open for the
 *       length of the conversation. Pausing speech for the whole of it means
 *       Sam is silent for the evening.</li>
 * </ul>
 *
 * The first version separated them by duration — a recording open longer than
 * two minutes stopped counting. That is a guess about what kind of thing is
 * holding the mic, and it costs two minutes of silence before it guesses.
 *
 * <h4>The signal that settled it</h4>
 *
 * Android names the difference itself, and we were inferring what it was
 * willing to say. A Gboard dictation records with
 * {@code MediaRecorder.AudioSource.VOICE_RECOGNITION} (6); a Claude Live
 * session records with {@code VOICE_COMMUNICATION} (7) — captured on p8a on
 * 2026-08-15, unredacted. VOICE_COMMUNICATION *means* a two-way conversation,
 * so it is not a heuristic that happens to work, it is the API answering the
 * actual question, at the first poll, with no timing floor.
 *
 * Everything else here is the fallback for a source we do not recognise, which
 * is why it stays. A recording we cannot classify is treated as dictation —
 * being wrong that way pauses Sam for a moment, and the other way lets him talk
 * over David.
 *
 * <h4>The corroborating signal</h4>
 *
 * <b>Dictation makes no sound; a conversation does.</b> Gboard's voice typing
 * plays nothing at all, so while David dictates the output stays ours. The
 * other side of a voice session has to speak, and speaking takes audio focus —
 * which this app watches closely, because reacting to it is the app's whole
 * job. Our own playback never causes it: mpv plays *under* the focus we hold,
 * so a loss means some other app took the output.
 *
 * So: mic open and something else has been audible ⇒ a conversation, and the
 * mic stops being a reason to hold the audio down. The focus policy takes over
 * from there, which is the better régime anyway — it pauses Sam while the other
 * voice speaks and hands him back when it stops, instead of silencing him for
 * the length of the session.
 *
 * <h4>Why a duration floor on the audio</h4>
 *
 * A notification ding also takes focus. Left at "any loss at all", a message
 * arriving mid-dictation would read as a conversation and un-pause Sam while
 * David was still talking. So the other app has to hold the output for
 * {@link #FOREIGN_AUDIO_MIN_MS}, which an utterance clears easily and a ding
 * does not.
 *
 * Latched while the mic stays open: a conversation does not become a dictation
 * because the other side paused for breath. The mic going quiet clears it, and
 * since push-to-talk closes the recording between utterances, dictation always
 * starts from a clean slate.
 *
 * android.*-free, so the deciding is testable on the build host. The service
 * feeds it; {@link MicWatch} owns the mic half and {@link FocusControl} the
 * other.
 */
final class BargeIn {

    /** Where to say what was decided. Set by the service; silent under test. */
    interface Log {
        void line(String message);
    }

    private Log log = message -> { };

    void logTo(Log sink) {
        if (sink != null) log = sink;
    }

    /**
     * How long another app must hold the output before this reads as a
     * conversation. Above a notification ding, below any real utterance.
     */
    static final long FOREIGN_AUDIO_MIN_MS = 1200L;

    /**
     * {@code MediaRecorder.AudioSource.VOICE_COMMUNICATION}. Duplicated rather
     * than imported to keep this class host-testable, like FocusPolicy's focus
     * constants. A recording opened with this is a conversation by definition.
     *
     * A phone call uses it too, and that is correct: a call is not someone
     * barging in on Sam either, and call_guard already handles calls by their
     * own route, with the policy calls need (no auto-resume).
     */
    static final int VOICE_COMMUNICATION = 7;

    private boolean micOpen = false;
    /** Latched once the mic-open episode has heard another app speak. */
    private boolean conversation = false;
    /** When the current foreign-audio run began; 0 = nothing else is audible. */
    private long foreignSince = 0L;
    /** Foreign audio already banked in this mic-open episode. */
    private long foreignMs = 0L;

    /** The mic opened or closed. Closing ends the episode and clears the latch. */
    void onMic(boolean active, int source, long now) {
        if (active == micOpen) return;
        micOpen = active;
        if (!active) {
            conversation = false;
            voiceSession = false;
            foreignSince = 0L;
            foreignMs = 0L;
        } else {
            // A new episode banks nothing, but audio already playing when the
            // mic opened counts from the moment it opened: if David starts
            // dictating into a room where something else is talking, the thing
            // that is talking is still evidence about what this recording is.
            foreignMs = 0L;
            if (foreignSince != 0L) foreignSince = now;
            voiceSession = source == VOICE_COMMUNICATION;
            if (voiceSession) {
                conversation = true;
                conversationWhy = "conversation (VOICE_COMMUNICATION)";
                log.line("barge-in: recording opened as VOICE_COMMUNICATION — "
                        + "a conversation, not someone talking over Sam");
            }
        }
    }

    /** A focus callback. Anything but GAIN means another app has the output. */
    void onFocus(int change, long now) {
        if (change == FocusPolicy.GAIN) {
            if (foreignSince != 0L) {
                foreignMs += Math.max(0L, now - foreignSince);
                foreignSince = 0L;
            }
        } else if (foreignSince == 0L) {
            foreignSince = now;
        }
        check(now);
    }

    /**
     * Should an open mic hold the audio down right now?
     *
     * False when the mic is shut (there is nothing to hold for) and false once
     * this episode has been identified as a conversation.
     */
    boolean holding(long now) {
        check(now);
        return micOpen && !conversation;
    }

    /** Why, for the readout — this is the line a human reads over ssh. */
    String why(long now) {
        if (!micOpen) return "mic shut";
        if (conversation) return conversationWhy;
        long heard = audible(now);
        return heard > 0 ? "dictation (" + heard + "ms of other audio, under "
                           + FOREIGN_AUDIO_MIN_MS + ")"
                         : "dictation (nothing else audible)";
    }

    private void check(long now) {
        if (micOpen && !conversation && audible(now) >= FOREIGN_AUDIO_MIN_MS) {
            conversation = true;
            conversationWhy = "conversation (another app spoke)";
            log.line("barge-in: another app has been audible for "
                    + FOREIGN_AUDIO_MIN_MS + "ms with the mic open — reading "
                    + "this as a conversation, not someone talking over Sam");
        }
    }

    /** True while the open recording is a VOICE_COMMUNICATION one. */
    private boolean voiceSession = false;

    /** Is a two-way voice session holding the mic right now? */
    boolean voiceSession() { return micOpen && voiceSession; }

    /** Which evidence decided it, for the readout. */
    private String conversationWhy = "conversation";

    /** Foreign audio in this episode, including a run still in progress. */
    private long audible(long now) {
        long open = (foreignSince == 0L) ? 0L : Math.max(0L, now - foreignSince);
        return foreignMs + open;
    }
}
