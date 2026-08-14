package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

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
 * David's rule: <b>duck the music, pause the speech.</b> This class is the
 * music half — a half-heard sentence is a lost sentence, so speech cannot be
 * ducked meaningfully, but music under a navigation prompt just needs to get
 * out of the way. Speech is a separate mpv (sink-speech.sock) and needs its own
 * loopback bridge before it can be driven from here at all.
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
     * this class stays host-testable; FocusControl asserts they still match.
     */
    static final int GAIN = 1;
    static final int LOSS = -1;
    static final int LOSS_TRANSIENT = -2;
    static final int LOSS_TRANSIENT_CAN_DUCK = -3;

    /**
     * Where to duck to, on mpv's scale. Not a new number: it is
     * InterruptionPolicy.duck_level for ContentType.MUSIC
     * (agent_media_core/route/policy.py), the level music already drops to
     * while Sam speaks over it. Deep, but the phone's mpv runs --volume-max=170
     * with 130 as normal, so this is quiet rather than silent.
     *
     * Deliberately *not* call_guard's 20: keeping the two distinct is what
     * makes "did we set this volume, or did something else?" answerable.
     */
    static final int DUCK_VOLUME = 10;

    /** mpv rounds; anything inside this of what we wrote is still our value. */
    private static final double VOLUME_EPSILON = 0.5;

    enum Action {
        /** set pause=true */
        PAUSE,
        /** set pause=false */
        RESUME,
        /** set volume=DUCK_VOLUME */
        DUCK,
        /** set volume={@link #volumeToRestore()} */
        UNDUCK,
    }

    /** True while a pause of ours is owed a resume. */
    private boolean pausedByUs = false;
    /** True while a duck of ours is owed a restore. */
    private boolean duckedByUs = false;
    /** The volume to put back, valid only while duckedByUs. */
    private double volumeBeforeDuck = 100.0;
    /** The last volume this policy asked for; NaN when it has asked for none. */
    private double volumeWeSet = Double.NaN;

    boolean owesResume() { return pausedByUs; }

    boolean owesUnduck() { return duckedByUs; }

    double volumeToRestore() { return volumeBeforeDuck; }

    /**
     * Returns the actions to perform, in order. A list rather than one action
     * because a single focus change genuinely owes two things: a GAIN that
     * finds us both ducked and paused must undo both, and no second GAIN is
     * coming to carry the leftover.
     */
    List<Action> onFocusChange(int change, MpvState state) {
        List<Action> actions = new ArrayList<Action>(2);

        switch (change) {
            case LOSS:
                // Permanent: something else owns the output now. Pause, and
                // owe nothing — Android's convention is that a permanent loss
                // is not followed by a resume, and silently restarting music
                // some minutes later is worse than a button press.
                unduckInto(actions);
                if (state.playing()) actions.add(Action.PAUSE);
                pausedByUs = false;
                return actions;

            case LOSS_TRANSIENT:
                if (!state.loaded()) return Collections.emptyList();
                // Put the volume back before pausing: a ducked *and* paused
                // mpv is quiet for no reason, and unducking now is simpler
                // than carrying the debt across the pause. The GAIN then only
                // has a resume to do.
                unduckInto(actions);
                if (state.playing()) {
                    pausedByUs = true;
                    actions.add(Action.PAUSE);
                }
                return actions;

            case LOSS_TRANSIENT_CAN_DUCK:
                // A navigation prompt or a notification: duck rather than
                // pause, which is what the listener expects and what
                // call_guard already does for calls.
                if (!state.playing() || duckedByUs) return Collections.emptyList();
                volumeBeforeDuck = state.volume;
                duckedByUs = true;
                volumeWeSet = DUCK_VOLUME;
                actions.add(Action.DUCK);
                return actions;

            case GAIN:
                // Volume back before audio: the other order plays a moment of
                // full-level music that the listener did not ask for.
                unduckInto(actions);
                if (pausedByUs) {
                    pausedByUs = false;
                    // If it is already running, someone beat us to it.
                    if (state.paused) actions.add(Action.RESUME);
                }
                return actions;

            default:
                return Collections.emptyList();
        }
    }

    /** Append the restore, if one is owed, and clear the debt. */
    private void unduckInto(List<Action> actions) {
        if (!duckedByUs) return;
        duckedByUs = false;
        volumeWeSet = volumeBeforeDuck;
        actions.add(Action.UNDUCK);
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
     * it, something else owns the volume now — call_guard ducking the same mpv
     * during a call is the live example — so drop the restore rather than
     * clobber it.
     *
     * Compared against what we actually wrote rather than against DUCK_VOLUME:
     * the guard then survives the constant changing, and still reads correctly
     * when the foreign write happens to be another duck.
     */
    void onVolumeChanged(double volume) {
        if (!duckedByUs) return;
        if (Double.isNaN(volumeWeSet)) return;
        if (Math.abs(volume - volumeWeSet) > VOLUME_EPSILON) duckedByUs = false;
    }

    /** mpv went idle: nothing is owed to a file that is no longer open. */
    void reset() {
        pausedByUs = false;
        duckedByUs = false;
        volumeWeSet = Double.NaN;
    }
}
