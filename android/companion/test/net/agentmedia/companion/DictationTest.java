package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for the dictation hold.
 *
 * The behaviour this protects is the one that was missing for a fortnight:
 * voice typing did not pause Sam, because the mic watch was a probe and the
 * thing that used to act on it had been retired. Every rule below is a way that
 * could go wrong again — talking over a dictation, or never coming back.
 */
public final class DictationTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    private static final boolean MIC = true, QUIET = false;
    private static final boolean SESSION = true, DICTATION = false;
    private static final boolean AUDIBLE = true, SILENT = false;

    public static void main(String[] args) {
        testPausesWhileDictating();
        testResumesWhenTheMicShuts();
        testDoesNotResumeWhatItNeverPaused();
        testLeavesAVoiceSessionAlone();
        testReAssertsThePause();
        testNothingToPauseIsNotAFailure();
        testPausesAReplyThatArrivesMidDictation();
        testGivesUpOnAMicThatNeverCloses();
        testANewDictationAfterGivingUp();
        testHandsBackWhenAConversationTakesOver();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testPausesWhileDictating() {
        DictationHold d = new DictationHold();
        check("mic open over a clip pauses it",
                d.onState(MIC, DICTATION, AUDIBLE, 0) == DictationHold.Action.PAUSE);
        check("and it knows it owes a resume", d.owesResume());
    }

    private static void testResumesWhenTheMicShuts() {
        DictationHold d = new DictationHold();
        d.onState(MIC, DICTATION, AUDIBLE, 0);
        check("mic shut resumes",
                d.onState(QUIET, DICTATION, SILENT, 900) == DictationHold.Action.RESUME);
        check("and owes nothing after", !d.owesResume());
        check("a second look does nothing",
                d.onState(QUIET, DICTATION, SILENT, 1000) == DictationHold.Action.NONE);
    }

    private static void testDoesNotResumeWhatItNeverPaused() {
        // David may have paused Sam himself, or call_guard may hold him for a
        // call. Lifting a pause we did not take is how a held channel starts
        // talking in the middle of a phone call.
        DictationHold d = new DictationHold();
        d.onState(MIC, DICTATION, SILENT, 0);
        check("nothing paused, nothing resumed",
                d.onState(QUIET, DICTATION, SILENT, 500) == DictationHold.Action.NONE);
    }

    private static void testLeavesAVoiceSessionAlone() {
        // A Live session has its own hold, with a card and a queue count. This
        // one must not also grab the broker.
        DictationHold d = new DictationHold();
        check("a voice session is not this hold's business",
                d.onState(MIC, SESSION, AUDIBLE, 0) == DictationHold.Action.NONE);
        check("and it is not holding", !d.holding());
    }

    private static void testReAssertsThePause() {
        // The coordinator clears pause at the start of every response, so a
        // single pause when the mic opened would not survive the next reply.
        DictationHold d = new DictationHold();
        d.onState(MIC, DICTATION, AUDIBLE, 0);
        check("audible again while the mic is open pauses again",
                d.onState(MIC, DICTATION, AUDIBLE, 400) == DictationHold.Action.PAUSE);
    }

    private static void testNothingToPauseIsNotAFailure() {
        DictationHold d = new DictationHold();
        check("silence needs no pause",
                d.onState(MIC, DICTATION, SILENT, 0) == DictationHold.Action.NONE);
        check("but the hold is on", d.holding());
    }

    private static void testPausesAReplyThatArrivesMidDictation() {
        DictationHold d = new DictationHold();
        d.onState(MIC, DICTATION, SILENT, 0);          // dictation starts, quiet
        check("a reply that starts talking mid-dictation is paused",
                d.onState(MIC, DICTATION, AUDIBLE, 3000) == DictationHold.Action.PAUSE);
        check("mic shut hands it back",
                d.onState(QUIET, DICTATION, SILENT, 4000) == DictationHold.Action.RESUME);
    }

    private static void testGivesUpOnAMicThatNeverCloses() {
        DictationHold d = new DictationHold();
        d.onState(MIC, DICTATION, AUDIBLE, 0);
        long past = DictationHold.MAX_HOLD_MS + 1;
        check("past the cap it hands the broker back",
                d.onState(MIC, DICTATION, AUDIBLE, past) == DictationHold.Action.RESUME);
        check("and says so", d.expired());
        check("and stops pausing even while audible",
                d.onState(MIC, DICTATION, AUDIBLE, past + 500) == DictationHold.Action.NONE);
        check("owing nothing", !d.owesResume());
    }

    private static void testANewDictationAfterGivingUp() {
        // Giving up must not be permanent: the next actual dictation holds.
        DictationHold d = new DictationHold();
        d.onState(MIC, DICTATION, AUDIBLE, 0);
        d.onState(MIC, DICTATION, AUDIBLE, DictationHold.MAX_HOLD_MS + 1);
        d.onState(QUIET, DICTATION, SILENT, DictationHold.MAX_HOLD_MS + 2);
        check("a fresh dictation holds again",
                d.onState(MIC, DICTATION, AUDIBLE, DictationHold.MAX_HOLD_MS + 3)
                        == DictationHold.Action.PAUSE);
        check("not expired any more", !d.expired());
    }

    private static void testHandsBackWhenAConversationTakesOver() {
        // The recording is reclassified without the mic closing — Live starting
        // while the mic is already open. The session hold takes it from here,
        // so this one must let go of the pause it is holding.
        DictationHold d = new DictationHold();
        d.onState(MIC, DICTATION, AUDIBLE, 0);
        check("reclassified as a conversation hands the pause back",
                d.onState(MIC, SESSION, AUDIBLE, 100) == DictationHold.Action.RESUME);
        check("and lets go", !d.holding() && !d.owesResume());
    }

    private static void check(String what, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  ok   " + what);
        } else {
            failures.add(what);
            System.out.println("  FAIL " + what);
        }
    }
}
