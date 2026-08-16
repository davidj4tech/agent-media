package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * What played lately, and how to play it again.
 *
 * The history itself lives in agent-media's SQLite store, written wherever each
 * channel already remembered what it was playing. This class is the phone's
 * view of it: fetch {@code /recent}, turn the rows into something a list can
 * render, and post a tapped row back to {@code /play}.
 *
 * The rows arrive pre-labelled on purpose. `media recent` in the terminal and
 * this list show the same string for the same item, because the label is
 * computed once on the Python side — two renderings of one history drift, and
 * the phone is the harder one to check.
 *
 * Replay does not re-classify. The row carries the channel and content type it
 * played under, so tapping "a 90-minute lecture" puts it back on the book
 * channel without another yt-dlp round trip that might land somewhere else.
 *
 * {@code android.*}-free, so {@code test/run.sh} covers it.
 */
final class RecentList {

    /** One row: what it was, where it played, and enough to repeat it. */
    static final class Item {
        final String label;
        final String channel;
        final String contentType;
        final String uri;
        final String ago;
        /**
         * When it played, epoch seconds; 0 when the store had no time for it.
         *
         * Alongside {@code ago} rather than instead of it: "18m ago" is what a
         * terminal wants, and a list that groups by day and shows a clock time
         * cannot get either back out of it.
         */
        final double startedAt;

        Item(String label, String channel, String contentType, String uri, String ago) {
            this(label, channel, contentType, uri, ago, 0.0);
        }

        Item(String label, String channel, String contentType, String uri,
             String ago, double startedAt) {
            this.label = label;
            this.channel = channel;
            this.contentType = contentType;
            this.uri = uri;
            this.ago = ago;
            this.startedAt = startedAt;
        }

        /** When it played, in milliseconds; 0 for "the store did not say". */
        long startedAtMs() {
            return startedAt <= 0 ? 0L : (long) (startedAt * 1000.0);
        }

        /** The line the list shows: what it was. */
        String title() {
            return label == null || label.isEmpty() ? uri : label;
        }

        /** The line under it: where it played and how long ago. */
        String subtitle() {
            StringBuilder sb = new StringBuilder();
            if (ago != null && !ago.isEmpty()) sb.append(ago).append(" ago");
            if (channel != null && !channel.isEmpty()) {
                if (sb.length() > 0) sb.append(" · ");
                sb.append(channel);
            }
            if (contentType != null && !contentType.isEmpty()
                    && !contentType.equals(channel)) {
                sb.append(" (").append(contentType).append(")");
            }
            return sb.toString();
        }

        /** True when this row can be played again. Speech clips cannot. */
        boolean playable() {
            return uri != null && !uri.isEmpty()
                    && ("music".equals(channel) || "book".equals(channel));
        }
    }

    private RecentList() {}

    /**
     * Parse a {@code /recent} reply into rows.
     *
     * Never throws: a listing that cannot be shown should be an empty list and
     * a message, not a crash in the activity that was opened to read it.
     */
    static List<Item> parse(String payload) {
        List<Item> out = new ArrayList<Item>();
        try {
            Map<String, Object> obj = Json.parseObject(payload);
            Object rows = obj.get("rows");
            if (!(rows instanceof List)) return out;
            for (Object row : (List<?>) rows) {
                if (!(row instanceof Map)) continue;
                Map<?, ?> r = (Map<?, ?>) row;
                out.add(new Item(str(r.get("label")), str(r.get("channel")),
                                 str(r.get("content_type")), str(r.get("uri")),
                                 str(r.get("ago")),
                                 Json.asDouble(r.get("started_at"), 0.0)));
            }
        } catch (RuntimeException e) {
            return Collections.emptyList();
        }
        return out;
    }

    /** The body {@code /play} wants for this row. */
    static String playBody(Item item) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("uri", item.uri == null ? "" : item.uri);
        m.put("channel", item.channel == null ? "music" : item.channel);
        m.put("content_type", item.contentType == null ? "" : item.contentType);
        m.put("title", item.label == null ? "" : item.label);
        return Json.write(m);
    }

    /** Fetch the list. Returns empty when the listener cannot be reached. */
    static List<Item> fetch(int port, int limit) {
        Loopback.Reply r = Loopback.get(port, "/recent?limit=" + limit);
        return r.ok() ? parse(r.body) : Collections.<Item>emptyList();
    }

    /** Why the list is empty, in words a person can act on. */
    static String emptyReason(Loopback.Reply r) {
        if (r == null) return "";
        if (!r.reached()) return r.failure;
        if (!r.ok()) return "agent-media: the listener said " + r.status;
        return "nothing played yet";
    }

    /** Play a row again; returns the line to show. */
    static String play(int port, Item item) {
        if (!item.playable()) {
            return "agent-media: that one cannot be replayed";
        }
        Loopback.Reply r = Loopback.post(port, "/play", playBody(item));
        if (!r.reached()) return r.failure;
        String line = "";
        try {
            Map<String, Object> o = Json.parseObject(r.body);
            line = str(o.get("line"));
            if (line.isEmpty()) line = str(o.get("error"));
        } catch (RuntimeException e) {
            line = "";
        }
        if (!line.isEmpty()) return line;
        return r.ok() ? "playing " + item.title()
                      : "agent-media: could not play that (HTTP " + r.status + ")";
    }

    private static String str(Object v) {
        String s = Json.asString(v);
        return s == null ? "" : s;
    }
}
