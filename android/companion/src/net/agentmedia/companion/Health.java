package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;

/**
 * Is this thing working? — in the three or four words the home screen has room
 * for.
 *
 * <h4>Why a strip rather than a log</h4>
 *
 * The main screen used to answer this by showing everything: a status dump and
 * a thousand-line event log, re-rendered twice a second, from which a reader
 * could work out that the mic watch had died. It is the right raw material and
 * the wrong answer — the failures this app actually has are few, they are
 * known, and each one has a sentence. The log stays, one screen away, for the
 * question this cannot answer: <em>why</em>.
 *
 * The failures worth a pill, learned the hard way:
 *
 * <ul>
 *   <li><b>The mic watch.</b> The app is the only mic trigger since Automate was
 *       retired, so a dead watch is barge-in gone — silently, which is exactly
 *       how it stayed broken for a fortnight in August 2026.</li>
 *   <li><b>The bridges.</b> Three socat listeners carry the three channels. A
 *       missing one costs its card and its controls, and looks from the outside
 *       like the channel being idle.</li>
 *   <li><b>Deaths today.</b> Android stops this app whenever it likes. One is
 *       ordinary. Several in a day is a crash loop, and the difference between
 *       those two is the whole reason the exit history is read at startup.</li>
 * </ul>
 *
 * Green is the absence of news, so a healthy phone shows three quiet pills
 * rather than nothing at all: "nothing to report" and "not being reported" look
 * identical when the answer is blank, and this app has been the second one
 * often enough to have earned the distinction.
 *
 * android.*-free, so test/run.sh covers the deciding.
 */
final class Health {

    /** How a pill reads. The screen maps these to Style.OK / Style.WARN. */
    enum Level { OK, WARN }

    static final class Pill {
        final String text;
        final Level level;

        Pill(String text, Level level) {
            this.text = text;
            this.level = level;
        }
    }

    /** Deaths in a day past which this is a loop, not an event. */
    static final int DEATHS_WORTH_SAYING = 2;

    private Health() { }

    /**
     * The strip, left to right, most-likely-to-matter first.
     *
     * @param serviceUp   the activity is bound to a running service
     * @param micWatching the mic watch is started and answering
     * @param bridgesUp   how many of the three channel bridges are connected
     * @param deathsToday exits Android has recorded since midnight
     */
    static List<Pill> strip(boolean serviceUp, boolean micWatching,
                            int bridgesUp, int deathsToday) {
        List<Pill> out = new ArrayList<Pill>();

        // First, because nothing below it means anything if this is false: the
        // readout is talking to a service that is not there.
        if (!serviceUp) {
            out.add(new Pill("service down", Level.WARN));
            return out;
        }

        out.add(micWatching ? new Pill("mic watching", Level.OK)
                            : new Pill("mic watch dead", Level.WARN));

        if (bridgesUp >= 3) {
            out.add(new Pill("3 bridges", Level.OK));
        } else {
            out.add(new Pill(bridgesUp + " of 3 bridges", Level.WARN));
        }

        if (deathsToday >= DEATHS_WORTH_SAYING) {
            out.add(new Pill(deathsToday + " deaths today", Level.WARN));
        } else if (deathsToday == 1) {
            // Said, but quietly. One restart is how this app lives on Android;
            // hiding it would make the second one look like the first.
            out.add(new Pill("1 restart today", Level.OK));
        } else {
            out.add(new Pill("up all day", Level.OK));
        }
        return out;
    }

    /**
     * How many of Android's recorded exits happened today.
     *
     * The lines come from {@link ExitReason#describe}, which puts the date at
     * the front as {@code MM-DD}. Parsing our own formatting is not lovely, but
     * the alternative is a second trip to ActivityManager for a number that is
     * already sitting in a list — and the format is one this repo owns.
     */
    static int deathsOn(List<String> exits, String todayMonthDay) {
        if (exits == null || todayMonthDay == null) return 0;
        int n = 0;
        for (String line : exits) {
            if (line != null && line.startsWith(todayMonthDay)) n++;
        }
        return n;
    }
}
