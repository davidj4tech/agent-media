package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for the book hold.
 *
 * The behaviour this protects is the one reported on 2026-08-16: a conversation
 * with Cece left the audiobook playing underneath it, because a voice session
 * reaches neither call_guard (BargeIn reports not-holding, deliberately) nor
 * the focus policy (a book takes no part in it). Every rule below is a way that
 * could come back — narrating through a conversation, or fighting David over
 * the book he just restarted.
 */
public final class BookHoldTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    private static final boolean SESSION = true, NONE = false;
    private static final boolean PLAYING = true, QUIET = false;
    private static final boolean CALL = true, NOT_A_CALL = false;

    public static void main(String[] args) {
        testPausesTheBookForAConversation();
        testPicksItBackUpAfterwards();
        testDoesNotPauseWhatIsNotPlaying();
        testDoesNotResumeWhatItNeverPaused();
        testDoesNotFightDavid();
        testCatchesABookThatStartsLate();
        testLeavesALongConversationsBookAlone();
        testAFreshConversationAfterSurrendering();
        testACallIsDavidsToLift();
        testACallDuringAConversationStillCounts();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testPausesTheBookForAConversation() {
        BookHold b = new BookHold();
        check("a session over a playing book pauses it",
                b.onState(SESSION, PLAYING, NOT_A_CALL, 0) == BookHold.Action.PAUSE);
        check("and it knows it owes a resume", b.owesResume());
        check("and does not pause again while it stays paused",
                b.onState(SESSION, QUIET, NOT_A_CALL, 500) == BookHold.Action.NONE);
    }

    private static void testPicksItBackUpAfterwards() {
        BookHold b = new BookHold();
        b.onState(SESSION, PLAYING, NOT_A_CALL, 0);
        check("the session ending hands the book back",
                b.onState(NONE, QUIET, NOT_A_CALL, 60000) == BookHold.Action.RESUME);
        check("owing nothing after", !b.owesResume());
        check("and a second look does nothing",
                b.onState(NONE, QUIET, NOT_A_CALL, 60100) == BookHold.Action.NONE);
    }

    private static void testDoesNotPauseWhatIsNotPlaying() {
        BookHold b = new BookHold();
        check("no book, nothing to do",
                b.onState(SESSION, QUIET, NOT_A_CALL, 0) == BookHold.Action.NONE);
        check("but the hold is on, watching", b.holding());
        check("and it lifts nothing at the end",
                b.onState(NONE, QUIET, NOT_A_CALL, 1000) == BookHold.Action.NONE);
    }

    private static void testDoesNotResumeWhatItNeverPaused() {
        // David may have paused the book himself, or call_guard may hold it for
        // a call. Lifting a pause we did not take is how a narrator starts up in
        // the middle of a phone call.
        BookHold b = new BookHold();
        b.onState(SESSION, QUIET, NOT_A_CALL, 0);
        check("nothing paused, nothing resumed",
                b.onState(NONE, QUIET, NOT_A_CALL, 2000) == BookHold.Action.NONE);
    }

    private static void testDoesNotFightDavid() {
        BookHold b = new BookHold();
        b.onState(SESSION, PLAYING, NOT_A_CALL, 0);
        check("the book playing again mid-session is not ours to stop",
                b.onState(SESSION, PLAYING, NOT_A_CALL, 5000) == BookHold.Action.NONE);
        check("we have surrendered", b.surrendered());
        check("and owe nothing", !b.owesResume());
        check("and stay out even if it stops again",
                b.onState(SESSION, PLAYING, NOT_A_CALL, 9000) == BookHold.Action.NONE);
        check("and the session ending changes nothing",
                b.onState(NONE, PLAYING, NOT_A_CALL, 12000) == BookHold.Action.NONE);
    }

    private static void testCatchesABookThatStartsLate() {
        // A dictation that Live takes over mid-recording: call_guard releases
        // its own hold the moment BargeIn reclassifies, so the book it had
        // paused starts playing a beat *after* the session began.
        BookHold b = new BookHold();
        b.onState(SESSION, QUIET, NOT_A_CALL, 0);
        check("a book that arrives after the session started is still paused",
                b.onState(SESSION, PLAYING, NOT_A_CALL, 300) == BookHold.Action.PAUSE);
        check("and handed back at the end",
                b.onState(NONE, QUIET, NOT_A_CALL, 20000) == BookHold.Action.RESUME);
    }

    private static void testLeavesALongConversationsBookAlone() {
        // Past the window, coming back to the book is a thing to decide. The
        // pause stands; the card is right there.
        BookHold b = new BookHold();
        b.onState(SESSION, PLAYING, NOT_A_CALL, 0);
        long late = BookHold.RESUME_WINDOW_MS + 1;
        check("a conversation longer than the window leaves it paused",
                b.onState(NONE, QUIET, NOT_A_CALL, late) == BookHold.Action.NONE);
        check("owing nothing after", !b.owesResume());
    }

    private static void testAFreshConversationAfterSurrendering() {
        // Surrender is for the session it happened in, not forever.
        BookHold b = new BookHold();
        b.onState(SESSION, PLAYING, NOT_A_CALL, 0);
        b.onState(SESSION, PLAYING, NOT_A_CALL, 5000);      // David starts it again
        b.onState(NONE, PLAYING, NOT_A_CALL, 6000);         // session ends
        check("the next conversation holds again",
                b.onState(SESSION, PLAYING, NOT_A_CALL, 7000) == BookHold.Action.PAUSE);
        check("not surrendered any more", !b.surrendered());
    }

    private static void testACallIsDavidsToLift() {
        // A call records as VOICE_COMMUNICATION too, so it arrives here looking
        // like a conversation. Pausing is right; resuming afterwards would
        // quietly reverse the policy calls have always had.
        BookHold b = new BookHold();
        check("a call over a playing book pauses it",
                b.onState(SESSION, PLAYING, CALL, 0) == BookHold.Action.PAUSE);
        check("but it is not a resume we owe", !b.owesResume());
        check("and the call ending leaves the book down",
                b.onState(NONE, QUIET, NOT_A_CALL, 30000) == BookHold.Action.NONE);
        check("the flag clears with the episode", !b.wasCall());
    }

    private static void testACallDuringAConversationStillCounts() {
        // The call arrives after the hold is already ours. Latched, so the
        // resume we were going to do is dropped.
        BookHold b = new BookHold();
        b.onState(SESSION, PLAYING, NOT_A_CALL, 0);
        check("we owed a resume before the call", b.owesResume());
        b.onState(SESSION, QUIET, CALL, 5000);
        check("the call takes the resume away", !b.owesResume());
        check("and the end leaves it paused",
                b.onState(NONE, QUIET, NOT_A_CALL, 9000) == BookHold.Action.NONE);
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
