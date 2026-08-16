package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The three channels as the control screen sees them, and the verbs it sends.
 *
 * The phone already has a media card per channel, but a card carries transport
 * and nothing else: no seek by an amount, no speed, no volume, no chapter, no
 * mute, and no way to see how far into a two-hour set you are. That is what the
 * tmux popup is for, and this is the portable half of it.
 *
 * Rendering decisions live here rather than in the activity — the clock, the
 * progress fraction, whether a button should be enabled — so they can be tested
 * on the build host. Every field is nullable by design, because the listener
 * answers with whatever it could read: a channel whose backend is down should
 * cost its own panel, not the screen.
 *
 * {@code android.*}-free, so {@code test/run.sh} covers it.
 */
final class Channels {

    /** Channel order on screen. Speech first: it is the one that interrupts. */
    static final String[] ORDER = {"speech", "music", "book"};

    /** One channel's state. */
    static final class Channel {
        final String name;
        final boolean idle;
        final boolean playing;
        final boolean paused;
        final String title;
        final String chapter;
        final Long posMs;
        final Long durMs;
        final Double speed;
        final Integer volume;
        final boolean muted;
        final int mutedPanes;

        Channel(String name, boolean idle, boolean playing, boolean paused,
                String title, String chapter, Long posMs, Long durMs,
                Double speed, Integer volume, boolean muted, int mutedPanes) {
            this.name = name;
            this.idle = idle;
            this.playing = playing;
            this.paused = paused;
            this.title = title;
            this.chapter = chapter;
            this.posMs = posMs;
            this.durMs = durMs;
            this.speed = speed;
            this.volume = volume;
            this.muted = muted;
            this.mutedPanes = mutedPanes;
        }

        /** What to show as the headline. Never empty — a blank panel reads as broken. */
        String heading() {
            if (title != null && !title.isEmpty()) return title;
            return idle ? "nothing playing" : "(untitled)";
        }

        /** `12:04 / 58:31`, or just the position when the length is unknown
         *  (a live stream), or empty when there is nothing to time. */
        String clock() {
            if (posMs == null) return "";
            String pos = time(posMs.longValue());
            if (durMs == null || durMs.longValue() <= 0) return pos;
            return pos + " / " + time(durMs.longValue());
        }

        /** 0..1 for the progress bar, or -1 when it should not be drawn. */
        float progress() {
            if (posMs == null || durMs == null || durMs.longValue() <= 0) return -1f;
            float f = posMs.floatValue() / durMs.floatValue();
            return f < 0f ? 0f : (f > 1f ? 1f : f);
        }

        /** `1.25×`, or empty at normal speed — the popup shows it the same way. */
        String speedLabel() {
            if (speed == null) return "";
            if (Math.abs(speed.doubleValue() - 1.0) < 0.01) return "";
            return trim(speed.doubleValue()) + "×";
        }

        /** The line under the heading: chapter, speed, mutes — whatever applies. */
        String detail() {
            List<String> bits = new ArrayList<String>();
            if (chapter != null && !chapter.isEmpty()) bits.add(chapter);
            String sp = speedLabel();
            if (!sp.isEmpty()) bits.add(sp);
            if (volume != null) bits.add("vol " + volume);
            if (muted) bits.add("muted");
            if (mutedPanes > 0) bits.add(mutedPanes + " muted");
            StringBuilder sb = new StringBuilder();
            for (String b : bits) {
                if (sb.length() > 0) sb.append("  ·  ");
                sb.append(b);
            }
            return sb.toString();
        }

        /** Chapters are a music-channel thing, and only with a live mpv track. */
        boolean mayHaveChapters() {
            return "music".equals(name) && !idle;
        }
    }

    private Channels() {}

    /** `h:mm:ss` past an hour, `m:ss` under it. Same shape the popup uses. */
    static String time(long ms) {
        if (ms < 0) ms = 0;
        long total = ms / 1000;
        long h = total / 3600, m = (total % 3600) / 60, s = total % 60;
        if (h > 0) return h + ":" + two(m) + ":" + two(s);
        return m + ":" + two(s);
    }

    private static String two(long v) {
        return v < 10 ? "0" + v : Long.toString(v);
    }

    /** `1.25` not `1.25000000001`, and `1.5` not `1.50`. */
    static String trim(double v) {
        String s = String.format(java.util.Locale.US, "%.2f", v);
        while (s.endsWith("0")) s = s.substring(0, s.length() - 1);
        if (s.endsWith(".")) s = s.substring(0, s.length() - 1);
        return s;
    }

    /**
     * Parse a {@code /channels} reply. Missing channels come back idle rather
     * than absent, so the screen always has three panels to draw.
     */
    static Map<String, Channel> parse(String payload) {
        Map<String, Channel> out = new LinkedHashMap<String, Channel>();
        Map<?, ?> channels = null;
        try {
            Object got = Json.parseObject(payload).get("channels");
            if (got instanceof Map) channels = (Map<?, ?>) got;
        } catch (RuntimeException e) {
            channels = null;
        }
        for (String name : ORDER) {
            Object row = channels == null ? null : channels.get(name);
            out.put(name, row instanceof Map ? one(name, (Map<?, ?>) row)
                                             : blank(name));
        }
        return out;
    }

    private static Channel blank(String name) {
        return new Channel(name, true, false, false, null, null, null, null,
                           null, null, false, 0);
    }

    private static Channel one(String name, Map<?, ?> r) {
        return new Channel(name,
                Json.asBool(r.get("idle"), true),
                Json.asBool(r.get("playing"), false),
                Json.asBool(r.get("paused"), false),
                Json.asString(r.get("title")),
                Json.asString(r.get("chapter")),
                num(r.get("pos_ms")), num(r.get("dur_ms")),
                r.get("speed") instanceof Number
                        ? Double.valueOf(((Number) r.get("speed")).doubleValue()) : null,
                r.get("volume") instanceof Number
                        ? Integer.valueOf(((Number) r.get("volume")).intValue()) : null,
                Json.asBool(r.get("muted"), false),
                (int) Json.asDouble(r.get("muted_panes"), 0));
    }

    private static Long num(Object v) {
        return v instanceof Number ? Long.valueOf(((Number) v).longValue()) : null;
    }

    /** The body {@code /control} wants. */
    static String controlBody(String channel, String action, String arg) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("channel", channel);
        m.put("action", action);
        if (arg != null && !arg.isEmpty()) m.put("arg", arg);
        return Json.write(m);
    }

    /** Fetch the three channels. Empty map only if the reply was unusable. */
    static Map<String, Channel> fetch(int port) {
        Loopback.Reply r = Loopback.get(port, "/channels");
        return r.ok() ? parse(r.body) : parse("");
    }

    /**
     * Press a button. Returns "" when it worked, or a line to show when it did
     * not — a control that silently fails is the thing this screen exists to
     * avoid, since the listener is the only place that knows.
     */
    static String control(int port, String channel, String action, String arg) {
        Loopback.Reply r = Loopback.post(port, "/control",
                                         controlBody(channel, action, arg));
        if (!r.reached()) return r.failure;
        try {
            Map<String, Object> o = Json.parseObject(r.body);
            if (Json.asBool(o.get("ok"), false)) return "";
            String err = Json.asString(o.get("error"));
            if (err != null && !err.isEmpty()) return err;
            return channel + " " + action + " did not take";
        } catch (RuntimeException e) {
            return r.ok() ? "" : "agent-media: HTTP " + r.status;
        }
    }
}
