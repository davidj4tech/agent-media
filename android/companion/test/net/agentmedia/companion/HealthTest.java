package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Host-side tests for the health strip.
 *
 * The strip replaced "read the log and work it out", so the rule it is held to
 * is that a broken thing is *named*, in the words a person would use, and that
 * a working thing still says so — silence is what this app looked like for the
 * fortnight barge-in was dead.
 */
public final class HealthTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) {
        testHealthyPhoneSaysSo();
        testDeadMicIsNamed();
        testMissingBridgeIsCounted();
        testOneRestartIsNotAnAlarm();
        testACrashLoopIs();
        testNoServiceStopsThere();
        testDeathsAreCountedByDay();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testHealthyPhoneSaysSo() {
        List<Health.Pill> s = Health.strip(true, true, 3, 0);
        check("three pills when all is well", s.size() == 3);
        check("the mic is watching", s.get(0).text.equals("mic watching"));
        check("and green", s.get(0).level == Health.Level.OK);
        check("all three bridges", s.get(1).text.equals("3 bridges"));
        check("up all day", s.get(2).text.equals("up all day"));
        check("nothing amber", !hasWarn(s));
    }

    private static void testDeadMicIsNamed() {
        List<Health.Pill> s = Health.strip(true, false, 3, 0);
        check("a dead watch says dead", s.get(0).text.equals("mic watch dead"));
        check("and is amber", s.get(0).level == Health.Level.WARN);
    }

    private static void testMissingBridgeIsCounted() {
        List<Health.Pill> s = Health.strip(true, true, 2, 0);
        check("two of three", s.get(1).text.equals("2 of 3 bridges"));
        check("and is amber", s.get(1).level == Health.Level.WARN);
    }

    private static void testOneRestartIsNotAnAlarm() {
        // One is how this app lives on Android. Said, so the second one does not
        // look like the first.
        List<Health.Pill> s = Health.strip(true, true, 3, 1);
        check("one restart is reported", s.get(2).text.equals("1 restart today"));
        check("but not as a problem", s.get(2).level == Health.Level.OK);
    }

    private static void testACrashLoopIs() {
        List<Health.Pill> s = Health.strip(true, true, 3, 4);
        check("four deaths is a loop", s.get(2).text.equals("4 deaths today"));
        check("and amber", s.get(2).level == Health.Level.WARN);
    }

    private static void testNoServiceStopsThere() {
        // Nothing below it means anything if the service is not there.
        List<Health.Pill> s = Health.strip(false, false, 0, 9);
        check("one pill only", s.size() == 1);
        check("and it says which", s.get(0).text.equals("service down"));
    }

    private static void testDeathsAreCountedByDay() {
        List<String> exits = Arrays.asList(
                "08-17 08:22:31  PACKAGE_UPDATED importance=125 — stop",
                "08-17 07:04:39  ANR importance=300 — bg anr",
                "08-16 21:56:47  ANR importance=300 — bg anr");
        check("today's two", Health.deathsOn(exits, "08-17") == 2);
        check("yesterday's one", Health.deathsOn(exits, "08-16") == 1);
        check("a quiet day", Health.deathsOn(exits, "08-15") == 0);
        check("no list, no deaths", Health.deathsOn(null, "08-17") == 0);
    }

    private static boolean hasWarn(List<Health.Pill> pills) {
        for (Health.Pill p : pills) if (p.level == Health.Level.WARN) return true;
        return false;
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
