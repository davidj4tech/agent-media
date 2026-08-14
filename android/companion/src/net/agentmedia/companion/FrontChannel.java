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
 * So the *metadata* follows whichever channel is in front, and nothing else
 * does. The PlaybackState — reported state, position, actions — stays with the
 * music mpv, because the framework resolves a PLAY_PAUSE toggle from what we
 * report, and answering that question about a two-second speech clip is exactly
 * the class of bug 3519172 fixed. The transport callbacks likewise still drive
 * music only.
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

    /** Shown when there is nothing better to say. Also the artist line for music. */
    static final String DEFAULT_SUBTITLE = "agent-media";

    private FrontChannel() { }

    /**
     * True while a spoken clip is actually running. Deliberately {@code
     * playing()} and not {@code loaded()}: sink-speech keeps the last clip open
     * after it ends, and a broker paused from the popup should not hold the
     * display either.
     */
    static boolean speechInFront(MpvState speech) {
        return speech.playing();
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

    static String title(MpvState music, MpvState speech) {
        return speechInFront(speech) ? SPEECH_TITLE : music.title();
    }

    /**
     * The second line. While Sam speaks it names the music underneath, because
     * the progress bar next to it is still the music track's — a display that
     * says "Sam" over someone else's position is less coherent than one that
     * says whose position it is.
     */
    static String subtitle(MpvState music, MpvState speech) {
        if (speechInFront(speech) && music.loaded()) return music.title();
        return DEFAULT_SUBTITLE;
    }

    /**
     * Duration for the metadata. Unknown (-1) while speech is in front: the
     * position we publish belongs to the music track, so pairing it with the
     * clip's length would draw a progress bar that is wrong in both directions.
     */
    static long durationMs(MpvState music, MpvState speech) {
        return speechInFront(speech) ? -1L : music.durationMs();
    }

    /** For the readout: "speech" or "music". */
    static String name(MpvState speech) {
        return speechInFront(speech) ? "speech" : "music";
    }
}
