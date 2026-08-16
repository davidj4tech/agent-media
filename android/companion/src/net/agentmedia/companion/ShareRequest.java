package net.agentmedia.companion;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
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

    /** Where the Termux-side listener binds. Loopback, always. */
    static final String HOST = "127.0.0.1";
    static final int DEFAULT_PORT = 8771;

    /**
     * Long enough for a yt-dlp metadata probe on a mobile connection, short
     * enough that a dead listener does not hold the share sheet open. The
     * listener answers as soon as it has classified — the download it starts
     * afterwards is not on this clock.
     */
    static final int TIMEOUT_MS = 25000;

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
    static Result send(int port, String text) {
        HttpURLConnection c = null;
        try {
            URL url = new URL("http://" + HOST + ":" + port + "/share");
            c = (HttpURLConnection) url.openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(TIMEOUT_MS);
            c.setReadTimeout(TIMEOUT_MS);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            byte[] payload = body(text).getBytes(StandardCharsets.UTF_8);
            c.setFixedLengthStreamingMode(payload.length);
            OutputStream os = c.getOutputStream();
            os.write(payload);
            os.close();
            int status = c.getResponseCode();
            return parse(status, read(status >= 400 ? c.getErrorStream()
                                                    : c.getInputStream()));
        } catch (IOException e) {
            // Nearly always the same cause, so say the useful thing rather
            // than the accurate-but-opaque one: the service is not running.
            return new Result(false, "agent-media: no listener on "
                    + HOST + ":" + port + " (is media-share running?)");
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String str(Object v) {
        String s = Json.asString(v);
        return s == null ? "" : s;
    }

    private static String read(InputStream in) throws IOException {
        if (in == null) return "";
        java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
        byte[] chunk = new byte[1024];
        int n;
        while ((n = in.read(chunk)) > 0) buf.write(chunk, 0, n);
        in.close();
        return new String(buf.toByteArray(), StandardCharsets.UTF_8);
    }
}
