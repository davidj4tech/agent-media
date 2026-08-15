package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for the scrolling card title.
 *
 * The properties worth pinning are the ones that make it stop rather than the
 * ones that make it move: a title that fits must never animate, because that is
 * what keeps this feature free for the ordinary case and away from the
 * notification churn the app already worries about.
 */
public final class MarqueeTest {

    private static final String BOOK =
            "FULL AUDIOBOOK - Patrick Rothfuss - Kingkiller Chronicle #1 "
            + "- The Name of the Wind";

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) {
        testShortTitlesNeverMove();
        testWindowIsAlwaysTheAskedWidth();
        testItScrolls();
        testItComesRoundAgain();
        testGapSeparatesTheEndFromTheStart();
        testDegenerateInputs();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testShortTitlesNeverMove() {
        no(Marquee.needed("Hippie", 30), "a short title needs no marquee");
        no(Marquee.needed("", 30), "nor an empty one");
        no(Marquee.needed("     ", 30), "nor whitespace");
        yes(Marquee.needed(BOOK, 30), "a book title does");

        // The one that matters: same answer at every moment, so a fitting title
        // is not merely still, it is the same string the card would have shown
        // with no marquee in the app at all.
        for (long t = 0; t < 10_000; t += 700) {
            is("Hippie", Marquee.window("Hippie", 30, t), "unmoved at t=" + t);
        }
    }

    private static void testWindowIsAlwaysTheAskedWidth() {
        for (long t = 0; t < 200_000; t += 313) {
            String w = Marquee.window(BOOK, 30, t);
            if (w.length() != 30) {
                failures.add("width drifted at t=" + t + ": " + w.length());
                return;
            }
        }
        passed++;
        // Including the wrap point, where a naive substring runs off the end.
        String loop = Marquee.collapse(BOOK) + Marquee.GAP;
        long justBeforeWrap = (long) ((loop.length() - 1) * 1000);
        is(30, Marquee.window(BOOK, 30, justBeforeWrap).length(), "width holds at the wrap");
    }

    private static void testItScrolls() {
        String at0 = Marquee.window(BOOK, 30, 0);
        is("FULL AUDIOBOOK - Patrick Rothf", at0, "starts at column zero");
        is("ULL AUDIOBOOK - Patrick Rothfu", Marquee.window(BOOK, 30, 1000),
           "one column per second");
        is("LL AUDIOBOOK - Patrick Rothfus", Marquee.window(BOOK, 30, 2000), "and again");
        // Sub-second ticks do not jitter it forward and back.
        is(at0, Marquee.window(BOOK, 30, 999), "still column zero at 999ms");
    }

    private static void testItComesRoundAgain() {
        String loop = Marquee.collapse(BOOK) + Marquee.GAP;
        long round = (long) (loop.length() * 1000);
        is(Marquee.window(BOOK, 30, 0), Marquee.window(BOOK, 30, round),
           "one full lap returns to the start");
    }

    private static void testGapSeparatesTheEndFromTheStart() {
        // Without the gap the last word runs straight into the first, and
        // "...the WindFULL AUDIOBOOK" reads as a title nobody wrote.
        String loop = Marquee.collapse(BOOK) + Marquee.GAP;
        long atEnd = (long) ((Marquee.collapse(BOOK).length() - 5) * 1000);
        has(Marquee.window(BOOK, 30, atEnd), "Wind" + Marquee.GAP + "FULL",
            "the gap sits between the lap and the next");
        is(loop.length(), Marquee.collapse(BOOK).length() + Marquee.GAP.length(),
           "the lap is the text plus the gap");
    }

    private static void testDegenerateInputs() {
        is("", Marquee.window(null, 30, 0), "null title");
        is("", Marquee.window(BOOK, 0, 0), "zero width");
        is("", Marquee.window(BOOK, -4, 0), "negative width");
        // A clock that went backwards must not throw a negative substring index.
        is(Marquee.window(BOOK, 30, 0), Marquee.window(BOOK, 30, -5000),
           "a backwards clock restarts rather than crashing");
        is("A B", Marquee.collapse("  A \n\t B  "), "runs of whitespace collapse to one");
    }

    // ---- assertions ------------------------------------------------------

    private static void is(Object want, Object got, String what) {
        if (want == null ? got == null : want.equals(got)) passed++;
        else failures.add(what + ": wanted [" + want + "], got [" + got + "]");
    }

    private static void has(String haystack, String needle, String what) {
        if (haystack != null && haystack.contains(needle)) passed++;
        else failures.add(what + ": [" + needle + "] missing from [" + haystack + "]");
    }

    private static void yes(boolean got, String what) {
        if (got) passed++; else failures.add(what + ": wanted true");
    }

    private static void no(boolean got, String what) {
        if (!got) passed++; else failures.add(what + ": wanted false");
    }
}
