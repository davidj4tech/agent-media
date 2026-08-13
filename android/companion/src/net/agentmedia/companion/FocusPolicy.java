package net.agentmedia.companion;

/**
 * What to do with mpv when Android's audio focus moves.
 *
 * mpv ignores audio focus — that single fact is the root of most of the
 * phone-side complexity agent-media carries (call_guard polling notifications,
 * the Automate mic-detect hold flag, the duck/restore policy). The app holds
 * focus on mpv's behalf and translates the callbacks into IPC commands.
 *
 * Kept free of android.* so the decisions are testable on the build host; the
 * service supplies the constants and performs the actions.
 *
 * The delicate part is not the loss, it is the recovery: this class must never
 * resume music the listener paused themselves, and never restore a volume that
 * something else now owns. call_guard is still live and ducks the same mpv
 * during calls, so "someone else moved it, leave it alone" is a real case, not
 * a hypothetical.
 */
final class FocusPolicy {

    /**
     * Mirrors android.media.AudioManager. Duplicated rather than imported so
     * this class stays host-testable; the service asserts they still match.
     */
    static final int GAIN = 1;
    static final int LOSS = -1;
    static final int LOSS_TRANSIENT = -2;
    static final int LOSS_TRANSIENT_CAN_DUCK = -3;

    /** Where to duck to, on mpv's 0-100 scale. call_guard uses 10 for calls. */
    static final int DUCK_VOLUME = 15;

    enum Action {
        NONE,
        /** set pause=true */
        PAUSE,
        /** set pause=false */
        RESUME,
        /** set volume=DUCK_VOLUME */
        DUCK,
        /** set volume back to what it was before we ducked */
        UNDUCK,
    }

    /** True while a pause of ours is owed a resume. */
    private boolean pausedByUs = false;
    /** True while a duck of ours is owed a restore. */
    private boolean duckedByUs = false;
    /** The volume to put back, valid only while duckedByUs. */
    private double volumeBeforeDuck = 100.0;

    boolean owesResume() { return pausedByUs; }

    boolean owesUnduck() { return duckedByUs; }

    double volumeToRestore() { return volumeBeforeDuck; }

    Action onFocusChange(int change, MpvState state) {
        switch (change) {
            case LOSS:
                // Permanent: something else owns the output now. Pause, and
                // owe nothing — Android's convention is that a permanent loss
                // is not followed by a resume, and silently restarting music
                // some minutes later is worse than a button press.
                pausedByUs = false;
                if (duckedByUs) {
                    duckedByUs = false;
                }
                return state.playing() ? Action.PAUSE : Action.NONE;

            case LOSS_TRANSIENT:
                if (!state.playing()) return Action.NONE;
                pausedByUs = true;
                return Action.PAUSE;

            case LOSS_TRANSIENT_CAN_DUCK:
                // A navigation prompt or a notification: duck rather than
                // pause, which is what the listener expects and what
                // call_guard already does for calls.
                if (!state.loaded() || duckedByUs) return Action.NONE;
                volumeBeforeDuck = state.volume;
                duckedByUs = true;
                return Action.DUCK;

            case GAIN:
                if (duckedByUs) {
                    duckedByUs = false;
                    return Action.UNDUCK;
                }
                if (pausedByUs) {
                    pausedByUs = false;
                    // If it is already running, someone beat us to it.
                    return state.paused ? Action.RESUME : Action.NONE;
                }
                return Action.NONE;

            default:
                return Action.NONE;
        }
    }

    /**
     * Told whenever mpv's pause flag changes. A resume from anywhere else —
     * the earbuds, the CLI, red5 — cancels the resume we owe, so that when
     * focus comes back we do not fight the listener.
     */
    void onPauseChanged(boolean paused) {
        if (!paused) pausedByUs = false;
    }

    /**
     * Told whenever mpv's volume changes. If it moved somewhere we did not put
     * it, something else owns the volume now — call_guard during a call is the
     * live example — so drop the restore rather than clobber it.
     */
    void onVolumeChanged(double volume) {
        if (duckedByUs && Math.abs(volume - DUCK_VOLUME) > 0.5) {
            duckedByUs = false;
        }
    }

    /** mpv went idle: nothing is owed to a file that is no longer open. */
    void reset() {
        pausedByUs = false;
        duckedByUs = false;
    }
}
