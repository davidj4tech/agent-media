package net.agentmedia.companion;

/**
 * How to read a transport key, given what mpv is actually doing.
 *
 * Normally the framework translates media keys into onPlay/onPause and gets it
 * right. It cannot here, because the headset lies about the state — and it lies
 * for a reason we cannot remove.
 *
 * The silent AudioTrack follows {@code loaded()}, so it keeps writing zeros
 * while mpv is paused; that is deliberate, because dropping it would surrender
 * the Bluetooth addressed-player slot (see the spike). But it also keeps the
 * A2DP stream flowing, and the earbuds take *that* as "audio is playing". So
 * they send {@code KEYCODE_MEDIA_PAUSE} — the dedicated key, not the toggle —
 * every single time, and the play half of the button never arrives at all.
 * Observed on 2026-08-14: five presses, three consecutive, every one a PAUSE
 * while the session correctly reported PAUSED.
 *
 * Since the headset will only ever hand us one key, we read it against our own
 * state instead of taking its word for it.
 *
 * android.*-free so test/run.sh covers it; the service asserts the keycodes
 * still match {@code android.view.KeyEvent}.
 */
final class ButtonPolicy {

    /** Mirrors android.view.KeyEvent. */
    static final int KEYCODE_MEDIA_PLAY_PAUSE = 85;
    static final int KEYCODE_MEDIA_PLAY = 126;
    static final int KEYCODE_MEDIA_PAUSE = 127;

    enum Press {
        /** Let the framework translate it — it will get this one right. */
        DEFAULT,
        PLAY,
        PAUSE,
    }

    /**
     * What the press should mean. {@link Press#DEFAULT} hands it back to the
     * framework untouched, which is what happens for every key we have no
     * reason to second-guess — including the PLAY_PAUSE toggle, which the
     * framework resolves from the PlaybackState we publish and therefore gets
     * right already.
     */
    static Press interpret(int keyCode, MpvState state) {
        // With nothing open there is no state to read the key against, and
        // guessing would fight whatever the framework wants to do instead.
        if (!state.loaded()) return Press.DEFAULT;

        switch (keyCode) {
            case KEYCODE_MEDIA_PAUSE:
                // The observed case: the headset sends this believing audio is
                // playing. If we are already paused it can only sensibly mean
                // the other thing.
                return state.paused ? Press.PLAY : Press.DEFAULT;

            case KEYCODE_MEDIA_PLAY:
                // The mirror image, for a headset that gets stuck the other
                // way. Not observed, but it costs one line and the same
                // reasoning applies.
                return state.paused ? Press.DEFAULT : Press.PAUSE;

            default:
                return Press.DEFAULT;
        }
    }
}
