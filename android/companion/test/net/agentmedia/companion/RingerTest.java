package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for the ringer readout.
 *
 * Every check here is one of the two ways this feature fails, and they are not
 * equally bad. Speaking into a silenced phone is the complaint that started it.
 * Withholding an alert that should have been spoken is worse: it is silent by
 * construction, indistinguishable from a broken TTS stack, and this project has
 * lost whole afternoons to exactly that shape. So most of what follows is about
 * the second — the cases where we must decide to speak.
 */
public final class RingerTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    private static final boolean GRANTED = true, UNGRANTED = false;

    public static void main(String[] args) {
        testTheRingerSwitch();
        testDoNotDisturb();
        testAnUnansweredQuestionIsNeverQuiet();
        testTheWireFormat();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testTheRingerSwitch() {
        check("silent is quiet", RingerState.quiet(
                RingerState.RINGER_SILENT, RingerState.FILTER_ALL, GRANTED));
        // A phone that answers a call with a buzz is not a phone that wants a
        // paragraph of agenda read aloud to the room.
        check("vibrate is quiet", RingerState.quiet(
                RingerState.RINGER_VIBRATE, RingerState.FILTER_ALL, GRANTED));
        check("normal speaks", !RingerState.quiet(
                RingerState.RINGER_NORMAL, RingerState.FILTER_ALL, GRANTED));
    }

    private static void testDoNotDisturb() {
        // The whole reason the DND half exists: on modern Android the ringer
        // mode stays `normal` right through Do Not Disturb, so the switch alone
        // would have missed every night David silences the phone this way.
        check("priority-only DND is quiet even at ringer normal",
                RingerState.quiet(RingerState.RINGER_NORMAL,
                                  RingerState.FILTER_PRIORITY, GRANTED));
        check("total silence is quiet", RingerState.quiet(
                RingerState.RINGER_NORMAL, RingerState.FILTER_NONE, GRANTED));
        check("alarms-only is quiet", RingerState.quiet(
                RingerState.RINGER_NORMAL, RingerState.FILTER_ALARMS, GRANTED));
        check("filter all speaks", !RingerState.quiet(
                RingerState.RINGER_NORMAL, RingerState.FILTER_ALL, GRANTED));
    }

    private static void testAnUnansweredQuestionIsNeverQuiet() {
        // Ungranted, getCurrentInterruptionFilter() reports UNKNOWN — which is
        // not "no DND", it is "we were not allowed to look". Reading it as
        // quiet would withhold alerts on a phone in no special state at all,
        // on every install that has not been through the settings screen.
        check("ungranted filter is ignored, whatever it says",
                !RingerState.quiet(RingerState.RINGER_NORMAL,
                                   RingerState.FILTER_NONE, UNGRANTED));
        check("UNKNOWN is not quiet even when granted",
                !RingerState.quiet(RingerState.RINGER_NORMAL,
                                   RingerState.FILTER_UNKNOWN, GRANTED));
        // The ringer switch still decides on its own — it needs no grant, so an
        // install without one keeps the half of the feature that works.
        check("silent is still quiet without the grant",
                RingerState.quiet(RingerState.RINGER_SILENT,
                                  RingerState.FILTER_UNKNOWN, UNGRANTED));
        // A mode integer we have never seen is a phone we do not understand.
        check("an unknown ringer mode speaks",
                !RingerState.quiet(99, RingerState.FILTER_ALL, GRANTED));
        check("and names itself unknown",
                "unknown".equals(RingerState.modeName(99)));
    }

    private static void testTheWireFormat() {
        // The reader takes split()[0] as the mode, exactly as it does for /mic.
        check("mode comes first", RingerState.line(
                RingerState.RINGER_SILENT, RingerState.FILTER_PRIORITY, GRANTED)
                .equals("silent dnd=priority granted=1"));
        // Ungranted must not leak a filter value the reader might believe.
        check("ungranted reports the filter as unknown", RingerState.line(
                RingerState.RINGER_NORMAL, RingerState.FILTER_NONE, UNGRANTED)
                .equals("normal dnd=unknown granted=0"));
        check("no newline of its own — StatusServer adds it",
                RingerState.line(RingerState.RINGER_NORMAL,
                                 RingerState.FILTER_ALL, GRANTED).indexOf('\n') < 0);
    }

    private static void check(String what, boolean ok) {
        if (ok) { passed++; return; }
        failures.add(what);
    }
}
