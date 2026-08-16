package net.agentmedia.companion;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for the in-app "recently played" list.
 *
 * The list is the first screen in this app that shows agent-media's state
 * rather than the phone's own players, and the thing it must never do is take
 * a tap and do nothing. So the failures are tested as carefully as the success:
 * no listener, a bad reply, a row that cannot be replayed.
 */
public final class RecentTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    private static final String ROWS =
            "{\"ok\":true,\"rows\":["
            + "{\"uri\":\"mpv:https://y/1\",\"channel\":\"book\","
            + "\"content_type\":\"podcast\",\"label\":\"Episode 12\",\"ago\":\"3h\"},"
            + "{\"uri\":\"mpv:https://y/2\",\"channel\":\"music\","
            + "\"content_type\":\"dj-set\",\"label\":\"A Long Set\",\"ago\":\"2d\"}"
            + "]}";

    public static void main(String[] args) throws Exception {
        testParsesRows();
        testDisplayStrings();
        testParseSurvivesRubbish();
        testSpeechRowsAreNotPlayable();
        testPlayBodyCarriesTheChannel();
        testFetchOverTheWire();
        testPlayOverTheWire();
        testNoListenerSaysSo();
        testARejectedPlaySurfacesTheReason();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testParsesRows() {
        List<RecentList.Item> items = RecentList.parse(ROWS);
        check("both rows parse", items.size() == 2);
        check("newest first is preserved", items.get(0).label.equals("Episode 12"));
        check("the uri comes through", items.get(1).uri.equals("mpv:https://y/2"));
        check("and the content type", items.get(1).contentType.equals("dj-set"));
    }

    private static void testDisplayStrings() {
        RecentList.Item book = RecentList.parse(ROWS).get(0);
        check("title is the label", book.title().equals("Episode 12"));
        check("subtitle says when and where",
                book.subtitle().equals("3h ago · book (podcast)"));
        // A music row whose type is plain music should not say "music (music)".
        RecentList.Item plain = new RecentList.Item(
                "A Song", "music", "music", "u", "5m");
        check("no redundant type", plain.subtitle().equals("5m ago · music"));
        // An untitled row still has to show something.
        RecentList.Item bare = new RecentList.Item("", "music", "", "local:x", "");
        check("a row with no label falls back to the uri",
                bare.title().equals("local:x"));
        check("and an empty subtitle is empty, not \"null\"",
                bare.subtitle().equals("music"));
    }

    private static void testParseSurvivesRubbish() {
        // The activity was opened to read a list; it must not crash on one.
        check("html is an empty list", RecentList.parse("<html>no</html>").isEmpty());
        check("empty is an empty list", RecentList.parse("").isEmpty());
        check("rows of the wrong type are skipped",
                RecentList.parse("{\"rows\":[1,2,\"three\"]}").isEmpty());
        check("a missing rows key is an empty list",
                RecentList.parse("{\"ok\":true}").isEmpty());
    }

    private static void testSpeechRowsAreNotPlayable() {
        // Speech clips are in the same history and cannot be re-played: there
        // is no uri a channel would accept.
        RecentList.Item clip = new RecentList.Item(
                "something Sam said", "speech", "", "clip-17", "1h");
        check("a speech row is not playable", !clip.playable());
        check("and says so rather than failing silently",
                RecentList.play(1, clip).contains("cannot be replayed"));
        RecentList.Item empty = new RecentList.Item("x", "music", "", "", "1h");
        check("a row with no uri is not playable", !empty.playable());
    }

    private static void testPlayBodyCarriesTheChannel() {
        String body = RecentList.playBody(RecentList.parse(ROWS).get(0));
        check("body has the uri", body.contains("mpv:https://y/1"));
        check("body has the channel", body.contains("\"channel\""), body.contains("book"));
        check("body has the content type", body.contains("podcast"));
    }

    private static void testFetchOverTheWire() throws Exception {
        Fake fake = new Fake(200, ROWS);
        try {
            List<RecentList.Item> items = RecentList.fetch(fake.port(), 25);
            check("fetch returns the rows", items.size() == 2);
            check("fetch asks for /recent with the limit",
                    fake.request().startsWith("GET /recent?limit=25 "));
        } finally {
            fake.close();
        }
    }

    private static void testPlayOverTheWire() throws Exception {
        Fake fake = new Fake(200,
                "{\"ok\":true,\"line\":\"Episode 12 → book (podcast): replayed from history\"}");
        try {
            String line = RecentList.play(fake.port(), RecentList.parse(ROWS).get(0));
            check("play returns the listener's line", line.contains("replayed from history"));
            String req = fake.request();
            check("play posts to /play", req.startsWith("POST /play "));
            check("and sends the row", req.contains("mpv:https://y/1"));
        } finally {
            fake.close();
        }
    }

    private static void testNoListenerSaysSo() throws Exception {
        ServerSocket probe = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"));
        int dead = probe.getLocalPort();
        probe.close();
        check("a dead port gives an empty list",
                RecentList.fetch(dead, 25).isEmpty());
        Loopback.Reply r = Loopback.get(dead, "/recent");
        check("and the reason names the service",
                RecentList.emptyReason(r).contains("media-share"));
        check("playing against a dead port says the same",
                RecentList.play(dead, RecentList.parse(ROWS).get(0))
                        .contains("media-share"));
    }

    private static void testARejectedPlaySurfacesTheReason() throws Exception {
        Fake fake = new Fake(422, "{\"ok\":false,\"error\":\"no such channel: speech\"}");
        try {
            String line = RecentList.play(fake.port(), RecentList.parse(ROWS).get(0));
            check("a rejection is shown, not swallowed",
                    line.contains("no such channel"));
        } finally {
            fake.close();
        }
    }

    /** A listener that answers once and remembers what it was asked. */
    private static final class Fake implements AutoCloseable {
        private final ServerSocket server;
        private final Thread thread;
        private volatile String request = "";

        Fake(final int status, final String json) throws Exception {
            server = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"));
            thread = new Thread(new Runnable() {
                @Override public void run() {
                    try (Socket s = server.accept()) {
                        BufferedReader in = new BufferedReader(
                                new InputStreamReader(s.getInputStream(),
                                        StandardCharsets.UTF_8));
                        StringBuilder head = new StringBuilder();
                        String line;
                        int length = 0;
                        while ((line = in.readLine()) != null && !line.isEmpty()) {
                            head.append(line).append('\n');
                            if (line.toLowerCase().startsWith("content-length:")) {
                                length = Integer.parseInt(line.split(":")[1].trim());
                            }
                        }
                        char[] body = new char[length];
                        int read = 0;
                        while (read < length) {
                            int n = in.read(body, read, length - read);
                            if (n < 0) break;
                            read += n;
                        }
                        request = head.toString() + new String(body, 0, Math.max(read, 0));
                        byte[] payload = json.getBytes(StandardCharsets.UTF_8);
                        OutputStream out = s.getOutputStream();
                        out.write(("HTTP/1.1 " + status + " x\r\n"
                                + "Content-Type: application/json\r\n"
                                + "Content-Length: " + payload.length + "\r\n\r\n")
                                .getBytes(StandardCharsets.UTF_8));
                        out.write(payload);
                        out.flush();
                    } catch (Exception e) {
                        // The assertion in the test that needed it will fail.
                    }
                }
            });
            thread.setDaemon(true);
            thread.start();
        }

        int port() { return server.getLocalPort(); }

        String request() throws Exception {
            thread.join(3000);
            return request;
        }

        @Override public void close() throws Exception { server.close(); }
    }

    private static void check(String what, boolean... oks) {
        boolean ok = true;
        for (boolean b : oks) ok = ok && b;
        if (ok) {
            passed++;
            System.out.println("  ok   " + what);
        } else {
            failures.add(what);
            System.out.println("  FAIL " + what);
        }
    }
}
