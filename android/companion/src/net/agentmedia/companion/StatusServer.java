package net.agentmedia.companion;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

/**
 * A readout the *outside* can reach: state and the event log over loopback HTTP.
 *
 * Until this existed the app's own screen was the only way to see what it was
 * doing. p8a has no adb; logcat from Termux shows only Termux's own uid, and
 * {@code dumpsys media_session} is refused to a non-shell uid — so diagnosing a
 * misbehaving session meant asking David to read his phone aloud. That is a bad
 * property for something meant to run unattended.
 *
 * Bound to 127.0.0.1 only, and deliberately: the tailnet listeners on this
 * phone are separate services with their own bind addresses, and this one must
 * never join them. Everything that needs it (agent-media on red5) already has a
 * shell on the phone.
 *
 * Hand-rolled because Android ships no {@code com.sun.net.httpserver}, and
 * android.*-free so test/run.sh can exercise it on the build host.
 *
 * <pre>
 *   GET /state  -> JSON, one object
 *   GET /log    -> text/plain, newest line first
 *   GET /       -> both, for a human with curl
 * </pre>
 */
final class StatusServer {

    /** Where the state and log text come from. Implemented by the service. */
    interface Source {
        /** A JSON object describing what the app currently thinks is true. */
        String state();
        /** The event log, newest first. */
        String log();

        /**
         * Any crash this app has recorded about itself. A default because it is
         * the one readout that is useless while everything works, and every
         * other implementor of this interface is a test.
         */
        default String crash() { return "(not recorded)"; }

        /**
         * Is anything recording right now — one line, first field 1 or 0.
         *
         * Deliberately not part of /state: call_guard polls this several times
         * a second to decide whether to pause Sam, and making barge-in latency
         * pay for the whole JSON snapshot (three mpv mirrors, two histories)
         * would be a poor trade. The rest of the line is for a human reading it
         * over ssh and is not parsed.
         */
        default String mic() { return "0 (no probe)"; }

        /**
         * Read (and optionally set) what we do about focus during a voice
         * session. The one writable knob on this server, and it is here rather
         * than behind a button because the alternative is a sideload per
         * experiment. Loopback only, like everything else on this port.
         */
        default String live(String set) { return "yield\n"; }
    }

    static final int DEFAULT_PORT = 8770;

    private final int port;
    private final Source source;
    private final Listener listener;

    private volatile ServerSocket server;
    private volatile boolean running = false;
    private Thread thread;

    /** So failures reach the on-screen log rather than vanishing. */
    interface Listener {
        void onLog(String line);
    }

    StatusServer(int port, Source source, Listener listener) {
        this.port = port;
        this.source = source;
        this.listener = listener;
    }

    /** The port actually bound, or -1 before start / after a bind failure. */
    int boundPort() {
        ServerSocket s = server;
        return (s == null || s.isClosed()) ? -1 : s.getLocalPort();
    }

    synchronized void start() {
        if (running) return;
        running = true;
        thread = new Thread(this::loop, "status-server");
        thread.setDaemon(true);
        thread.start();
    }

    synchronized void stop() {
        running = false;
        ServerSocket s = server;
        server = null;
        if (s != null) {
            try { s.close(); } catch (IOException ignored) { }
        }
        if (thread != null) {
            thread.interrupt();
            thread = null;
        }
    }

    private void loop() {
        try {
            ServerSocket s = new ServerSocket();
            s.setReuseAddress(true);
            s.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 8);
            server = s;
            listener.onLog("status server on 127.0.0.1:" + s.getLocalPort());
        } catch (IOException e) {
            // Not fatal: the app's real job is the session, and a port already
            // taken must not cost us that.
            listener.onLog("status server failed to bind " + port + ": " + e.getMessage());
            running = false;
            return;
        }

        while (running) {
            Socket c = null;
            try {
                c = server.accept();
                c.setSoTimeout(3000);
                serve(c);
            } catch (IOException e) {
                if (running) listener.onLog("status server: " + e.getMessage());
            } finally {
                if (c != null) {
                    try { c.close(); } catch (IOException ignored) { }
                }
            }
        }
    }

    private void serve(Socket c) throws IOException {
        BufferedReader in = new BufferedReader(
                new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8));
        String request = in.readLine();
        if (request == null) return;
        // Drain the headers so the client sees a clean response rather than a
        // reset while it is still writing.
        String line;
        while ((line = in.readLine()) != null && !line.isEmpty()) { /* discard */ }

        String path = pathOf(request);
        if ("/state".equals(path)) {
            respond(c, 200, "application/json", source.state() + "\n");
        } else if ("/log".equals(path)) {
            respond(c, 200, "text/plain; charset=utf-8", source.log());
        } else if ("/live".equals(path)) {
            respond(c, 200, "text/plain; charset=utf-8",
                    source.live(paramOf(request, "set")));
        } else if ("/mic".equals(path)) {
            respond(c, 200, "text/plain; charset=utf-8", source.mic() + "\n");
        } else if ("/crash".equals(path)) {
            respond(c, 200, "text/plain; charset=utf-8", source.crash());
        } else if ("/".equals(path)) {
            respond(c, 200, "text/plain; charset=utf-8",
                    source.state() + "\n\n" + source.log());
        } else {
            respond(c, 404, "text/plain; charset=utf-8",
                    "no such path: " + path + "\ntry /state, /mic, /live, /log, /crash or /\n");
        }
    }

    /** "GET /live?set=duck HTTP/1.1", "set" -> "duck". Absent -> "". */
    static String paramOf(String requestLine, String name) {
        if (requestLine == null) return "";
        int q = requestLine.indexOf('?');
        if (q < 0) return "";
        int end = requestLine.indexOf(' ', q);
        String query = (end < 0) ? requestLine.substring(q + 1)
                                 : requestLine.substring(q + 1, end);
        for (String pair : query.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0 && pair.substring(0, eq).equals(name)) {
                return pair.substring(eq + 1);
            }
        }
        return "";
    }

    /** "GET /log?x=1 HTTP/1.1" -> "/log". Anything unparseable becomes "". */
    static String pathOf(String requestLine) {
        if (requestLine == null) return "";
        int sp = requestLine.indexOf(' ');
        if (sp < 0) return "";
        int end = requestLine.indexOf(' ', sp + 1);
        String target = (end < 0) ? requestLine.substring(sp + 1)
                                  : requestLine.substring(sp + 1, end);
        int q = target.indexOf('?');
        if (q >= 0) target = target.substring(0, q);
        return target;
    }

    private static void respond(Socket c, int code, String type, String body)
            throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        StringBuilder head = new StringBuilder();
        head.append("HTTP/1.1 ").append(code).append(code == 200 ? " OK" : " Not Found").append("\r\n");
        head.append("Content-Type: ").append(type).append("\r\n");
        head.append("Content-Length: ").append(bytes.length).append("\r\n");
        head.append("Connection: close\r\n\r\n");

        OutputStream out = c.getOutputStream();
        out.write(head.toString().getBytes(StandardCharsets.UTF_8));
        out.write(bytes);
        out.flush();
    }
}
