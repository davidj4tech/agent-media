package net.agentmedia.companion;

import android.app.ActivityManager;
import android.app.ApplicationExitInfo;
import android.content.Context;

import java.util.ArrayList;
import java.util.List;

/**
 * Ask Android why the last process died, and say so at startup.
 *
 * The Android-touching half of {@link ExitReason}. Everything it needs is
 * available to the app about its own package with no permission at all, which
 * is what makes it the one route open on this phone.
 *
 * Read once in onCreate and cached: the answer is about a process that has
 * already ended and cannot change, and a readout that re-queries the framework
 * on every `/state` poll would be paying for a constant.
 */
final class LastExit {

    /**
     * How many deaths to keep. A crash loop is a sequence — three identical
     * reasons and one different first one is a different story from three
     * different ones — and the same argument the crash file makes by appending.
     */
    private static final int KEEP = 5;

    private LastExit() { }

    /**
     * The recent exits, newest first, one formatted line each; empty if Android
     * has nothing (a first run, or a reboot since).
     *
     * Never throws. This is a diagnostic, and a diagnostic that can take the app
     * down is worse than no diagnostic — the same rule the cards are held to.
     */
    static List<String> read(Context ctx) {
        List<String> out = new ArrayList<String>();
        try {
            ActivityManager am = ctx.getSystemService(ActivityManager.class);
            if (am == null) return out;
            List<ApplicationExitInfo> infos = am.getHistoricalProcessExitReasons(
                    ctx.getPackageName(), 0, KEEP);
            if (infos == null) return out;
            for (ApplicationExitInfo i : infos) {
                out.add(ExitReason.describe(i.getTimestamp(), i.getReason(),
                        i.getStatus(), i.getImportance(), i.getDescription()));
            }
        } catch (Throwable ignored) {
            // Deliberately silent: see above.
        }
        return out;
    }

    /**
     * The line the event log gets at startup, so "why did it go away?" is
     * answered by the log that is already being read rather than by remembering
     * to ask a second question.
     */
    static void log(List<String> exits) {
        if (exits.isEmpty()) {
            CompanionService.log("last exit: (Android has no record — first run "
                    + "or a reboot since)");
            return;
        }
        CompanionService.log("last exit: " + exits.get(0));
        for (int i = 1; i < exits.size(); i++) {
            CompanionService.log("  before that: " + exits.get(i));
        }
    }
}
