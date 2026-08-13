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
                return true;
            }
            case "path": {
                String v = Json.asString(value);
                if (eq(v, path)) return false;
                path = v;
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
        if (mediaTitle != null && !mediaTitle.isEmpty()) return mediaTitle;
        if (path != null && !path.isEmpty()) return basename(path);
        return "agent-media";
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
