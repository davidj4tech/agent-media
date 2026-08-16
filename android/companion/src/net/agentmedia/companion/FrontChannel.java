package net.agentmedia.companion;

/**
 * Which channel the one MediaSession is describing right now.
 *
 * The app publishes exactly one session, deliberately: two sessions compete for
 * the Bluetooth addressed-player slot, which is what the spike learned and what
 * the transport fix in 3519172 depends on. But speech is a different Termux mpv
 * (sink-speech.sock) from music, so a session pinned to the music mpv leaves the
 * car display naming a track while Sam is the thing actually being heard.
 *
 * <b>This is no longer about the display.</b> Until 2026-08-15 one session had
 * to describe every channel, and this class decided which one it named — a
 * mechanism that produced, in one morning, a card titled "Sam" reporting
 * STOPPED whose play button drove an idle music mpv, and then a card that could
 * pause Sam but not resume him. Each channel publishes its own session now (see
 * SideChannel), so each names itself and the taking of turns is gone.
 *
 * What is left is the question that was always the hard one and never really a
 * display question at all: <b>whose focus loss is this</b>. `ourSpeech` answers
 * it, the app ducks and pauses on the answer, and the two constants below are
 * still the labels a spoken clip cannot supply for itself.
 *
 * android.*-free, so test/run.sh covers it.
 */
final class FrontChannel {

    /**
     * What a spoken clip is called on the lock screen and the car display.
     *
     * A constant rather than the speech mpv's {@code media-title}: the clips are
     * rendered files and mpv titles them by filename, so that field reads
     * {@code remote-20260814T190922-18480.mp3} (checked against the phone's own
     * speech mpv on 2026-08-14, not assumed). Putting the sentence itself here
     * would mean the speech sink setting {@code force-media-title} before each
     * loadfile — worth doing, but it is a change on the red5 side and not this
     * one.
     */
    static final String SPEECH_TITLE = "Sam";

    // DEFAULT_SUBTITLE ("agent-media") was removed on 2026-08-17. It was the
    // artist line on all three cards at once, which is the app naming itself
    // three times on the one surface where a line costs something. Each channel
    // now says what only it knows — see CardText — and an empty line is a
    // legitimate answer.

    private FrontChannel() { }

    /**
     * True while a spoken clip is actually running. Deliberately {@code
     * playing()} and not {@code loaded()}: sink-speech keeps the last clip open
     * after it ends, and a broker paused from the popup should not hold the
     * display either.
     */
    static boolean speechInFront(MpvState speech) {
        return speechInFront(speech, false);
    }

    /**
     * @param held a clip is paused by something that means to resume it — the
     *     card's own pause button, or a focus pause of ours.
     *
     * Without this term the front channel drops the instant speech is paused,
     * and the card that just paused Sam turns back into a music card whose play
     * button goes to an idle music mpv. Observed on p8a 2026-08-15: {@code
     * transport: pause -> speech} at 08:54:17, {@code transport: play -> music}
     * at 08:54:20, and the reply stranded paused in between. A control that can
     * pause something it cannot resume is worse than one that does neither.
     *
     * {@code loaded()} is safe here where it is not above, precisely because
     * {@code held} is false for the clip sink-speech merely parks.
     */
    static boolean speechInFront(MpvState speech, boolean held) {
        return speech.playing() || (held && speech.loaded());
    }

    /**
     * How long after a clip is staged the focus loss it causes may still arrive.
     *
     * Measured, not guessed: on p8a on 2026-08-14 the {@code LOSS_TRANSIENT} for
     * a spoken reply landed at 20:16:29 and the clip's first audio at 20:16:40 —
     * mpv takes the output when it opens the file, and the relayed clip took
     * eleven seconds to get going. "Is speech playing right now" therefore
     * answers *no* for a loss that is entirely ours, which is how the first
     * build of this rule would have ducked anyway.
     *
     * Bounded rather than open-ended because the cost runs the other way too: for
     * this long after a clip is staged, a genuine outside interruption does not
     * duck the music.
     */
    static final long STAGING_GRACE_MS = 20000;

    /**
     * How long the coordinator's flag is believed without being renewed.
     *
     * It is cleared in `after_speech`, so a process killed mid-response leaves
     * it raised — and a raised flag means we never duck for anything. The next
     * response clears it, but the phone should not depend on there being one.
     * Long enough for the longest reply by a wide margin, short enough that a
     * crash costs an evening's ducking rather than the machine's uptime.
     */
    static final long SPEAKING_FLAG_MAX_MS = 300000;   // 5 minutes

    /** Is this focus loss our own speech? See the 3-arg form; no flag known. */
    static boolean ourSpeech(MpvState speech, long msSinceStaged) {
        return ourSpeech(speech, msSinceStaged, Long.MAX_VALUE);
    }

    /**
     * Is this focus loss our own speech?
     *
     * Three answers in descending order of trust. The coordinator's flag is the
     * only one that is actually told to us rather than inferred, and it is the
     * only one that covers the real case: mpv takes the output when it *opens* a
     * clip, and a response is rendered and relayed ahead of time, so the loss
     * has been seen 37 s before the first clip was staged. The two below it are
     * fallbacks for a coordinator too old to set the flag.
     *
     * @param msSinceStaged since the speech mpv last opened or started a clip;
     *     {@link Long#MAX_VALUE} when it never has.
     * @param msSinceFlagSet since the coordinator raised the flag;
     *     {@link Long#MAX_VALUE} when it never has.
     */
    static boolean ourSpeech(MpvState speech, long msSinceStaged, long msSinceFlagSet) {
        // An unreachable speech mpv tells us nothing, and a loss we cannot
        // attribute is treated as somebody else's — the duck is the behaviour
        // this app exists to provide, so it is what we fall back to.
        if (!speech.connected) return false;
        if (speech.speaking && msSinceFlagSet < SPEAKING_FLAG_MAX_MS) return true;
        return speechInFront(speech) || msSinceStaged < STAGING_GRACE_MS;
    }

    // title/subtitle/durationMs/name lived here until 2026-08-15. They existed
    // so one card could describe two channels by taking turns; speech and book
    // have cards of their own now (SideChannel), each naming itself, and a
    // display helper nobody calls is a claim about the app that is no longer
    // true. What remains is the question this class is actually good at: whose
    // focus loss is this.
}
