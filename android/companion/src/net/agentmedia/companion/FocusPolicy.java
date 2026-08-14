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
        /** set volume=DUCK_VOLUME */
        DUCK,
        /** set volume={@link #volumeToRestore()} */
        UNDUCK,
    }

    /** True while a duck of ours is owed a restore. */
    private boolean duckedByUs = false;
    /** The volume to put back, valid only while duckedByUs. */
    private double volumeBeforeDuck = 100.0;
    /** The last volume this policy asked for; NaN when it has asked for none. */
    private double volumeWeSet = Double.NaN;

    boolean owesUnduck() { return duckedByUs; }

    double volumeToRestore() { return volumeBeforeDuck; }

    /**
     * Returns the actions to perform, in order. A list rather than one action
     * because a permanent loss genuinely owes two: the volume goes back before
     * the pause, and no second callback is coming to carry the leftover.
     */
    List<Action> onFocusChange(int change, MpvState state) {
        return onFocusChange(change, state, false);
    }

    /**
     * @param ourSpeech a clip of ours is playing right now — the focus loss is
     *     Sam speaking, not another app. See the transient branch.
     */
    List<Action> onFocusChange(int change, MpvState state, boolean ourSpeech) {
        List<Action> actions = new ArrayList<Action>(2);

        switch (change) {
            case LOSS:
                // Permanent: something else owns the output now. Restore the
                // volume, then pause, and owe nothing — Android's convention is
                // that a permanent loss is not followed by a resume, and music
                // restarting minutes later is worse than a button press.
                // Unducking first matters: otherwise the volume stays down for
                // good and the next press of play is near-silence.
                unduckInto(actions);
                if (state.playing()) actions.add(Action.PAUSE);
                return actions;

            case LOSS_TRANSIENT:
            case LOSS_TRANSIENT_CAN_DUCK:
                // Both transient losses duck. David's rule — duck the music,
                // pause the speech — supersedes the handover's table here, and
                // the evidence agrees: on 2026-08-14 our *own* spoken replies
                // were observed taking focus with LOSS_TRANSIENT (log at
                // 18:32:20, bracketing the clip exactly). Pausing on that would
                // stop the music dead for every sentence Sam says, and fight
                // the coordinator, which is already ducking for the same clip.
                //
                // A real call is covered too: call_guard ducks phone-local
                // music during calls rather than pausing it, so ducking here
                // converges with the behaviour rather than inventing a second.
                //
                // Except when the loss IS our own speech: red5's coordinator
                // already ducks this same mpv for its own clip, and two duckers
                // on one volume lose the restore between them. Observed on p8a
                // 2026-08-14 19:17:38 — we ducked 130 -> 10, restored to 130 on
                // the GAIN, and the coordinator (which had captured 10 as the
                // value to put back) restored to 10 one second later. The music
                // then played quiet for two hours. Whoever captured the
                // pre-duck volume must be the one to put it back, and for a
                // spoken reply that is the coordinator, which knows when the
                // whole response ends rather than one clip.
                if (ourSpeech) return Collections.emptyList();
                if (!state.playing() || duckedByUs) return Collections.emptyList();
                volumeBeforeDuck = state.volume;
                duckedByUs = true;
                volumeWeSet = DUCK_VOLUME;
                actions.add(Action.DUCK);
                return actions;

            case GAIN:
                return unduckInto(actions);

            default:
                return Collections.emptyList();
        }
    }

    /** Append the restore, if one is owed, and clear the debt. */
    private List<Action> unduckInto(List<Action> actions) {
        if (!duckedByUs) return actions;
        duckedByUs = false;
        volumeWeSet = volumeBeforeDuck;
        actions.add(Action.UNDUCK);
        return actions;
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
        duckedByUs = false;
        volumeWeSet = Double.NaN;
    }
}
