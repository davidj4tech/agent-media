package net.agentmedia.companion;

/**
 * The mpv properties this app mirrors, kept as one snapshot.
 *
 * Deliberately the same predicates the Python side uses
 * (agent_media_core.sinks.music_local): {@code loaded()} is "a file is open,
 * playing or paused" and {@code playing()} adds "and not paused". Keeping the
 * two definitions identical is what lets the router and the session agree about
 * what the phone is doing.
 *
 * android.*-free so it can be unit-tested on the build host.
 */
final class MpvState {

    volatile boolean connected = false;
    /** True when mpv has no file open. Assume idle until told otherwise. */
    volatile boolean idleActive = true;
    volatile boolean paused = false;
    volatile String mediaTitle = null;
    volatile String path = null;
    /** Seconds; NaN when unknown. */
    volatile double duration = Double.NaN;
    volatile double speed = 1.0;
    volatile double volume = 100.0;
    /** Seconds; NaN when unknown. Polled, not observed — see MpvIpc. */
    volatile double position = Double.NaN;
    /**
     * Speech mirror only: the coordinator says a response is in flight. See
     * MpvIpc.SPEAKING_PROPERTY.
     */
    volatile boolean speaking = false;
    /**
     * Speech mirror only: what this reply is worth interrupting for. See
     * MpvIpc.PRIORITY_PROPERTY. Absent reads as "normal" — a coordinator too
     * old to say, and an ordinary answer, deserve the same treatment.
     */
    volatile String priority = "normal";
    /** Clips on the broker's playlist, the open one included. See MpvIpc. */
    volatile int queued = 0;

    /** Apply one property update. Returns true when something actually changed. */
    boolean apply(String name, Object value) {
        switch (name) {
            case "idle-active": {
                boolean v = Json.asBool(value, true);
                if (v == idleActive) return false;
                idleActive = v;
                return true;
            }
            case "pause": {
                boolean v = Json.asBool(value, false);
                if (v == paused) return false;
                paused = v;
                return true;
            }
            case "media-title": {
                String v = Json.asString(value);
                if (eq(v, mediaTitle)) return false;
                mediaTitle = v;
                remember(v);
                return true;
            }
            case "path": {
                String v = Json.asString(value);
                if (eq(v, path)) return false;
                path = v;
                if (mediaTitle == null || mediaTitle.isEmpty()) {
                    remember(v == null || v.isEmpty() ? null : basename(v));
                }
                return true;
            }
            case "duration": {
                double v = Json.asDouble(value, Double.NaN);
                if (sameNumber(v, duration)) return false;
                duration = v;
                return true;
            }
            case "speed": {
                double v = Json.asDouble(value, 1.0);
                if (sameNumber(v, speed)) return false;
                speed = v;
                return true;
            }
            case "volume": {
                double v = Json.asDouble(value, 100.0);
                if (sameNumber(v, volume)) return false;
                volume = v;
                return true;
            }
            case MpvIpc.SPEAKING_PROPERTY: {
                // Absent (null) reads as false: an mpv that has never been told
                // is one whose coordinator does not speak this, and the caller
                // falls back to its own heuristics.
                boolean v = Json.asBool(value, false);
                if (v == speaking) return false;
                speaking = v;
                return true;
            }
            case MpvIpc.PRIORITY_PROPERTY: {
                String v = Json.asString(value);
                if (v == null || v.trim().isEmpty()) v = "normal";
                if (v.equals(priority)) return false;
                priority = v;
                return true;
            }
            case MpvIpc.QUEUE_PROPERTY: {
                int v = (int) Json.asDouble(value, 0);
                if (v == queued) return false;
                queued = v;
                return true;
            }
            case MpvIpc.POSITION_PROPERTY: {
                position = Json.asDouble(value, Double.NaN);
                // Position never counts as a state change on its own: it moves
                // constantly and the session extrapolates between updates.
                return false;
            }
            default:
                return false;
        }
    }

    /** A file is open — playing or paused. */
    boolean loaded() {
        return connected && !idleActive;
    }

    /** A file is open and running. */
    boolean playing() {
        return loaded() && !paused;
    }

    /** What the lock screen and the car display should say. */
    String title() {
        // Blank counts as absent: mpv reports a cleared title as "" and, on at
        // least one path, as a run of spaces — which used to render as a card
        // with no name on it at all.
        if (mediaTitle != null && !mediaTitle.trim().isEmpty()) return mediaTitle;
        if (path != null && !path.trim().isEmpty()) return basename(path);
        if (lastTitle != null) return lastTitle;
        return "agent-media";
    }

    /**
     * The last thing this channel actually played, kept after it stops.
     *
     * A channel that goes idle used to forget: the card fell back to
     * "agent-media" and the shade lost the one piece of information worth
     * having there, which is what you just listened to. mpv clears
     * {@code media-title} and {@code path} on end-file, so nothing else on this
     * side can answer it a second later.
     *
     * Only ever set, never cleared — an empty title arriving is the end of a
     * clip, not a new one, and forgetting on it would defeat the whole point.
     */
    private volatile String lastTitle = null;

    /** The last played title, or null if this channel has played nothing. */
    String lastTitle() { return lastTitle; }

    private void remember(String t) {
        if (t != null && !t.trim().isEmpty()) lastTitle = t.trim();
    }

    /** Duration in milliseconds, or -1 when mpv has not reported one. */
    long durationMs() {
        return (Double.isNaN(duration) || duration <= 0) ? -1L : (long) (duration * 1000.0);
    }

    long positionMs() {
        return Double.isNaN(position) ? 0L : (long) (position * 1000.0);
    }

    static String basename(String p) {
        int slash = p.lastIndexOf('/');
        String base = (slash >= 0) ? p.substring(slash + 1) : p;
        return base.isEmpty() ? p : base;
    }

    private static boolean eq(String a, String b) {
        return (a == null) ? (b == null) : a.equals(b);
    }

    private static boolean sameNumber(double a, double b) {
        if (Double.isNaN(a) && Double.isNaN(b)) return true;
        return a == b;
    }

    @Override
    public String toString() {
        return (connected ? "connected" : "disconnected")
                + (idleActive ? " idle" : (paused ? " paused" : " playing"))
                + " title=" + title()
                + " pos=" + (Double.isNaN(position) ? "?" : String.format("%.0f", position))
                + "/" + (Double.isNaN(duration) ? "?" : String.format("%.0f", duration))
                + " vol=" + (int) volume + " speed=" + speed;
    }
}
