package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * The places this channel can be jumped to.
 *
 * On music and book those are the loaded track's chapters: a fetched DJ set or
 * album upload carries them, an audiobook has them by definition, and they are
 * how you navigate two hours of audio without scrubbing — the popup gives them
 * their own key for that reason. An MPD stream has none, so an empty list is a
 * normal answer rather than a failure.
 *
 * On speech they are the clips already spoken, which is that channel's answer
 * to the same question. The shape is the listener's either way; the only thing
 * the app has to know is that a row is chosen by its {@link Chapter#ref}.
 *
 * {@code android.*}-free, so {@code test/run.sh} covers it.
 */
final class Chapters {

    static final class Chapter {
        final int number;
        final String title;
        final Long startMs;
        final boolean current;
        /**
         * What to send to pick this row, when it is not the row's number.
         *
         * A chapter is its number and always will be. A speech clip is a
         * history id, because the clip list is newest-first: a turn landing
         * while the dialog is open renumbers every row under the finger.
         */
        private final String ref;

        Chapter(int number, String title, Long startMs, boolean current) {
            this(number, title, startMs, current, "");
        }

        Chapter(int number, String title, Long startMs, boolean current,
                String ref) {
            this.number = number;
            this.title = title;
            this.startMs = startMs;
            this.current = current;
            this.ref = ref == null ? "" : ref;
        }

        /** The argument that picks this row: its ref, else its number. */
        String ref() {
            return ref.isEmpty() ? Integer.toString(number) : ref;
        }

        /** `▸ 3  Second Movement   11:47` — the marker shows where you are. */
        String label() {
            StringBuilder sb = new StringBuilder();
            sb.append(current ? "▸ " : "  ").append(number).append("  ");
            sb.append(title == null || title.isEmpty()
                      ? "Chapter " + number : title);
            if (startMs != null) {
                sb.append("   ").append(Channels.time(startMs.longValue()));
            }
            return sb.toString();
        }
    }

    private Chapters() {}

    /** Never throws: a chapter list that cannot be read is simply not offered. */
    static List<Chapter> parse(String payload) {
        List<Chapter> out = new ArrayList<Chapter>();
        try {
            Object rows = Json.parseObject(payload).get("rows");
            if (!(rows instanceof List)) return out;
            for (Object row : (List<?>) rows) {
                if (!(row instanceof Map)) continue;
                Map<?, ?> r = (Map<?, ?>) row;
                int n = (int) Json.asDouble(r.get("number"), out.size() + 1);
                Object start = r.get("start_ms");
                out.add(new Chapter(n, Json.asString(r.get("title")),
                        start instanceof Number
                                ? Long.valueOf(((Number) start).longValue()) : null,
                        Json.asBool(r.get("current"), false),
                        Json.asString(r.get("ref"))));
            }
        } catch (RuntimeException e) {
            return Collections.emptyList();
        }
        return out;
    }
}
