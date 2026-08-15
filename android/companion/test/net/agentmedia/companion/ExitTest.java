package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for the exit-reason wording.
 *
 * Small, and worth having anyway: this text is the whole product. It exists to
 * be read from red5 by a session that was not here, about a process that is
 * already gone, on a phone where nothing else will say a word about it — so a
 * reason silently rendered as a bare number, or a foreground kill that reads
 * like a background reap, costs exactly as much as the bug it was meant to
 * explain.
 */
public final class ExitTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) {
        testNamesEveryReason();
        testUnknownCodeStillReads();
        testDescribeCarriesTheFacts();
        testOnlyACrashOwesATrace();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    /**
     * Every code Android defines has a word. The reason this is a test rather
     * than a glance: the switch is a mirror of a framework constant list, and a
     * mirror is exactly the kind of thing that goes quietly out of date.
     */
    private static void testNamesEveryReason() {
        is("LOW_MEMORY", ExitReason.name(ExitReason.LOW_MEMORY), "memory");
        is("USER_REQUESTED", ExitReason.name(ExitReason.USER_REQUESTED), "swiped away");
        is("USER_STOPPED", ExitReason.name(ExitReason.USER_STOPPED), "force-stopped");
        is("ANR", ExitReason.name(ExitReason.ANR), "anr");
        is("CRASH", ExitReason.name(ExitReason.CRASH), "crash");
        is("FREEZER", ExitReason.name(ExitReason.FREEZER), "frozen");
        is("EXCESSIVE_RESOURCE_USAGE", ExitReason.name(ExitReason.EXCESSIVE_RESOURCE_USAGE),
           "reaped for resource use");
        for (int r = ExitReason.UNKNOWN; r <= ExitReason.PACKAGE_UPDATED; r++) {
            no(ExitReason.name(r).startsWith("reason "), "code " + r + " has a word");
        }
    }

    /** A future Android with a seventeenth reason must still print something. */
    private static void testUnknownCodeStillReads() {
        is("reason 99", ExitReason.name(99), "unmapped code falls back to the number");
    }

    private static void testDescribeCarriesTheFacts() {
        String line = ExitReason.describe(0L, ExitReason.LOW_MEMORY, 0, 125, "");
        has(line, "LOW_MEMORY", "the reason");
        has(line, "importance=125", "foreground-or-not, the field that reads the kill");
        no(line.contains("status="), "a zero status is noise, not a fact");

        String anr = ExitReason.describe(0L, ExitReason.ANR, 9, 100,
                "Input dispatching timed out");
        has(anr, "status=9", "a non-zero status is kept");
        has(anr, "Input dispatching timed out", "Android's own words survive");

        no(ExitReason.describe(0L, ExitReason.CRASH, 0, 100, null).contains("null"),
           "a null description is absent, not the word null");
    }

    /**
     * The one inference the readout is allowed to make. An empty Downloads
     * directory means "not a throw" for sixteen of the seventeen reasons and
     * "the recorder itself failed" for the seventeenth, and those are different
     * bugs to go and look for.
     */
    private static void testOnlyACrashOwesATrace() {
        yes(ExitReason.wouldHaveLeftATrace(ExitReason.CRASH), "a throw is recordable");
        no(ExitReason.wouldHaveLeftATrace(ExitReason.CRASH_NATIVE),
           "a native crash never reaches a Java handler");
        no(ExitReason.wouldHaveLeftATrace(ExitReason.LOW_MEMORY), "a kill is not a throw");
        no(ExitReason.wouldHaveLeftATrace(ExitReason.USER_STOPPED), "nor is a force-stop");
        no(ExitReason.wouldHaveLeftATrace(ExitReason.ANR), "nor an ANR");
    }

    // ---- assertions ------------------------------------------------------

    private static void is(Object want, Object got, String what) {
        if (want == null ? got == null : want.equals(got)) passed++;
        else failures.add(what + ": wanted " + want + ", got " + got);
    }

    private static void has(String haystack, String needle, String what) {
        if (haystack != null && haystack.contains(needle)) passed++;
        else failures.add(what + ": " + needle + " missing from " + haystack);
    }

    private static void yes(boolean got, String what) {
        if (got) passed++; else failures.add(what + ": wanted true");
    }

    private static void no(boolean got, String what) {
        if (!got) passed++; else failures.add(what + ": wanted false");
    }
}
