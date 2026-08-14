package net.agentmedia.companion;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for the loopback readout.
 *
 * This is the class that exists so a future session does not have to ask David
 * to read his phone screen aloud, so it had better work before it ships.
 */
public final class StatusTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) throws Exception {
        testPathParsing();
        testServesStateAndLog();
        testUnknownPathIs404();
        testSurvivesABadRequest();
        testBindFailureIsNotFatal();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testPathParsing() {
        is("/log", StatusServer.pathOf("GET /log HTTP/1.1"), "plain path");
        is("/state", StatusServer.pathOf("GET /state?pretty=1 HTTP/1.1"), "query stripped");
        is("/", StatusServer.pathOf("GET / HTTP/1.0"), "root");
        is("/log", StatusServer.pathOf("GET /log"), "no version");
        is("", StatusServer.pathOf("garbage"), "unparseable is empty, not a crash");
        is("", StatusServer.pathOf(null), "null is empty");
    }

    private static void testServesStateAndLog() throws Exception {
        StatusServer s = server(0);
        try {
            s.start();
            int port = awaitPort(s);
            yes(port > 0, "binds a port");

            String state = get(port, "/state");
            yes(state.contains("200 OK"), "/state is 200");
            yes(state.contains("application/json"), "/state is json");
            yes(state.contains("\"focus_mode\":\"acting\""), "/state carries the body");

            String log = get(port, "/log");
            yes(log.contains("line one"), "/log carries the log");

            String both = get(port, "/");
            yes(both.contains("focus_mode") && both.contains("line one"),
                "/ carries both");

            // Content-Length must match the UTF-8 byte count, not the char
            // count, or a non-ASCII title truncates the response.
            yes(get(port, "/log").contains("héllo — ☺"), "non-ASCII survives");
        } finally {
            s.stop();
        }
    }

    private static void testUnknownPathIs404() throws Exception {
        StatusServer s = server(0);
        try {
            s.start();
            int port = awaitPort(s);
            String r = get(port, "/nope");
            yes(r.contains("404"), "unknown path is 404");
            yes(r.contains("try /state"), "and says what to try");
        } finally {
            s.stop();
        }
    }

    private static void testSurvivesABadRequest() throws Exception {
        StatusServer s = server(0);
        try {
            s.start();
            int port = awaitPort(s);

            // A client that connects and hangs up without saying anything.
            Socket rude = new Socket(InetAddress.getByName("127.0.0.1"), port);
            rude.close();

            // The server must still be answering afterwards.
            yes(get(port, "/state").contains("200 OK"), "still serving after a rude client");
        } finally {
            s.stop();
        }
    }

    private static void testBindFailureIsNotFatal() throws Exception {
        StatusServer first = server(0);
        first.start();
        int port = awaitPort(first);

        // A second server on the same port cannot bind. The app's real job is
        // the MediaSession; losing the readout must not cost us that.
        final List<String> logs = new ArrayList<String>();
        StatusServer second = new StatusServer(port, source(), logs::add);
        second.start();
        Thread.sleep(300);
        is(-1, second.boundPort(), "the loser reports no port");
        yes(anyContains(logs, "failed to bind"), "and says so rather than dying");

        second.stop();
        first.stop();
    }

    // ---- fixtures --------------------------------------------------------

    private static StatusServer.Source source() {
        return new StatusServer.Source() {
            @Override public String state() {
                return "{\"focus_mode\":\"acting\",\"focus_held\":true}";
            }
            @Override public String log() {
                return "line one\nhéllo — ☺\n";
            }
        };
    }

    private static StatusServer server(int port) {
        return new StatusServer(port, source(), line -> { });
    }

    /** Port 0 means the OS picks; give the accept loop a moment to bind. */
    private static int awaitPort(StatusServer s) throws Exception {
        for (int i = 0; i < 100; i++) {
            int p = s.boundPort();
            if (p > 0) return p;
            Thread.sleep(20);
        }
        return -1;
    }

    private static String get(int port, String path) throws Exception {
        Socket c = new Socket(InetAddress.getByName("127.0.0.1"), port);
        try {
            c.setSoTimeout(3000);
            OutputStream out = c.getOutputStream();
            out.write(("GET " + path + " HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    .getBytes(StandardCharsets.UTF_8));
            out.flush();

            BufferedReader r = new BufferedReader(
                    new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) sb.append(line).append('\n');
            return sb.toString();
        } finally {
            c.close();
        }
    }

    // ---- assertions ------------------------------------------------------

    private static boolean anyContains(List<String> lines, String needle) {
        for (String l : lines) if (l.contains(needle)) return true;
        return false;
    }

    private static void is(Object want, Object got, String what) {
        if (want == null ? got == null : want.equals(got)) {
            passed++;
        } else {
            failures.add(what + ": wanted " + want + ", got " + got);
        }
    }

    private static void yes(boolean got, String what) {
        if (got) passed++; else failures.add(what + ": wanted true");
    }
}
