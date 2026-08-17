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
        /**
         * The speech history id, or 0.
         *
         * A spoken turn has no uri worth keeping — the clips live wherever
         * they were rendered, which is not always this phone — so the id is
         * the whole of the handle, and the door is {@code /control speech
         * chapter}, the same one the clip picker taps.
         */
        final long id;
        /**
         * Which conversation said it, and what that conversation is called.
         *
         * Speech only, and empty everywhere else — a track has no conversation.
         * The id is the grouping key because it survives a session being
         * resumed into another window; the name is only what to write on it.
         */
        final String session;
        final String window;
        /**
         * Where tmux says it came from: the server session, and the pane.
         *
         * The session is the place — one of them holds several conversations
         * and outlives all of them — and the pane is the conversation within
         * it. A list that has both can nest them; one that has neither cannot.
         */
        final String tmux;
        final String pane;

        Item(String label, String channel, String contentType, String uri, String ago) {
            this(label, channel, contentType, uri, ago, 0.0, 0L);
        }

        Item(String label, String channel, String contentType, String uri,
             String ago, double startedAt) {
            this(label, channel, contentType, uri, ago, startedAt, 0L);
        }

        Item(String label, String channel, String contentType, String uri,
             String ago, double startedAt, long id) {
            this(label, channel, contentType, uri, ago, startedAt, id,
                 "", "", "", "");
        }

        Item(String label, String channel, String contentType, String uri,
             String ago, double startedAt, long id, String session, String window) {
            this(label, channel, contentType, uri, ago, startedAt, id,
                 session, window, "", "");
        }

        Item(String label, String channel, String contentType, String uri,
             String ago, double startedAt, long id, String session, String window,
             String tmux, String pane) {
            this.session = session == null ? "" : session;
            this.window = window == null ? "" : window;
            this.tmux = tmux == null ? "" : tmux;
            this.pane = pane == null ? "" : pane;
            this.label = label;
            this.channel = channel;
            this.contentType = contentType;
            this.uri = uri;
            this.ago = ago;
            this.startedAt = startedAt;
            this.id = id;
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

        /**
         * True when this row can be heard again.
         *
         * Speech rows could not, for as long as this list only knew how to
         * post a uri to {@code /play}. They can: a turn is replayed by id, and
         * the row now carries one.
         */
        boolean playable() {
            if ("speech".equals(channel)) return id > 0;
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
                                 Json.asDouble(r.get("started_at"), 0.0),
                                 (long) Json.asDouble(r.get("id"), 0.0),
                                 str(r.get("session")), str(r.get("window")),
                                 str(r.get("tmux")), str(r.get("pane"))));
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
    static List<Item> fetch(Server server, int limit) {
        return fetch(server, limit, "");
    }

    /** The same, for one channel; "" (or "all") is every channel merged. */
    static List<Item> fetch(Server server, int limit, String channel) {
        Loopback.Reply r = Loopback.get(server, path(limit, channel));
        return r.ok() ? parse(r.body) : Collections.<Item>emptyList();
    }

    /** The query for a tab. "all" is the absence of a filter, not a value. */
    static String path(int limit, String channel) {
        String q = "/recent?limit=" + limit;
        if (channel != null && !channel.isEmpty() && !"all".equals(channel)) {
            q += "&channel=" + channel;
        }
        return q;
    }

    /** Why the list is empty, in words a person can act on. */
    static String emptyReason(Loopback.Reply r) {
        if (r == null) return "";
        if (!r.reached()) return r.failure;
        if (r.refused()) return Loopback.REFUSED;
        if (!r.ok()) return "agent-media: the listener said " + r.status;
        return "nothing played yet";
    }

    /**
     * Play a row again; returns the line to show.
     *
     * Each channel is repeated the way that channel repeats: music and book go
     * back through {@code /play}, which re-acquires and re-routes them, while a
     * spoken turn is replayed from the history by id. One list, three verbs —
     * the alternative was a screen that could only offer two of its own rows.
     */
    static String play(Server server, Item item) {
        if (!item.playable()) {
            return "agent-media: that one cannot be replayed";
        }
        if ("speech".equals(item.channel)) {
            String problem = Channels.control(server, "speech", "chapter",
                                              Long.toString(item.id));
            return problem.isEmpty() ? "replaying " + item.title() : problem;
        }
        Loopback.Reply r = Loopback.post(server, "/play", playBody(item));
        if (!r.reached()) return r.failure;
        if (r.refused()) return Loopback.REFUSED;
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
