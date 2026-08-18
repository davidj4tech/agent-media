package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for the second line of a card.
 *
 * The rule being protected is that the line is worth its space: it says
 * something only this channel knows, or it says nothing. Every check below is a
 * way of drifting back to "agent-media" — padding an unknown, restating the
 * play button, or announcing a queue of one.
 */
public final class CardTextTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) {
        testMusicPrefersTheArtist();
        testMusicFallsBackToTheQueue();
        testMusicSaysNothingRatherThanUnknown();
        testSpeechSaysWhatWasSaid();
        testAParkedClipIsNotAQueue();
        testSpeechWhileSpeaking();
        testBookCountsDown();
        testBookWithoutADuration();
        testDurationReadsAloud();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testMusicPrefersTheArtist() {
        check("the artist wins",
                "Nick Cave".equals(CardText.music("Nick Cave", 2, 12)));
        check("and is trimmed",
                "Nick Cave".equals(CardText.music("  Nick Cave  ", -1, 0)));
    }

    private static void testMusicFallsBackToTheQueue() {
        check("no artist, a real queue",
                "track 3 of 12".equals(CardText.music(null, 2, 12)));
        check("blank artist counts as none",
                "track 1 of 4".equals(CardText.music("   ", 0, 4)));
    }

    private static void testMusicSaysNothingRatherThanUnknown() {
        check("a queue of one is not worth saying",
                CardText.music(null, 0, 1).isEmpty());
        check("nothing loaded, nothing said",
                CardText.music(null, -1, 0).isEmpty());
    }

    private static void testSpeechSaysWhatWasSaid() {
        // The title is the conversation — an identity, and short enough to sit
        // still — so this line carries the words, which change every turn.
        check("the words lead",
                "a reply".equals(CardText.speech(1, true, true, false, "a reply")));
        check("with a pile behind them",
                "a reply · 2 more waiting".equals(
                        CardText.speech(3, true, true, false, "a reply")));
        // Parked after the reply: the entry is still there, the pile is not.
        check("and stand alone without one",
                "a reply".equals(CardText.speech(1, false, true, false, "a reply")));
        check("no words, just the pile",
                "2 more waiting".equals(CardText.speech(2, false, true, true, "")));
        check("and nothing at all is empty",
                CardText.speech(0, false, false, false, "").isEmpty());
    }

    private static void testAParkedClipIsNotAQueue() {
        // The bug this rewrite is for: mpv keeps the entry of a clip that has
        // finished — it parks the last one open — so an idle player reports
        // one, and the card said "1 reply waiting" for the rest of the day.
        check("idle says nothing",
                CardText.speech(1, false, false, false, "").isEmpty());
        check("parked and running says nothing either",
                CardText.speech(1, false, true, false, "").isEmpty());
        // Paused with entries behind it is what a hold looks like, and that is
        // a real pile.
        check("a hold is a pile",
                "1 more waiting".equals(CardText.speech(1, false, true, true, "")));
    }

    private static void testSpeechWhileSpeaking() {
        // The open clip is in the count, so "1" mid-reply means nothing behind it.
        check("speaking, nothing behind",
                CardText.speech(1, true, true, false, "").isEmpty());
        check("speaking, one behind",
                "1 more waiting".equals(CardText.speech(2, true, true, false, "")));
        check("speaking, several behind",
                "3 more waiting".equals(CardText.speech(4, true, true, false, "")));
    }

    private static void testBookCountsDown() {
        long hour = 3600000L;
        check("hours and minutes",
                "1h 13m left".equals(CardText.book(hour + 20 * 60000L, 7 * 60000L)));
        check("minutes alone",
                "13m left".equals(CardText.book(20 * 60000L, 7 * 60000L)));
        check("the last seconds",
                "44s left".equals(CardText.book(60000L, 16000L)));
    }

    private static void testBookWithoutADuration() {
        check("no duration, no countdown", CardText.book(-1, 0).isEmpty());
        check("finished says nothing", CardText.book(1000L, 1000L).isEmpty());
    }

    private static void testDurationReadsAloud() {
        check("a long book", "28h".equals(CardText.duration(28 * 3600000L)));
        check("hours drop empty minutes",
                "2h".equals(CardText.duration(2 * 3600000L + 20000L)));
        check("seconds for the short ones", "8s".equals(CardText.duration(8400L)));
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
