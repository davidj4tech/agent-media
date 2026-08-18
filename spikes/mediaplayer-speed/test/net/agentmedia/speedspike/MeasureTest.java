package net.agentmedia.speedspike;

import java.util.ArrayList;
import java.util.List;

/**
 * The arithmetic and the verdict, on the build host.
 *
 * Small, but not ceremonial: the case it exists for is the one the mpv bug
 * produced — a player reporting the speed it was asked for while the clock says
 * otherwise. A measurement that called that a pass would let the spike answer
 * "ship it" to the wrong question, and there is no adb on the phone to catch it
 * afterwards.
 */
public class MeasureTest {

    public static void main(String[] args) {
        int failures = 0;
        failures += check("a clean 1.6x passes",
                m(1.6, 1.6, 12800, 8000).passed());
        failures += check("the mpv failure (1.6 asked, 1.18 played) fails",
                !m(1.6, 1.6, 9440, 8000).passed());
        failures += check("and it is not the reported speed that decides",
                m(1.6, 1.6, 9440, 8000).reported == 1.6);
        failures += check("a 2% drift is inside tolerance",
                m(1.6, 1.6, 13056, 8000).passed());
        failures += check("a 10% drift is not",
                !m(1.6, 1.6, 14080, 8000).passed());
        failures += check("a thrown trial never passes",
                !new Measure("t", 1.6, -1, 12800, 8000, "IllegalArgumentException").passed());
        failures += check("too short a window is unmeasurable, not a pass",
                !m(1.6, 1.6, 1600, 1000).passed());
        failures += check("an unmeasurable rate is -1",
                m(1.6, 1.6, 1600, 1000).rate() == -1);

        List<Measure> all = new ArrayList<Measure>();
        all.add(m(1.0, 1.0, 8000, 8000));
        all.add(m(1.6, 1.6, 12800, 8000));
        failures += check("all-pass reads as the no-Gradle branch",
                Measure.verdict(all).contains("no-Gradle build survives"));
        all.add(m(2.0, 2.0, 9440, 8000));
        failures += check("one failure names it and points at Media3",
                Measure.verdict(all).contains("Media3")
                        && Measure.verdict(all).contains("2/3"));

        System.out.println(failures == 0 ? "MeasureTest ok" : failures + " failed");
        if (failures != 0) System.exit(1);
    }

    private static Measure m(double want, double said, long pos, long wall) {
        return new Measure("t", want, said, pos, wall, null);
    }

    private static int check(String what, boolean ok) {
        System.out.println((ok ? "  ok   " : "  FAIL ") + what);
        return ok ? 0 : 1;
    }
}
