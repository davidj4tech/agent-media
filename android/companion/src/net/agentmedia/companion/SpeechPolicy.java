package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * What to do with the speech mpv when Android's audio focus moves.
 *
 * The other half of David's rule — <b>duck the music, pause the speech</b>.
 * {@link FocusPolicy} is the music half; this one exists because a half-heard
 * sentence is a lost sentence. Ducking Sam under a navigation prompt does not
 * make him quieter, it makes him gone: the words keep going while nobody can
 * hear them, and nothing replays them. Music has the opposite property, which
 * is why the two halves are different classes rather than one table with a
 * flag.
 *
 * Speech is a separate Termux mpv (sink-speech.sock) behind its own loopback
 * bridge, so this drives a different {@link MpvIpc} from the music one.
 *
 * android.*-free, so test/run.sh covers it.
 */
final class SpeechPolicy {

    enum Action {
        /** set pause=true on the speech mpv */
        PAUSE,
        /** set pause=false on the speech mpv */
        RESUME,
    }

    /**
     * How long a pause of ours may go unpaid before it is paid anyway.
     *
     * The normal payer is the {@code GAIN}. This is for the case where one
     * never comes — a permanent loss, or an app that takes the output and is
     * killed holding it. mpv's {@code pause} is a property of the player, not
     * of the clip: it outlives the file that was open when it was set, so a
     * stranded pause is not "the rest of that sentence is lost", it is "the
     * speech broker is wedged". The coordinator does clear pause at the start
     * of each response (sinks/speech.py, reset_state), so the damage is bounded
     * at one reply — but a control surface we set and stopped tracking is not a
     * thing to leave lying on the phone.
     *
     * Long enough that a real interruption is over by the time it fires, and a
     * five-minute-old half sentence arriving late is the acknowledged cost.
     */
    static final long RESUME_DEADLINE_MS = 300000;   // 5 minutes

    /** True while a pause of ours is owed a resume. */
    private boolean pausedByUs = false;
    /** When we paused; valid only while pausedByUs. */
    private long pausedAt = 0L;

    boolean owesResume() { return pausedByUs; }

    /**
     * Returns the actions to perform, in order.
     *
     * @param speech the speech mpv's mirrored state.
     * @param ourSpeech this focus loss is Sam speaking, not another app — see
     *     {@link FrontChannel#ourSpeech}. The transient branch turns on it.
     * @param nowMs wall clock, passed in rather than read, so the deadline is
     *     testable on the host.
     */
    List<Action> onFocusChange(int change, MpvState speech, boolean ourSpeech, long nowMs) {
        switch (change) {
            case FocusPolicy.LOSS:
                // Permanent, so nothing is coming to say it is over. Pause
                // anyway — the alternative is talking under whatever took the
                // output — and, unlike the music half, keep owing the resume.
                // Music can sit paused indefinitely because a listener presses
                // play; nobody presses play on the speech broker, and a pause
                // left on it is not one lost sentence but every later one. The
                // deadline pays it if no GAIN ever does.
                return pauseInto(speech, nowMs);

            case FocusPolicy.LOSS_TRANSIENT:
            case FocusPolicy.LOSS_TRANSIENT_CAN_DUCK:
                // The loss our own clip caused is the one loss that must not
                // pause speech: mpv takes the output when it opens the file, so
                // acting on it would pause the very clip that produced it — Sam
                // silencing himself on the first word of every reply. The
                // coordinator's flag is what tells us, and CAN_DUCK is treated
                // as a plain transient here on purpose: permission to duck is
                // not permission to be inaudible.
                if (ourSpeech) return Collections.emptyList();
                return pauseInto(speech, nowMs);

            case FocusPolicy.GAIN:
                return resumeInto();

            default:
                return Collections.emptyList();
        }
    }

    private List<Action> pauseInto(MpvState speech, long nowMs) {
        // Already paused: either ours (and already owed) or the listener's from
        // the popup, which is not ours to take over and not ours to undo.
        if (!speech.playing() || pausedByUs) return Collections.emptyList();
        pausedByUs = true;
        pausedAt = nowMs;
        List<Action> actions = new ArrayList<Action>(1);
        actions.add(Action.PAUSE);
        return actions;
    }

    private List<Action> resumeInto() {
        if (!pausedByUs) return Collections.emptyList();
        pausedByUs = false;
        List<Action> actions = new ArrayList<Action>(1);
        actions.add(Action.RESUME);
        return actions;
    }

    /**
     * Pay the debt if it has gone unpaid too long. Called on a timer by the
     * service; returns the same list shape as the focus table so the caller has
     * one way to perform an action.
     */
    List<Action> onTick(long nowMs) {
        if (!pausedByUs) return Collections.emptyList();
        if (nowMs - pausedAt < RESUME_DEADLINE_MS) return Collections.emptyList();
        return resumeInto();
    }

    /**
     * Told whenever the speech mpv's pause changes. A resume from anywhere else
     * — the popup, the CLI, or the coordinator clearing pause at the start of
     * the next response — means the pause we owed is no longer ours to pay, and
     * re-paying it would fight whoever now owns it. The twin of
     * {@link FocusPolicy#onVolumeChanged}.
     *
     * Our own PAUSE echoes back through the same observer; that one arrives as
     * {@code true} and changes nothing.
     */
    void onPauseChanged(boolean paused) {
        if (pausedByUs && !paused) pausedByUs = false;
    }

    /**
     * Deliberately no reset-on-idle twin of {@link FocusPolicy#reset}: mpv's
     * pause belongs to the player, not to the file, so a clip ending while we
     * owe a resume does not cancel the debt — it is precisely the case where
     * forgetting it wedges the broker. Only a mode switch clears it, and the
     * service pays it first.
     */
    void forget() {
        pausedByUs = false;
    }
}
