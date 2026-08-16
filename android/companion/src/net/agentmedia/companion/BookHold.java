package net.agentmedia.companion;

/**
 * Stop the book while David is in a conversation.
 *
 * <h4>The hole this fills</h4>
 *
 * Two routes hold the phone's audio down when the microphone opens, and until
 * this class the book was on only one of them.
 *
 * <ul>
 *   <li><b>Dictation.</b> Gboard records as {@code VOICE_RECOGNITION}, so
 *       {@link BargeIn#holding} is true, the {@code /mic} readout says so, and
 *       {@code call_guard} pauses speech <em>and the book</em> on the phone.
 *       That route works.</li>
 *   <li><b>A conversation.</b> Claude Live records as
 *       {@code VOICE_COMMUNICATION}, which {@link BargeIn} deliberately reports
 *       as <em>not</em> holding: a voice session keeps the mic for its whole
 *       length, and pausing everything for the length of an evening's
 *       conversation is the failure that policy exists to avoid. The audio
 *       focus callbacks take over instead — {@link SpeechPolicy} pauses Sam
 *       while the other voice speaks and hands him back when it stops, and
 *       {@link FocusPolicy} ducks the music. Neither touches the book, because
 *       a book takes no part in the focus policy.</li>
 * </ul>
 *
 * So a conversation with Cece left Rothfuss talking underneath it, indefinitely
 * — the same failure {@code call_guard} was given {@code sink-book.sock} for on
 * 2026-08-15, arriving by the one door that was still open. Reported 2026-08-16.
 *
 * <h4>Why the book is not on the focus régime</h4>
 *
 * Ducking and handing back per utterance is right for Sam: he is talking
 * <em>to</em> David, so the two voices take turns and a gap of a few seconds is
 * the turn ending. A book is not in the conversation. Un-pausing it between
 * Cece's sentences would put a narrator in every gap, which is worse than not
 * pausing at all. So this hold is coarse on purpose: it takes the book down
 * when the session starts and gives it back when the session ends.
 *
 * <h4>What it will not do</h4>
 *
 * <b>Fight David.</b> If the book is playing again after we paused it, he
 * started it, and he wants it: the hold surrenders for the rest of the session
 * and owes nothing. And a pause it took is only lifted inside
 * {@link #RESUME_WINDOW_MS} — past that the conversation was long enough that
 * coming back to the book is a thing to decide, not a thing to have happen. The
 * book's standing default holds here as it does for a call: it stays paused
 * until David lifts it, which for a book is no loss, because it resumes where
 * it stopped.
 *
 * <b>Change what a phone call means.</b> A call records as
 * {@code VOICE_COMMUNICATION} too, so it arrives here looking exactly like a
 * conversation — and pausing for it is right, but <em>resuming</em> after it is
 * not: calls are the one case where the book deliberately stays down until
 * David lifts it (see {@code call_guard._DEFAULT_SOCKET_NAMES}), and a hold
 * added for Cece must not quietly reverse that. {@code inCall} is the
 * telephony mode, which no other app can put the phone into, and it is latched
 * for the episode: a call that has been up at any point during the hold ends
 * with the book where it is.
 *
 * android.*-free, so test/run.sh covers the deciding.
 */
final class BookHold {

    /** What the service should do to the book mpv. */
    enum Action { NONE, PAUSE, RESUME }

    /**
     * How long a conversation may run and still hand the book back by itself.
     *
     * Half an hour. Long enough that the ordinary case — a few minutes with
     * Cece and back to the chapter — always resumes, short enough that a
     * session left open all evening does not start narrating into a room that
     * has moved on. Being wrong this way costs one tap on a card that is
     * already in the shade; being wrong the other way is a voice in an empty
     * room, or over whatever David is doing by then.
     */
    static final long RESUME_WINDOW_MS = 1800000;

    /** True while a voice session is being held for. */
    private boolean holding = false;
    /** True while a pause of ours is owed a resume. */
    private boolean pausedByUs = false;
    /** True once David has restarted the book himself; we stay out after that. */
    private boolean surrendered = false;
    /** True once this episode has been a phone call: pause yes, resume no. */
    private boolean wasCall = false;
    /** When we paused; valid only while pausedByUs. */
    private long pausedAt = 0L;

    boolean holding() { return holding; }

    boolean owesResume() { return pausedByUs && !wasCall; }

    boolean surrendered() { return surrendered; }

    boolean wasCall() { return wasCall; }

    /**
     * Decide, from everything the service knows right now.
     *
     * @param voiceSession {@link BargeIn} says a two-way session holds the mic
     * @param bookAudible  the book is loaded and not paused — i.e. it is
     *                     talking underneath the conversation right now
     * @param inCall       the phone is in a telephony call right now
     * @param now          milliseconds
     */
    Action onState(boolean voiceSession, boolean bookAudible, boolean inCall,
                   long now) {
        if (!voiceSession) {
            // The session ended. Only a pause we actually took is ours to lift,
            // only while the book is still a thing to come back to, and never
            // after a call — that one is David's to lift.
            boolean owed = pausedByUs && !surrendered && !wasCall
                    && now - pausedAt < RESUME_WINDOW_MS;
            holding = false;
            pausedByUs = false;
            surrendered = false;
            wasCall = false;
            pausedAt = 0L;
            return owed ? Action.RESUME : Action.NONE;
        }

        holding = true;
        if (inCall) wasCall = true;

        if (surrendered || !bookAudible) return Action.NONE;

        if (pausedByUs) {
            // We paused it and it is playing again. Nothing else on this phone
            // un-pauses the book — the coordinator clears speech's pause, not
            // this one — so it was David, from the card or the popup. Let go.
            surrendered = true;
            pausedByUs = false;
            return Action.NONE;
        }

        // Audible with a conversation in progress, and we have not tried yet.
        // This covers the late arrival as well as the ordinary one: a dictation
        // that Live takes over mid-recording is handed back by call_guard the
        // moment BargeIn reclassifies it, so the book can start playing a beat
        // *after* the session began.
        pausedByUs = true;
        pausedAt = now;
        return Action.PAUSE;
    }

    /** One line for the readout: what this is doing and why. */
    String why() {
        if (!holding) return "not holding";
        if (surrendered) return "the book was started again by hand — leaving it";
        if (!pausedByUs) return "conversation, no book playing";
        return wasCall ? "holding the book for a call — David lifts this one"
                       : "holding the book for the conversation";
    }
}
