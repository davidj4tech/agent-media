package net.agentmedia.companion;

import java.util.Map;

/**
 * Carries shared text across the sandbox boundary to Termux, and turns the
 * answer into one line for a toast.
 *
 * The app decides nothing about the share. It cannot: choosing a channel needs
 * yt-dlp metadata, and yt-dlp — like mpv, like the cache, like `media` itself —
 * lives inside com.termux's private UID, which no other app on the phone can
 * open. Same wall the mpv bridges exist to cross. So this class is a pipe, and
 * every judgement is on the far side of it in {@code agent_media_core.share},
 * where it is a pure function with tests.
 *
 * {@code android.*}-free on purpose, so {@code test/run.sh} covers it against
 * a fake listener rather than a sideload and a squint at the phone screen.
 */
final class ShareRequest {

    /** What to show the sharer: one line, and whether it went well. */
    static final class Result {
        final boolean ok;
        final String message;

        Result(boolean ok, String message) {
            this.ok = ok;
            this.message = message;
        }
    }

    private ShareRequest() {}

    /** The request body: JSON, so a channel override can be added later. */
    static String body(String text) {
        java.util.Map<String, Object> m = new java.util.LinkedHashMap<String, Object>();
        m.put("text", text == null ? "" : text);
        return Json.write(m);
    }

    /**
     * Turn the listener's JSON into the toast line.
     *
     * Every failure path ends here too, because a share that silently does
     * nothing is the worst outcome available: the sharer taps, sees nothing,
     * and has no idea whether to try again.
     */
    static Result parse(int status, String payload) {
        String line = "";
        boolean ok = false;
        try {
            Map<String, Object> o = Json.parseObject(payload);
            ok = Json.asBool(o.get("ok"), false);
            // Json.asString returns null for a missing key, not "".
            line = str(o.get("line"));
            if (line.isEmpty()) line = str(o.get("error"));
        } catch (RuntimeException e) {
            line = "";
        }
        if (line.isEmpty()) {
            line = ok ? "shared" : "agent-media: share failed (HTTP " + status + ")";
        }
        return new Result(ok && status == 200, line);
    }

    /** POST the shared text; never throws — the caller has a toast to show. */
    static Result send(Server server, String text) {
        Loopback.Reply r = Loopback.post(server, "/share", body(text));
        if (!r.reached()) return new Result(false, r.failure);
        if (r.refused()) return new Result(false, Loopback.REFUSED);
        return parse(r.status, r.body);
    }

    private static String str(Object v) {
        String s = Json.asString(v);
        return s == null ? "" : s;
    }

}
