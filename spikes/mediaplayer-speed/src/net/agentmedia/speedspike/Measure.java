package net.agentmedia.speedspike;

/**
 * What a trial asked for, what came back, and whether that is good enough.
 *
 * <h4>Why the effective rate is measured rather than trusted</h4>
 *
 * The other player in this project has already failed here in a way that reads
 * as success from the outside: a pinned {@code scaletempo2} filter never sees a
 * new speed, so mpv reports 1.6 while the audio advances at 1.18. Nothing threw,
 * nothing logged, and the only symptom was a reply that took longer than it
 * should have. Asking {@code MediaPlayer.getPlaybackParams()} what the speed is
 * would reproduce exactly that mistake — it reports what was accepted, not what
 * the clock is doing.
 *
 * So a trial measures media-position advance against wall clock:
 * {@code rate = dPosition / dWall}. That is the number a listener actually
 * experiences, and it is the one that decides whether Media3 becomes mandatory.
 *
 * {@code android.*}-free, so {@code test/run.sh} covers the arithmetic and the
 * verdict on the build host — where every other test in this project runs,
 * because p8a has no adb and a device run costs a sideload and a tap.
 */
final class Measure {

    /**
     * How far off the requested speed a trial may land and still pass.
     *
     * 4% is well inside what a listener notices on a sentence-length clip and
     * well outside the 26% error the mpv bug produced, which is the failure
     * this threshold exists to catch. Sampling jitter over an eight-second
     * window is a fraction of a percent, so the band is about the player, not
     * about the measurement.
     */
    static final double TOLERANCE = 0.04;

    /** Below this much elapsed wall time a rate is noise, not a measurement. */
    static final long MIN_WALL_MS = 2000;

    final String name;
    final double requested;
    /** What getPlaybackParams() reported back, or -1 if it was not read. */
    final double reported;
    final long positionMs;
    final long wallMs;
    /** Whatever went wrong, or null. A thrown fallback-FAIL lands here. */
    final String error;
    /**
     * How many times the player stalled to rebuffer inside the window.
     *
     * Added after the first device run, where the 1.0x control measured 0.904
     * and the honest reading was ambiguous: a player that cannot hold a speed
     * and a player waiting on a slow network produce the same number. Counting
     * the stalls separates them, because the argument "it was only buffering"
     * is exactly the argument a spike should not be making on trust.
     */
    final int stalls;

    Measure(String name, double requested, double reported,
            long positionMs, long wallMs, String error) {
        this(name, requested, reported, positionMs, wallMs, error, 0);
    }

    Measure(String name, double requested, double reported,
            long positionMs, long wallMs, String error, int stalls) {
        this.name = name;
        this.requested = requested;
        this.reported = reported;
        this.positionMs = positionMs;
        this.wallMs = wallMs;
        this.error = error;
        this.stalls = stalls;
    }

    /** Media time advanced per second of wall clock, or -1 if unmeasurable. */
    double rate() {
        if (wallMs < MIN_WALL_MS || positionMs < 0) return -1;
        return (double) positionMs / (double) wallMs;
    }

    /** How far the measured rate sits from the requested one, as a fraction. */
    double drift() {
        double r = rate();
        if (r < 0 || requested <= 0) return -1;
        return Math.abs(r - requested) / requested;
    }

    boolean passed() {
        double d = drift();
        return error == null && d >= 0 && d <= TOLERANCE;
    }

    /**
     * Did this trial miss its speed only because the network made it wait?
     *
     * A rebuffer inside the window costs media time that the player never had
     * bytes for, so the rate reads low through no fault of the engine. That is
     * a finding about the transport — and it is the reason the shipping design
     * fetches a clip before playing it rather than streaming — but it is not a
     * reason to reach for Media3, which would stall on the same bytes.
     */
    boolean stalledNotSlow() {
        return error == null && stalls > 0 && rate() >= 0 && rate() < requested;
    }

    /**
     * One line, aligned enough to scan down a column on a phone screen.
     *
     * The reported speed is printed next to the measured one on purpose: when
     * they disagree, that disagreement <em>is</em> the finding.
     */
    String line() {
        StringBuilder b = new StringBuilder();
        b.append(passed() ? "PASS  " : (error != null ? "ERROR " : "FAIL  "));
        b.append(name).append("  want ").append(fmt(requested));
        if (reported >= 0) b.append("  said ").append(fmt(reported));
        double r = rate();
        if (r >= 0) {
            b.append("  measured ").append(fmt(r))
             .append("  (").append(Math.round(drift() * 1000) / 10.0)
             .append("% off, ").append(positionMs).append("ms in ")
             .append(wallMs).append("ms)");
        } else {
            b.append("  measured -- (").append(positionMs).append("ms in ")
             .append(wallMs).append("ms)");
        }
        if (stalls > 0) b.append("  [").append(stalls).append(" rebuffer")
                .append(stalls == 1 ? "" : "s").append("]");
        if (error != null) b.append("  ").append(error);
        return b.toString();
    }

    private static String fmt(double d) {
        return String.valueOf(Math.round(d * 1000) / 1000.0);
    }

    /**
     * The one sentence the handover asks for: does the platform player hold a
     * requested speed, or does Media3 become mandatory?
     *
     * Deliberately blunt. A spike that reports nuance is a spike that gets
     * re-litigated; the toolchain decision downstream of this is binary.
     */
    static String verdict(Iterable<Measure> trials) {
        int total = 0, passed = 0;
        StringBuilder bad = new StringBuilder();
        int stalled = 0;
        for (Measure m : trials) {
            total++;
            if (m.passed()) {
                passed++;
            } else if (m.stalledNotSlow()) {
                stalled++;
            } else {
                if (bad.length() > 0) bad.append(", ");
                bad.append(m.name);
            }
        }
        if (total == 0) return "no trials run";
        String stalls = stalled == 0 ? "" : " " + stalled
                + " missed only by rebuffering, which is the transport, not the"
                + " player: fetch the clip before playing it.";
        if (passed + stalled == total) {
            return "MediaPlayer holds the requested speed on all " + total
                    + " trials — the no-Gradle build survives." + stalls;
        }
        return passed + "/" + total + " trials held the requested speed; "
                + bad + " did not." + stalls + " Judge the ear test before "
                + "concluding, then see the handover: this is the Media3 branch.";
    }
}
