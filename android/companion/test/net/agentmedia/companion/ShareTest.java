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
 * Host-side tests for the share pipe, against a fake listener on loopback.
 *
 * Every install on p8a is a sideload and a tap, so a bug caught here is a
 * round trip saved — and the one thing a share must never do is fail silently.
 */
public final class ShareTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) throws Exception {
        testBodyIsJson();
        testParsePrefersTheLine();
        testParseFallsBackToTheError();
        testParseSurvivesGarbage();
        testSendPostsAndReadsTheVerdict();
        testSendReportsAnErrorStatus();
        testNoListenerIsAMessageNotACrash();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testBodyIsJson() {
        String b = ShareRequest.body("A Talk https://youtu.be/x");
        check("body is a JSON object", b.startsWith("{") && b.endsWith("}"));
        check("body carries the text under \"text\"",
                b.contains("\"text\"") && b.contains("https://youtu.be/x"));
        // Shared text is arbitrary: a quote in a video title must not produce
        // a body the listener refuses to parse.
        check("quotes in shared text are escaped",
                ShareRequest.body("say \"hi\"").contains("\\\"hi\\\""));
    }

    private static void testParsePrefersTheLine() {
        ShareRequest.Result r = ShareRequest.parse(200,
                "{\"ok\":true,\"line\":\"A Talk → book (podcast): 90m long\"}");
        check("ok verdict is ok", r.ok);
        check("shows the verdict line", r.message.contains("book (podcast)"));
    }

    private static void testParseFallsBackToTheError() {
        ShareRequest.Result r = ShareRequest.parse(422,
                "{\"ok\":false,\"error\":\"no link in the shared text\"}");
        check("rejected share is not ok", !r.ok);
        check("shows the reason", r.message.contains("no link"));
    }

    private static void testParseSurvivesGarbage() {
        // A crash here would be a share sheet that force-closes, which is the
        // most alarming way possible to say "the listener is misbehaving".
        ShareRequest.Result r = ShareRequest.parse(500, "<html>nope</html>");
        check("garbage is not ok", !r.ok);
        check("garbage still says something", r.message.length() > 0);
        ShareRequest.Result empty = ShareRequest.parse(200, "");
        check("an empty body still says something", empty.message.length() > 0);
    }

    private static void testSendPostsAndReadsTheVerdict() throws Exception {
        FakeListener fake = new FakeListener(200,
                "{\"ok\":true,\"line\":\"Rain → music (ambient): a live stream\"}");
        try {
            ShareRequest.Result r = ShareRequest.send(fake.port(), "https://youtu.be/x");
            check("send reports ok", r.ok);
            check("send returns the line", r.message.contains("ambient"));
            String req = fake.request();
            check("posts to /share", req.startsWith("POST /share "));
            check("sends the shared text", req.contains("https://youtu.be/x"));
        } finally {
            fake.close();
        }
    }

    private static void testSendReportsAnErrorStatus() throws Exception {
        FakeListener fake = new FakeListener(422,
                "{\"ok\":false,\"error\":\"no link in the shared text\"}");
        try {
            ShareRequest.Result r = ShareRequest.send(fake.port(), "words");
            check("a 422 is not ok", !r.ok);
            check("a 422 surfaces its reason", r.message.contains("no link"));
        } finally {
            fake.close();
        }
    }

    private static void testNoListenerIsAMessageNotACrash() throws Exception {
        // The single most likely failure in the field: the app is installed,
        // the Termux service is not running.
        ServerSocket probe = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"));
        int dead = probe.getLocalPort();
        probe.close();
        ShareRequest.Result r = ShareRequest.send(dead, "https://youtu.be/x");
        check("a dead port is not ok", !r.ok);
        check("a dead port names the service", r.message.contains("media-share"));
    }

    // ---- a listener that answers once, and remembers what it was asked ----

    private static final class FakeListener implements AutoCloseable {
        private final ServerSocket server;
        private final Thread thread;
        private volatile String request = "";

        FakeListener(final int status, final String json) throws Exception {
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
                        // The test that needed it will fail on its assertion.
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

    private static void check(String what, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  ok   " + what);
        } else {
            failures.add(what);
            System.out.println("  FAIL " + what);
        }
    }
}
