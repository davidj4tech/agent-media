package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.Calendar;
import java.util.List;
import java.util.Locale;

/**
 * Turning a history into a list you can scan.
 *
 * Twenty-five rows of white-on-black at one weight is not a list, it is a wall:
 * finding the thing you half-remember means reading every line. What the eye
 * wants first is not the title at all — it is <em>when</em>, and <em>which
 * channel</em>, and those are the two things the rows already carry.
 *
 * So this class answers three questions the activity should not be answering
 * itself, all of them testable on the build host:
 *
 * <ul>
 *   <li><b>Where do the day breaks go?</b> Today, Yesterday, then the date.</li>
 *   <li><b>What time was that?</b> A clock time, not "3h ago" — "3h ago" is
 *       right in a terminal and useless in a list you are scanning down.</li>
 *   <li><b>What do we show when the title is not a title?</b> A signed URL and
 *       {@code remote-20260814T190922-18480.mp3} are both what the row was
 *       <em>stored</em> as, and neither is what it was.</li>
 * </ul>
 *
 * android.*-free, so test/run.sh covers it.
 */
final class RecentRows {

    /** A day break, or a row. The list is rendered straight down this. */
    static final class Entry {
        /** Non-null on a heading row; null on an item row. */
        final String heading;
        /** Non-null on an item row; null on a heading. */
        final RecentList.Item item;
        /** Clock time for an item, "" when the store had no time for it. */
        final String clock;

        private Entry(String heading, RecentList.Item item, String clock) {
            this.heading = heading;
            this.item = item;
            this.clock = clock;
        }

        boolean isHeading() { return heading != null; }
    }

    private RecentRows() { }

    /**
     * Interleave day headings into the rows.
     *
     * The rows arrive newest first and stay that way — this only inserts a
     * heading each time the day changes. A row with no timestamp gets no
     * heading of its own and keeps the one above it: an unknown time is not a
     * new day, and a "1970" section would be the list lying about it.
     *
     * @param nowMs the clock now, so "today" means today on the phone
     */
    static List<Entry> group(List<RecentList.Item> items, long nowMs) {
        List<Entry> out = new ArrayList<Entry>();
        if (items == null) return out;
        String open = null;
        for (RecentList.Item item : items) {
            long at = item.startedAtMs();
            if (at > 0) {
                String heading = heading(at, nowMs);
                if (!heading.equals(open)) {
                    out.add(new Entry(heading, null, null));
                    open = heading;
                }
            }
            out.add(new Entry(null, item, at > 0 ? clock(at) : ""));
        }
        return out;
    }

    /** "Today", "Yesterday", or "Mon 11 Aug" for anything older. */
    static String heading(long atMs, long nowMs) {
        int days = daysBetween(atMs, nowMs);
        if (days <= 0) return "Today";
        if (days == 1) return "Yesterday";
        Calendar c = at(atMs);
        return String.format(Locale.US, "%s %d %s",
                dayName(c.get(Calendar.DAY_OF_WEEK)),
                c.get(Calendar.DAY_OF_MONTH),
                monthName(c.get(Calendar.MONTH)));
    }

    /** 24-hour, because that is what the rest of this system speaks. */
    static String clock(long atMs) {
        Calendar c = at(atMs);
        return String.format(Locale.US, "%02d:%02d",
                c.get(Calendar.HOUR_OF_DAY), c.get(Calendar.MINUTE));
    }

    /**
     * What to show as the row's title.
     *
     * A label the far side gave us wins — it is the same string {@code media
     * recent} prints, and one history rendered two ways drifts. This only steps
     * in when the label is machine wreckage: a bare URL, or the rendered-file
     * name of a spoken clip, both of which say nothing about what the thing
     * was.
     */
    static String title(RecentList.Item item) {
        String label = item.title();
        if (label == null) return "";
        label = label.trim();
        if (isSpeechFile(label)) return "a spoken reply";
        if (isUrl(label)) {
            String host = host(label);
            return host.isEmpty() ? "a link" : "a link from " + host;
        }
        return label;
    }

    /**
     * The line under it. For a row that cannot be played again, this is where
     * it says so — the old list dimmed those to grey and left you to find out
     * by tapping, which is a list keeping a secret it has no reason to keep.
     */
    static String subtitle(RecentList.Item item) {
        if (!item.playable()) {
            if ("speech".equals(item.channel)) return "gone — the clip was temporary";
            return "cannot be played again";
        }
        return item.subtitle();
    }

    // ---- the awkward bits --------------------------------------------------

    private static boolean isUrl(String s) {
        String lower = s.toLowerCase(Locale.US);
        return lower.startsWith("http://") || lower.startsWith("https://");
    }

    /**
     * A rendered speech clip: {@code remote-20260814T190922-18480.mp3}.
     *
     * Matched on shape rather than on the prefix alone, because "remote-" is a
     * plausible start to a real title and the timestamp is not.
     */
    private static boolean isSpeechFile(String s) {
        if (!s.endsWith(".mp3") && !s.endsWith(".wav")) return false;
        int digits = 0;
        for (int i = 0; i < s.length(); i++) {
            if (Character.isDigit(s.charAt(i))) digits++;
        }
        return digits >= 12 && s.indexOf(' ') < 0;
    }

    private static String host(String url) {
        int start = url.indexOf("//");
        if (start < 0) return "";
        start += 2;
        int end = url.length();
        for (int i = start; i < url.length(); i++) {
            char ch = url.charAt(i);
            if (ch == '/' || ch == ':' || ch == '?') { end = i; break; }
        }
        String host = url.substring(start, end);
        return host.startsWith("www.") ? host.substring(4) : host;
    }

    private static Calendar at(long ms) {
        Calendar c = Calendar.getInstance();
        c.setTimeInMillis(ms);
        return c;
    }

    /** Calendar days between two instants — not 24-hour blocks. */
    private static int daysBetween(long atMs, long nowMs) {
        Calendar a = midnight(atMs);
        Calendar b = midnight(nowMs);
        long diff = b.getTimeInMillis() - a.getTimeInMillis();
        return (int) Math.round(diff / 86400000.0);
    }

    private static Calendar midnight(long ms) {
        Calendar c = at(ms);
        c.set(Calendar.HOUR_OF_DAY, 0);
        c.set(Calendar.MINUTE, 0);
        c.set(Calendar.SECOND, 0);
        c.set(Calendar.MILLISECOND, 0);
        return c;
    }

    private static String dayName(int dayOfWeek) {
        String[] names = {"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"};
        int i = dayOfWeek - Calendar.SUNDAY;
        return (i < 0 || i >= names.length) ? "" : names[i];
    }

    private static String monthName(int month) {
        String[] names = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
        return (month < 0 || month >= names.length) ? "" : names[month];
    }
}
