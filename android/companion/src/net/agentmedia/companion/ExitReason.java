package net.agentmedia.companion;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * How the previous process died, in words — the half of {@link Crash} that
 * Crash cannot cover.
 *
 * The crash recorder only fires for an uncaught exception, which means it is
 * silent for every death the app does not throw: killed for memory, stopped by
 * the user, ANR'd, frozen, reaped when a dependency went. On 2026-08-15 at
 * about 19:51 the service simply stopped answering on 8770 and left nothing in
 * Downloads at all. That absence is itself a diagnosis — it says "not an
 * exception" — but it says nothing about which of the other eight it was.
 *
 * Android knows, and will tell the app about its own process without any
 * permission (ActivityManager#getHistoricalProcessExitReasons). Nobody else on
 * this phone will: logcat from Termux shows only Termux's uid and adb cannot
 * reach p8a, so the app asking after its own corpse is the only route there is.
 *
 * This class is the pure half so it can be tested off the device; {@link
 * LastExit} does the asking. Same split as FocusPolicy and FocusControl.
 */
final class ExitReason {

    // Mirrors android.app.ApplicationExitInfo, which the host tests cannot see.
    static final int UNKNOWN = 0;
    static final int EXIT_SELF = 1;
    static final int SIGNALED = 2;
    static final int LOW_MEMORY = 3;
    static final int CRASH = 4;
    static final int CRASH_NATIVE = 5;
    static final int ANR = 6;
    static final int INITIALIZATION_FAILURE = 7;
    static final int PERMISSION_CHANGE = 8;
    static final int EXCESSIVE_RESOURCE_USAGE = 9;
    static final int USER_REQUESTED = 10;
    static final int USER_STOPPED = 11;
    static final int DEPENDENCY_DIED = 12;
    static final int OTHER = 13;
    static final int FREEZER = 14;
    static final int PACKAGE_STATE_CHANGE = 15;
    static final int PACKAGE_UPDATED = 16;

    private ExitReason() { }

    /** The reason code as a name, or the raw number if Android grows a new one. */
    static String name(int reason) {
        switch (reason) {
            case UNKNOWN: return "UNKNOWN";
            case EXIT_SELF: return "EXIT_SELF";
            case SIGNALED: return "SIGNALED";
            case LOW_MEMORY: return "LOW_MEMORY";
            case CRASH: return "CRASH";
            case CRASH_NATIVE: return "CRASH_NATIVE";
            case ANR: return "ANR";
            case INITIALIZATION_FAILURE: return "INITIALIZATION_FAILURE";
            case PERMISSION_CHANGE: return "PERMISSION_CHANGE";
            case EXCESSIVE_RESOURCE_USAGE: return "EXCESSIVE_RESOURCE_USAGE";
            case USER_REQUESTED: return "USER_REQUESTED";
            case USER_STOPPED: return "USER_STOPPED";
            case DEPENDENCY_DIED: return "DEPENDENCY_DIED";
            case OTHER: return "OTHER";
            case FREEZER: return "FREEZER";
            case PACKAGE_STATE_CHANGE: return "PACKAGE_STATE_CHANGE";
            case PACKAGE_UPDATED: return "PACKAGE_UPDATED";
            default: return "reason " + reason;
        }
    }

    /**
     * Whether this death is one the app could have recorded itself.
     *
     * Used to say so out loud: a CRASH with no file in Downloads means the
     * recorder failed, which is a different bug from the crash. Every other
     * reason explains an empty Downloads directory rather than contradicting it.
     */
    static boolean wouldHaveLeftATrace(int reason) {
        return reason == CRASH;
    }

    /**
     * One line for the event log. Deliberately flat text rather than a record
     * type: it is read through `/log` and `/state` over curl, beside lines like
     * `focus: LOSS`, and it is read by eye.
     *
     * `importance` is the running-importance at the moment of death — the field
     * that separates "Android reaped a background process" from the alarming
     * case, a foreground service killed while it was still in the foreground.
     */
    static String describe(long timestampMs, int reason, int status,
                           int importance, String description) {
        StringBuilder b = new StringBuilder();
        b.append(new SimpleDateFormat("MM-dd HH:mm:ss", Locale.US)
                .format(new Date(timestampMs)));
        b.append("  ").append(name(reason));
        if (status != 0) b.append(" status=").append(status);
        b.append(" importance=").append(importance);
        if (description != null && !description.isEmpty()) {
            b.append(" — ").append(description);
        }
        return b.toString();
    }
}
