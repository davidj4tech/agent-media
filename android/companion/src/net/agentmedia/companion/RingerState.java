package net.agentmedia.companion;

/**
 * Does this phone want to be spoken to?
 *
 * <p>agent-media speaks unattended alerts — a morning agenda digest, a watcher,
 * mail arriving — and until now it spoke them into a phone deliberately set to
 * silent, which is the whole stack working exactly as designed and being wrong
 * anyway. The host that decides lives elsewhere and cannot see this; only an
 * app on the device can answer.
 *
 * <h3>Two questions, because one is not enough</h3>
 *
 * {@code AudioManager.getRingerMode()} answers the <i>ringer switch</i>:
 * silent, vibrate, normal. On modern Android, Do Not Disturb does not move it —
 * a phone in DND reports {@code normal} while being, in every sense the person
 * holding it means, on silent. The API for that is
 * {@code NotificationManager.getCurrentInterruptionFilter()}, which needs
 * {@code ACCESS_NOTIFICATION_POLICY}: a user grant through a settings intent,
 * and notably <b>not</b> the notification-listener access Play Protect refuses
 * to sideloaded apps.
 *
 * <p>So {@code /ringer} reports both, and the reader decides from both. Vibrate
 * counts as quiet: a phone that answers a call with a buzz is not a phone that
 * wants a paragraph of agenda read aloud to the room.
 *
 * <h3>Why the grant is reported and not assumed</h3>
 *
 * Without {@code ACCESS_NOTIFICATION_POLICY} the filter reads as
 * {@code INTERRUPTION_FILTER_UNKNOWN}, which is indistinguishable from "no DND"
 * to anyone who does not know whether the grant is held. Reporting
 * {@code granted=0} is what lets the reader ignore the field rather than read
 * an unanswered question as "not in DND" — a distinction worth a byte on the
 * wire, because the failure it prevents is an alert silently withheld.
 *
 * <p>Kept free of android.* so the formatting and the constants are testable on
 * the build host; the service supplies the two integers. Same arrangement as
 * {@link FocusPolicy}.
 */
final class RingerState {

    /**
     * Mirrors android.media.AudioManager. Duplicated rather than imported so
     * this class stays host-testable; CompanionService asserts they match.
     */
    static final int RINGER_SILENT = 0;
    static final int RINGER_VIBRATE = 1;
    static final int RINGER_NORMAL = 2;

    /** Mirrors android.app.NotificationManager, same reasoning. */
    static final int FILTER_UNKNOWN = 0;
    static final int FILTER_ALL = 1;
    static final int FILTER_PRIORITY = 2;
    static final int FILTER_NONE = 3;
    static final int FILTER_ALARMS = 4;

    private RingerState() { }

    static String modeName(int ringerMode) {
        switch (ringerMode) {
            case RINGER_SILENT:  return "silent";
            case RINGER_VIBRATE: return "vibrate";
            case RINGER_NORMAL:  return "normal";
            // Not "silent". An unrecognised mode is a phone we do not
            // understand, and the one thing we must never do is withhold a
            // person's alerts on the strength of a number nobody has seen.
            default:             return "unknown";
        }
    }

    static String filterName(int filter) {
        switch (filter) {
            case FILTER_ALL:      return "all";
            case FILTER_PRIORITY: return "priority";
            case FILTER_NONE:     return "none";
            case FILTER_ALARMS:   return "alarms";
            default:              return "unknown";
        }
    }

    /**
     * The line {@code GET /ringer} answers with.
     *
     * <pre>
     *   silent dnd=priority granted=1
     *   normal dnd=unknown granted=0
     * </pre>
     *
     * Mode first so a human reading it over ssh gets the answer in the first
     * word, and so the parser can take {@code split()[0]} the way {@code /mic}
     * does. The rest is loose {@code key=value}, which is what lets a later
     * field appear without a version negotiation between an APK on a phone and
     * a Python package on a server that update on entirely different days.
     */
    static String line(int ringerMode, int filter, boolean policyGranted) {
        return modeName(ringerMode)
                + " dnd=" + (policyGranted ? filterName(filter) : "unknown")
                + " granted=" + (policyGranted ? "1" : "0");
    }

    /**
     * The same decision the reader makes, kept here so both ends agree and the
     * app's own diagnostics can show what it thinks without asking red5.
     *
     * <p>Either ground is sufficient and neither is inferred from the other's
     * absence: an ungranted filter contributes nothing at all, and an unknown
     * ringer mode is not quiet.
     */
    static boolean quiet(int ringerMode, int filter, boolean policyGranted) {
        if (ringerMode == RINGER_SILENT || ringerMode == RINGER_VIBRATE) {
            return true;
        }
        if (!policyGranted) return false;
        return filter != FILTER_ALL && filter != FILTER_UNKNOWN;
    }
}
