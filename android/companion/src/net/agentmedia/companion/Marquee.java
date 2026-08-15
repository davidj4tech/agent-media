package net.agentmedia.companion;

/**
 * A scrolling window into a title too long for the card.
 *
 * The phone's card gives a title about thirty characters before it ellipsises,
 * and an audiobook does not fit in thirty characters: "FULL AUDIOBOOK - Patrick
 * Rothfuss - Kingkiller Chronicle #1 - The Name of the Wind" cuts off somewhere
 * in the author's surname, which is the half you already knew.
 *
 * Deliberately the same semantics as {@code _marquee} in cli.py, which does
 * this for the tmux status line and the popup — the offset is derived from how
 * long the current text has been showing rather than from a counter bumped once
 * per redraw. Here that buys the thing it bought there: three cards scrolling
 * off one clock stay in phase and at one speed, however often each happens to
 * be republished.
 */
final class Marquee {

    /** Columns a card title gets before Android ellipsises it. Measured, not derived. */
    static final int WIDTH = 30;

    /** Columns per second. One is readable and is what the status line uses. */
    static final double CPS = 1.0;

    /** The run between the end of the text and its start coming round again. */
    static final String GAP = "   ";

    private Marquee() { }

    /** True if the text needs scrolling at all — the common case is that it does not. */
    static boolean needed(String text, int width) {
        return !collapse(text).isEmpty() && width > 0 && collapse(text).length() > width;
    }

    /**
     * A `width`-wide window into `text`, `elapsedMs` into its crawl.
     *
     * Text that already fits comes back whole and unmoving: a card that fits is
     * a card that should never animate, and that is also what keeps the cost of
     * this feature at zero for every ordinary title.
     */
    static String window(String text, int width, long elapsedMs) {
        return window(text, width, elapsedMs, CPS, GAP);
    }

    static String window(String text, int width, long elapsedMs,
                         double cps, String gap) {
        String t = collapse(text);
        if (t.isEmpty() || width <= 0) return "";
        if (t.length() <= width) return t;
        if (elapsedMs < 0) elapsedMs = 0;
        double rate = cps <= 0 ? CPS : cps;

        String loop = t + gap;
        int off = (int) ((elapsedMs / 1000.0) * rate % loop.length());
        String twice = loop + loop;
        return twice.substring(off, off + width);
    }

    /** Runs of whitespace become one space, as the card would render them anyway. */
    static String collapse(String text) {
        return text == null ? "" : text.trim().replaceAll("\\s+", " ");
    }
}
