package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Host-side tests for the audio-focus decision table.
 *
 * FocusPolicy imports nothing from android.* precisely so this can run under a
 * plain JDK in seconds. p8a has no adb, so anything not proven here is debugged
 * by squinting at a phone screen — the table gets tested before it is sideloaded.
 *
 * Plain main() and hand-rolled assertions, matching IpcTest: pulling in JUnit
 * would mean a Maven dependency, which is the thing this project avoids.
 */
public final class FocusTest {

    /** What the phone's mpv actually runs at: --volume-max=170, --volume=130. */
    private static final double NORMAL = 130.0;

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) {
        testNothingLoaded();
        testDuckAndRestore();
        testDuckIsNotRepeated();
        testDuckSkippedWhenPaused();
        testTransientLossDucksRatherThanPausing();
        testPermanentLossOwesNothing();
        testPermanentLossRestoresVolumeFirst();
        testSecondTransientLossWhileDucked();
        testUserPausedIsNotOurs();
        testForeignVolumeDropsTheRestore();
        testOurOwnDuckEchoIsNotForeign();
        testOurOwnSpeechIsNotOursToDuck();
        testOurOwnSpeechStillPausesOnAPermanentLoss();
        testOurOwnSpeechDoesNotStrandAnExistingDuck();
        testIdleResets();
        testUnknownChangeDoesNothing();
        testButtonsAreReadAgainstOurOwnState();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    // ---- the table -------------------------------------------------------

    private static void testNothingLoaded() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = idle();
        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s),
                "nothing loaded: no duck");
        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT, s),
                "nothing loaded: no pause");
        actions(p.onFocusChange(FocusPolicy.LOSS, s),
                "nothing loaded: no pause on permanent loss");
        actions(p.onFocusChange(FocusPolicy.GAIN, s),
                "nothing loaded: nothing owed on gain");
    }

    private static void testDuckAndRestore() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();

        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s),
                "can-duck loss ducks", FocusPolicy.Action.DUCK);
        yes(p.owesUnduck(), "a duck is owed a restore");
        is(NORMAL, p.volumeToRestore(), "the pre-duck volume is remembered");

        // The service writes it; mpv echoes it back through the observer.
        s.volume = FocusPolicy.DUCK_VOLUME;
        p.onVolumeChanged(s.volume);

        actions(p.onFocusChange(FocusPolicy.GAIN, s),
                "gain restores the volume", FocusPolicy.Action.UNDUCK);
        is(NORMAL, p.volumeToRestore(), "restored to what it was, not to 100");
        no(p.owesUnduck(), "nothing owed after the restore");
    }

    private static void testDuckIsNotRepeated() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();
        p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s);
        s.volume = FocusPolicy.DUCK_VOLUME;
        p.onVolumeChanged(s.volume);

        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s),
                "a second can-duck loss does not re-duck");
        is(NORMAL, p.volumeToRestore(),
           "and does not overwrite the baseline with the ducked volume");
    }

    private static void testDuckSkippedWhenPaused() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();
        s.paused = true;
        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s),
                "nothing to duck while paused");
        no(p.owesUnduck(), "and no restore owed");
    }

    private static void testTransientLossDucksRatherThanPausing() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();

        // David's rule: duck the music, pause the speech. Our own spoken
        // replies were observed arriving as LOSS_TRANSIENT, so pausing here
        // would stop the music dead for every sentence Sam says.
        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT, s),
                "a transient loss ducks, it does not pause",
                FocusPolicy.Action.DUCK);
        yes(p.owesUnduck(), "and owes the restore");
        is(NORMAL, p.volumeToRestore(), "remembering where it was");

        s.volume = FocusPolicy.DUCK_VOLUME;
        p.onVolumeChanged(s.volume);

        actions(p.onFocusChange(FocusPolicy.GAIN, s),
                "gain puts it back", FocusPolicy.Action.UNDUCK);
        no(p.owesUnduck(), "nothing owed afterwards");
    }

    private static void testPermanentLossOwesNothing() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();

        actions(p.onFocusChange(FocusPolicy.LOSS, s),
                "permanent loss pauses", FocusPolicy.Action.PAUSE);
        no(p.owesUnduck(), "and owes nothing — no silent restart minutes later");

        s.paused = true;
        actions(p.onFocusChange(FocusPolicy.GAIN, s),
                "so a later gain does nothing");
    }

    private static void testPermanentLossRestoresVolumeFirst() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();
        p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s);
        s.volume = FocusPolicy.DUCK_VOLUME;
        p.onVolumeChanged(s.volume);

        // Otherwise the volume stays at the duck level for good, and the next
        // press of play hands the listener near-silence.
        actions(p.onFocusChange(FocusPolicy.LOSS, s),
                "permanent loss restores the volume before pausing",
                FocusPolicy.Action.UNDUCK, FocusPolicy.Action.PAUSE);
        is(NORMAL, p.volumeToRestore(), "back to the pre-duck volume");
        no(p.owesUnduck(), "nothing owed");
    }

    private static void testSecondTransientLossWhileDucked() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();
        p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s);
        s.volume = FocusPolicy.DUCK_VOLUME;
        p.onVolumeChanged(s.volume);

        // Speech clips arrive back to back; a second loss must not re-read the
        // baseline from the already-ducked volume.
        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT, s),
                "a loss while already ducked does nothing");
        is(NORMAL, p.volumeToRestore(), "the baseline survives it");

        actions(p.onFocusChange(FocusPolicy.GAIN, s),
                "and the gain still restores", FocusPolicy.Action.UNDUCK);
    }

    private static void testUserPausedIsNotOurs() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();
        s.paused = true;

        // Nothing to duck and nothing to pause; above all, nothing owed that
        // would undo the listener's own pause when focus returns.
        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT, s),
                "already paused: a transient loss does nothing");
        no(p.owesUnduck(), "and owes nothing");

        actions(p.onFocusChange(FocusPolicy.GAIN, s),
                "so focus returning leaves the listener's pause alone");
    }

    private static void testForeignVolumeDropsTheRestore() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();
        p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s);

        // call_guard ducks the same mpv during a call, to its own depth of 20.
        // It saved its own baseline and will restore it; ours is now stale.
        s.volume = 20.0;
        p.onVolumeChanged(s.volume);
        no(p.owesUnduck(), "a volume we did not write means someone else owns it");

        actions(p.onFocusChange(FocusPolicy.GAIN, s),
                "so we do not clobber it on the way back");
    }

    private static void testOurOwnDuckEchoIsNotForeign() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();
        p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s);

        // mpv echoing our own write back through the property observer, and
        // rounding it on the way.
        p.onVolumeChanged(FocusPolicy.DUCK_VOLUME + 0.2);
        yes(p.owesUnduck(), "our own write, rounded, is still ours");
    }

    /**
     * The 2026-08-14 19:17 trace: we ducked 130 -> 10 for our own spoken reply,
     * restored to 130 on the GAIN, and the coordinator — which had captured our
     * ducked 10 as the volume to put back — restored to 10 a second later. The
     * music played quiet for two hours. One ducker per volume.
     */
    private static void testOurOwnSpeechIsNotOursToDuck() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();

        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT, s, true),
                "our own speech: the coordinator is already ducking this mpv");
        no(p.owesUnduck(), "and we owe nothing back, so the GAIN cannot fight it");

        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK, s, true),
                "same for the can-duck loss");
        actions(p.onFocusChange(FocusPolicy.GAIN, s, true), "and the GAIN is a no-op");

        // Someone else's loss still ducks, clip or no clip: the flag says whose
        // the loss is, not whether ducking is allowed.
        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT, s, false),
                "another app still ducks the music", FocusPolicy.Action.DUCK);
    }

    private static void testOurOwnSpeechStillPausesOnAPermanentLoss() {
        FocusPolicy p = new FocusPolicy();
        // A permanent loss is never our own clip — it is something taking the
        // output for good — so the flag must not suppress the pause.
        actions(p.onFocusChange(FocusPolicy.LOSS, playing(), true),
                "a permanent loss pauses regardless", FocusPolicy.Action.PAUSE);
    }

    private static void testOurOwnSpeechDoesNotStrandAnExistingDuck() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();
        p.onFocusChange(FocusPolicy.LOSS_TRANSIENT, s, false);
        yes(p.owesUnduck(), "a real duck, owed back");

        s.volume = FocusPolicy.DUCK_VOLUME;
        actions(p.onFocusChange(FocusPolicy.LOSS_TRANSIENT, s, true),
                "a clip arriving mid-duck changes nothing");
        yes(p.owesUnduck(), "the debt from the real loss survives it");
        actions(p.onFocusChange(FocusPolicy.GAIN, s, true),
                "and is still paid on the GAIN", FocusPolicy.Action.UNDUCK);
    }

    private static void testIdleResets() {
        FocusPolicy p = new FocusPolicy();
        MpvState s = playing();
        p.onFocusChange(FocusPolicy.LOSS_TRANSIENT, s);
        yes(p.owesUnduck(), "a debt to clear");

        p.reset();
        no(p.owesUnduck(), "mpv going idle owes nothing to a file that is gone");
    }

    private static void testUnknownChangeDoesNothing() {
        FocusPolicy p = new FocusPolicy();
        // AUDIOFOCUS_REQUEST_FAILED and friends share the int space; anything
        // we do not recognise must be inert rather than guessed at.
        actions(p.onFocusChange(0, playing()), "an unknown focus change is inert");
        actions(p.onFocusChange(99, playing()), "including a positive one");
    }

    // ---- transport keys --------------------------------------------------

    private static void testButtonsAreReadAgainstOurOwnState() {
        MpvState paused = playing();
        paused.paused = true;
        MpvState running = playing();

        // The observed bug: the earbuds only ever send the dedicated PAUSE,
        // because the silent AudioTrack keeps A2DP flowing and they take that
        // as "playing". Read against our state it can only mean play.
        is(ButtonPolicy.Press.PLAY,
           ButtonPolicy.interpret(ButtonPolicy.KEYCODE_MEDIA_PAUSE, paused),
           "PAUSE pressed while already paused means play");
        is(ButtonPolicy.Press.DEFAULT,
           ButtonPolicy.interpret(ButtonPolicy.KEYCODE_MEDIA_PAUSE, running),
           "PAUSE pressed while playing means what it says");

        is(ButtonPolicy.Press.PAUSE,
           ButtonPolicy.interpret(ButtonPolicy.KEYCODE_MEDIA_PLAY, running),
           "PLAY pressed while already playing means pause");
        is(ButtonPolicy.Press.DEFAULT,
           ButtonPolicy.interpret(ButtonPolicy.KEYCODE_MEDIA_PLAY, paused),
           "PLAY pressed while paused means what it says");

        // The toggle is resolved by the framework from the PlaybackState we
        // publish, which is correct — second-guessing it would break it.
        is(ButtonPolicy.Press.DEFAULT,
           ButtonPolicy.interpret(ButtonPolicy.KEYCODE_MEDIA_PLAY_PAUSE, paused),
           "the toggle is left to the framework");
        is(ButtonPolicy.Press.DEFAULT,
           ButtonPolicy.interpret(85 + 1000, paused), "an unrelated key is untouched");

        // Nothing open: no state to read the key against.
        is(ButtonPolicy.Press.DEFAULT,
           ButtonPolicy.interpret(ButtonPolicy.KEYCODE_MEDIA_PAUSE, idle()),
           "idle mpv leaves every key alone");
    }

    // ---- fixtures --------------------------------------------------------

    private static MpvState idle() {
        MpvState s = new MpvState();
        s.connected = true;
        s.idleActive = true;
        return s;
    }

    private static MpvState playing() {
        MpvState s = new MpvState();
        s.connected = true;
        s.idleActive = false;
        s.paused = false;
        s.volume = NORMAL;
        return s;
    }

    // ---- assertions ------------------------------------------------------

    private static void actions(List<FocusPolicy.Action> got, String what,
                                FocusPolicy.Action... want) {
        is(Arrays.asList(want).toString(), got.toString(), what);
    }

    private static void is(Object want, Object got, String what) {
        if (want == null ? got == null : want.equals(got)) {
            passed++;
        } else {
            failures.add(what + ": wanted " + want + ", got " + got);
        }
    }

    private static void yes(boolean got, String what) {
        is(Boolean.TRUE, Boolean.valueOf(got), what);
    }

    private static void no(boolean got, String what) {
        is(Boolean.FALSE, Boolean.valueOf(got), what);
    }
}
