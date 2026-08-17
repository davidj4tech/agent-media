package net.agentmedia.companion;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * The one door to the server.
 *
 * Everything this app cannot do itself — classify a link, read the play
 * history, start playback — is done by `media`, and reached over HTTP. This is
 * that client: two verbs, no dependencies, and it never throws at the caller.
 *
 * It was called Loopback because for a long time there was only one server it
 * could talk to: `media` inside com.termux on this same phone, behind
 * {@code 127.0.0.1}. The name is kept and the assumption is not — where the
 * server lives is now {@link Server}'s answer, and this class asks it. On the
 * default configuration nothing has moved.
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

    /** Kept for the log lines that name the old default. See {@link Server}. */
    static final String HOST = Server.LOOPBACK;

    /** Where media-share binds when nobody has said otherwise. */
    static final int PORT = Server.CONTROL_PORT;

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

        /**
         * The server answered, and said no.
         *
         * Worth telling apart from every other failure: it is the one that is
         * fixed on this phone, in one field, and it is the one a person will
         * otherwise read as "the server is down".
         */
        boolean refused() { return status == 401 || status == 403; }
    }

    /** What to show for a {@link Reply#refused()}. */
    static final String REFUSED =
            "agent-media: the server refused the token — check Settings";

    private Loopback() {}

    static Reply get(Server server, String path) {
        return request(server, path, "GET", null);
    }

    static Reply post(Server server, String path, String body) {
        return request(server, path, "POST", body == null ? "" : body);
    }

    private static Reply request(Server server, String path, String method,
                                 String body) {
        Server s = server == null ? Server.defaults() : server;
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(
                    "http://" + s.host + ":" + s.control + path).openConnection();
            c.setRequestMethod(method);
            c.setConnectTimeout(TIMEOUT_MS);
            c.setReadTimeout(TIMEOUT_MS);
            // Sent whenever there is one, on loopback too: a listener that
            // requires a token should be reachable from a phone configured
            // with one, and a listener that does not want one ignores it.
            if (!s.token.isEmpty()) {
                c.setRequestProperty(Server.TOKEN_HEADER, s.token);
            }
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
            // accurate-but-opaque one: the service is not running. Which
            // service, and where, is the part that stopped being obvious the
            // day the address became a setting.
            return new Reply("agent-media: no listener on " + s.authority()
                    + (s.local() ? " (is media-share running?)"
                                 : " (is media-share running there, and bound"
                                   + " off loopback?)"));
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
