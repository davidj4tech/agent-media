package net.agentmedia.speedspike;

import android.content.Context;

/**
 * The run, held somewhere the activity's lifecycle cannot take it.
 *
 * Everything used to hang off {@code MainActivity}, and the first automated run
 * showed why that is wrong. From logcat, now that adb gives us shell uid:
 *
 * <pre>
 *   08:26:55 WindowManager: Task{...speedspike} m=TO_BACK
 *   08:28:08 ActivityManager: freezing 18699 net.agentmedia.speedspike
 * </pre>
 *
 * The activity went to the background when David used his phone for something
 * else, and about a minute later Android's cached-app freezer suspended the
 * process — threads stopped, the trials stopped mid-run, and the readout socket
 * stopped answering. Nothing crashed; a frozen process simply is not running.
 *
 * That is a finding about the shipping design, not about the spike: <b>an
 * in-app speech player has to live in a foreground service</b> with a media
 * notification, exactly as the companion's already does. A player that only
 * works while its screen is up would fail every time a reply arrives while
 * David is reading something else, which is most of them.
 *
 * So the state lives here, static, and {@link SpikeService} keeps the process
 * out of the freezer while a run is in flight.
 */
final class Spike {

    private static final StringBuilder LOG = new StringBuilder();
    private static SpeedTrials trials;
    private static Readout readout;

    private Spike() { }

    static synchronized SpeedTrials trials(Context context) {
        if (trials == null) {
            trials = new SpeedTrials(context.getCacheDir(), Spike::log);
            readout = new Readout(Spike::report);
            readout.start();
        }
        return trials;
    }

    static void log(String line) {
        synchronized (LOG) {
            LOG.append(line).append('\n');
        }
    }

    static void clear() {
        synchronized (LOG) {
            LOG.setLength(0);
        }
    }

    static String report() {
        synchronized (LOG) {
            return LOG.length() == 0 ? "(no run yet)" : LOG.toString();
        }
    }
}
