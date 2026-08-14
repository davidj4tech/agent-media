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
        /** set pause=false on the speech mpv — the clip carries on where it stopped */
        RESUME,
        /** stop the clip, then clear the pause: a clean broker, no stale sentence */
        DISCARD,
    }

    /**
     * How long an interruption may last and still be one the listener is in the
     * middle of.
     *
     * David's rule (2026-08-15): <i>depends how long it was paused for; for
     * short interruptions, make it resume.</i> Inside this window the `GAIN`
     * picks the sentence up where it stopped, which is what a navigation prompt
     * or a notification chime deserves. Outside it, the listener has moved on
     * and a voice resuming mid-clause is startling rather than helpful — so the
     * pause is left standing for a manual resume (popup Space, `media resume`),
     * which is exactly the policy `call_guard` chose for calls.
     *
     * Two minutes, David's number (2026-08-15, raised from the thirty seconds
     * this shipped with). It is the length of interruption you can still hold a
     * half-heard sentence across, and it is generous on purpose: the failure it
     * trades against is a sentence you wanted that never came back, which is
     * worse than one you had stopped waiting for arriving late. A short call
     * now falls inside it; the deadline below is what catches the rest.
     */
    static final long RESUME_WINDOW_MS = 120000;   // 2 minutes

    /**
     * How long a pause of ours may stand before it is cleaned up.
     *
     * mpv's {@code pause} is a property of the player, not of the clip: it
     * outlives the file that was open when it was set. So the failure to avoid
     * is not "the rest of that sentence was lost" — that is the intended
     * outcome of a long interruption — it is "the speech broker is wedged", and
     * every later reply loads into it and plays silently.
     *
     * This is why the deadline DISCARDs rather than resuming. Between
     * {@link #RESUME_WINDOW_MS} and here, the pause is David's to lift; past
     * here, nothing is going to lift it, so the clip is dropped and the broker
     * handed back clean. The coordinator clearing pause at the start of each
     * response (sinks/speech.py, reset_state) is the backstop underneath both.
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
                return onGain(nowMs);

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
        return one(Action.PAUSE);
    }

    /**
     * Focus is back. Whether that resumes the sentence depends on how long it
     * was gone — see {@link #RESUME_WINDOW_MS}.
     *
     * A late GAIN deliberately returns nothing <i>and keeps the debt</i>: the
     * pause stays for David to lift by hand, and if he never does, the deadline
     * clears it. Paying it here would be the startling case; forgetting it here
     * would be the wedged one.
     */
    private List<Action> onGain(long nowMs) {
        if (!pausedByUs) return Collections.emptyList();
        if (nowMs - pausedAt >= RESUME_WINDOW_MS) return Collections.emptyList();
        pausedByUs = false;
        return one(Action.RESUME);
    }

    /**
     * Clean up a pause nothing is going to lift. Called on a timer by the
     * service; returns the same list shape as the focus table so the caller has
     * one way to perform an action.
     */
    List<Action> onTick(long nowMs) {
        if (!pausedByUs) return Collections.emptyList();
        if (nowMs - pausedAt < RESUME_DEADLINE_MS) return Collections.emptyList();
        pausedByUs = false;
        return one(Action.DISCARD);
    }

    private static List<Action> one(Action action) {
        List<Action> actions = new ArrayList<Action>(1);
        actions.add(action);
        return actions;
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
     * forgetting it wedges the broker. Only the service leaving (a mode switch,
     * a shutdown) clears it, and it DISCARDs first.
     */
    void forget() {
        pausedByUs = false;
    }
}
