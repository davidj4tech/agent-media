package net.agentmedia.companion;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * The one door through the sandbox wall.
 *
 * Everything this app cannot do itself — classify a link, read the play
 * history, start playback — is done by `media` inside com.termux, and reached
 * over loopback HTTP on {@link #PORT}. This is that client: two verbs, no
 * dependencies, and it never throws at the caller.
 *
 * Separate from its callers because there are two of them now (the share sheet
 * and the recent list) and a second hand-rolled HttpURLConnection would be a
 * second set of timeouts, a second charset assumption and a second way to fail.
 *
 * {@code android.*}-free, so {@code test/run.sh} covers it against a fake
 * listener — which matters more here than anywhere: on p8a every alternative
 * is a sideload and a squint at the phone screen.
 */
final class Loopback {

    static final String HOST = "127.0.0.1";

    /** Where media-share binds. Never anything but loopback. */
    static final int PORT = 8771;

    /**
     * Long enough for a yt-dlp metadata probe on a mobile connection, short
     * enough that a dead listener does not hold the UI. The listener answers
     * as soon as it has decided; the download it starts is not on this clock.
     */
    static final int TIMEOUT_MS = 25000;

    /** An HTTP status and a body, or a status of 0 and why not. */
    static final class Reply {
        final int status;
        final String body;
        /** Set when the request never reached the listener. */
        final String failure;

        Reply(int status, String body) {
            this.status = status;
            this.body = body;
            this.failure = null;
        }

        Reply(String failure) {
            this.status = 0;
            this.body = "";
            this.failure = failure;
        }

        boolean ok() { return status == 200; }

        boolean reached() { return failure == null; }
    }

    private Loopback() {}

    static Reply get(int port, String path) {
        return request(port, path, "GET", null);
    }

    static Reply post(int port, String path, String body) {
        return request(port, path, "POST", body == null ? "" : body);
    }

    private static Reply request(int port, String path, String method, String body) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(
                    "http://" + HOST + ":" + port + path).openConnection();
            c.setRequestMethod(method);
            c.setConnectTimeout(TIMEOUT_MS);
            c.setReadTimeout(TIMEOUT_MS);
            if (body != null) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                byte[] payload = body.getBytes(StandardCharsets.UTF_8);
                c.setFixedLengthStreamingMode(payload.length);
                OutputStream os = c.getOutputStream();
                os.write(payload);
                os.close();
            }
            int status = c.getResponseCode();
            return new Reply(status, read(status >= 400 ? c.getErrorStream()
                                                        : c.getInputStream()));
        } catch (IOException e) {
            // Nearly always one cause, so say the useful thing rather than the
            // accurate-but-opaque one: the Termux service is not running.
            return new Reply("agent-media: no listener on " + HOST + ":" + port
                    + " (is media-share running?)");
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String read(InputStream in) throws IOException {
        if (in == null) return "";
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        byte[] chunk = new byte[2048];
        int n;
        while ((n = in.read(chunk)) > 0) buf.write(chunk, 0, n);
        in.close();
        return new String(buf.toByteArray(), StandardCharsets.UTF_8);
    }
}
