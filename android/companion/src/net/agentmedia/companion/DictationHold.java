package net.agentmedia.companion;

/**
 * Hold Sam while David dictates.
 *
 * <h4>Why this is separate from the session hold</h4>
 *
 * {@link BargeIn} already tells a dictation from a conversation, and the app
 * already acts on the conversation half — {@code applyLiveHold} makes Sam wait
 * out a Claude Live session and posts a card about it. The dictation half had
 * nobody: {@code MicWatch} was written as a probe ("it watches, publishes, and
 * drives nothing"), and the thing that used to act on dictation was an Automate
 * flow writing a flag file, retired on 2026-08-15 when mic detect moved into
 * this app. The actor was retired before its replacement started acting, so
 * from then until this class, voice typing did not pause Sam at all.
 *
 * The two holds want different manners, which is why this is not a flag on the
 * other one:
 *
 * <ul>
 *   <li>A voice session lasts as long as a conversation, so Sam waiting needs
 *       to be <em>announced</em> — a card, a toast, a queue count.</li>
 *   <li>A dictation lasts seconds. Announcing it would put a notification on
 *       screen every time David talks to his keyboard. It should be silent and
 *       it should end by itself.</li>
 * </ul>
 *
 * <h4>Why it re-asserts</h4>
 *
 * mpv's {@code pause} is not sticky against us: the coordinator clears it at
 * the start of every response (sinks/speech.py, reset_state). So pausing once
 * when the mic opens is not enough — a reply that arrives mid-dictation loads
 * into a broker whose pause has just been cleared and starts talking over the
 * dictation it was supposed to wait for. Every state push asks again, and every
 * time speech is audible while the mic is open the answer is PAUSE.
 *
 * <h4>Why it gives up</h4>
 *
 * A mic that never closes would otherwise hold Sam silent forever, and an app
 * can hold the mic open without anyone talking into it. {@link #MAX_HOLD_MS}
 * bounds the damage: past it the hold is released, Sam carries on, and nothing
 * pauses again until the mic actually closes and a fresh dictation starts. This
 * is not the duration-guessing that {@link BargeIn} rejected for *classifying* a
 * recording — the classification stays with the audio source. It is the same
 * reasoning as {@code SpeechPolicy.RESUME_DEADLINE_MS}: a pause of ours that
 * nothing is going to lift is a wedged broker.
 *
 * android.*-free, so test/run.sh covers the deciding.
 */
final class DictationHold {

    /** What the service should do to the speech mpv. */
    enum Action { NONE, PAUSE, RESUME }

    /**
     * How long a dictation may hold Sam before the hold is abandoned.
     *
     * Two minutes, matching {@code SpeechPolicy.RESUME_WINDOW_MS} — the length
     * of interruption David said a half-heard sentence can survive. Nothing
     * dictated into a keyboard runs longer; a mic open longer than this is
     * something else, and something else is not a reason to keep Sam quiet.
     */
    static final long MAX_HOLD_MS = 120000;

    /** True while the mic is open and it is not a two-way session. */
    private boolean holding = false;
    /** True while a pause of ours is owed a resume. */
    private boolean pausedByUs = false;
    /** Set when MAX_HOLD_MS passes: no more pausing until the mic closes. */
    private boolean expired = false;
    /** When the current hold began; valid only while holding. */
    private long heldSince = 0L;

    boolean holding() { return holding; }

    boolean owesResume() { return pausedByUs; }

    boolean expired() { return expired; }

    /**
     * Decide, from everything the service knows right now.
     *
     * @param micOpen       something is recording
     * @param voiceSession  ...and {@link BargeIn} says it is a conversation
     * @param speechAudible speech is loaded and not paused — i.e. it would be
     *                      talking over the dictation right now
     * @param now           milliseconds, monotonic enough for a two-minute cap
     */
    Action onState(boolean micOpen, boolean voiceSession, boolean speechAudible,
                   long now) {
        boolean dictating = micOpen && !voiceSession;

        if (!dictating) {
            // The mic closed, or the recording turned out to be a conversation
            // — which has its own hold, with its own manner. Either way this
            // one is over, and only a pause we actually took is ours to lift.
            boolean owed = pausedByUs;
            holding = false;
            pausedByUs = false;
            expired = false;
            heldSince = 0L;
            return owed ? Action.RESUME : Action.NONE;
        }

        if (!holding) {
            holding = true;
            expired = false;
            heldSince = now;
        }

        if (!expired && now - heldSince >= MAX_HOLD_MS) {
            expired = true;
            // Hand the broker back if we are holding it; otherwise say nothing
            // and simply stop pausing.
            boolean owed = pausedByUs;
            pausedByUs = false;
            return owed ? Action.RESUME : Action.NONE;
        }

        if (expired || !speechAudible) return Action.NONE;

        // Audible while the mic is open — pause, and keep pausing, because the
        // coordinator clears our pause at the start of each response.
        pausedByUs = true;
        return Action.PAUSE;
    }

    /** One line for the readout: what this is doing and why. */
    String why() {
        if (!holding) return "not holding";
        if (expired) return "gave up (mic open past " + (MAX_HOLD_MS / 1000) + "s)";
        return pausedByUs ? "holding Sam for dictation" : "dictation, nothing to hold";
    }
}
