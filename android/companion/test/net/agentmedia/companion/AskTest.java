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
 * Host-side tests for the ask pipe.
 *
 * The property under test throughout is that a refusal stays a refusal. "That
 * conversation has closed" arrives 200 with ok:false, and it must not read as
 * a transport failure — only one of those is worth trying again, and a phone
 * that cannot tell them apart either nags a hub that is fine or gives up on
 * one that is asleep.
 */
public final class AskTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) throws Exception {
        testBodyIsJson();
        testBodyTagsWhereItCameFrom();
        testStatusReadsWhoWouldBeAsked();
        testStatusRefusalIsNotLive();
        testStatusSurvivesGarbage();
        testTitleNamesTheConversation();
        testTitleSaysWhyNotWhenNobodyIsListening();
        testParseAsked();
        testParseRefusalKeepsTheReason();
        testParseAnErrorBeatsAReason();
        testParseSurvivesGarbage();
        testStatusGetsTheChannel();
        testSendPostsTheQuestion();
        testAClosedConversationIsNotACrash();
        testNoListenerIsAMessageNotACrash();
        testARefusalSaysSo();

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
        String b = AskRequest.body("who wrote this?", "book");
        check("body is a JSON object", b.startsWith("{") && b.endsWith("}"));
        check("body carries the question", b.contains("who wrote this?"));
        check("body carries the channel", b.contains("\"channel\"") && b.contains("book"));
        // A question is free text typed on a phone. A quote in it must not
        // produce a body the listener refuses to parse.
        check("quotes are escaped",
                AskRequest.body("is it \"live\"?", "").contains("\\\"live\\\""));
        check("no channel sends no channel key",
                !AskRequest.body("why?", "").contains("channel"));
    }

    private static void testBodyTagsWhereItCameFrom() {
        // A submitted line is otherwise indistinguishable from David typing it
        // at the keyboard, which invites the session to answer as though he
        // were sitting there rather than holding a phone.
        check("the line is tagged as the phone's",
                AskRequest.body("why?", "").contains("the phone"));
    }

    private static void testStatusReadsWhoWouldBeAsked() {
        AskRequest.Status s = AskRequest.parseStatus(200,
                "{\"live\":true,\"label\":\"deploy\",\"reason\":\"deploy is listening\","
                + "\"last\":\"I moved the service\",\"reachable\":true}");
        check("a live conversation is live", s.live);
        check("it has a name", s.label.equals("deploy"));
        check("and the last thing it said", s.last.contains("moved the service"));
        check("and it is reachable", s.reachable);
    }

    private static void testStatusRefusalIsNotLive() {
        AskRequest.Status s = AskRequest.parseStatus(200,
                "{\"live\":false,\"reason\":\"deploy has closed\",\"reachable\":true}");
        check("a closed conversation is not live", !s.live);
        check("but the hub was reachable", s.reachable);
        check("and the reason survives", s.reason.contains("closed"));
    }

    private static void testStatusSurvivesGarbage() {
        AskRequest.Status s = AskRequest.parseStatus(500, "<html>nope</html>");
        check("garbage is not live", !s.live);
        check("garbage is not reachable", !s.reachable);
        check("garbage still says something", s.title().length() > 0);
    }

    private static void testTitleNamesTheConversation() {
        AskRequest.Status s = new AskRequest.Status(true, "deploy", "x", true, "");
        check("the title names who is being asked", s.title().equals("ask deploy"));
        AskRequest.Status anon = new AskRequest.Status(true, "", "x", true, "");
        check("an unnamed conversation still has a title",
                anon.title().equals("ask the conversation"));
    }

    private static void testTitleSaysWhyNotWhenNobodyIsListening() {
        // The whole point of asking status first: the question is never typed
        // into the void, and the sentence explaining why is the one the far
        // side wrote, not one invented here.
        AskRequest.Status s =
                new AskRequest.Status(false, "deploy", "deploy has closed", true, "");
        check("a dead conversation's title is the reason",
                s.title().equals("deploy has closed"));
        AskRequest.Status blank = new AskRequest.Status(false, "", "", true, "");
        check("and there is always a title", blank.title().length() > 0);
    }

    private static void testParseAsked() {
        AskRequest.Result r = AskRequest.parse(200,
                "{\"ok\":true,\"asked\":true,\"label\":\"deploy\"}");
        check("an accepted question is ok", r.ok);
        check("and says who took it", r.message.equals("asked deploy"));
    }

    private static void testParseRefusalKeepsTheReason() {
        AskRequest.Result r = AskRequest.parse(200,
                "{\"ok\":false,\"asked\":false,\"reason\":\"deploy has closed\"}");
        check("a refusal is not ok", !r.ok);
        check("a refusal shows its reason", r.message.equals("deploy has closed"));
    }

    private static void testParseAnErrorBeatsAReason() {
        AskRequest.Result r = AskRequest.parse(422,
                "{\"ok\":false,\"error\":\"nothing to ask\"}");
        check("an error is not ok", !r.ok);
        check("an error is what is shown", r.message.contains("nothing to ask"));
    }

    private static void testParseSurvivesGarbage() {
        AskRequest.Result r = AskRequest.parse(500, "not json at all");
        check("garbage is not ok", !r.ok);
        check("garbage still says something", r.message.length() > 0);
        check("an empty body still says something",
                AskRequest.parse(200, "").message.length() > 0);
    }

    private static void testStatusGetsTheChannel() throws Exception {
        FakeListener fake = new FakeListener(200, "{\"live\":true,\"label\":\"deploy\"}");
        try {
            AskRequest.Status s = AskRequest.status(Server.loopback(fake.port()), "book");
            check("status comes back live", s.live);
            String req = fake.request();
            check("asks /ask", req.startsWith("GET /ask"));
            check("names the channel", req.contains("channel=book"));
        } finally {
            fake.close();
        }
        FakeListener plain = new FakeListener(200, "{\"live\":false}");
        try {
            AskRequest.status(Server.loopback(plain.port()), "");
            check("no channel means speech", plain.request().contains("channel=speech"));
        } finally {
            plain.close();
        }
    }

    private static void testSendPostsTheQuestion() throws Exception {
        FakeListener fake = new FakeListener(200,
                "{\"ok\":true,\"asked\":true,\"label\":\"deploy\"}");
        try {
            AskRequest.Result r = AskRequest.send(Server.loopback(fake.port()),
                                                  "who wrote this?", "music");
            check("send reports ok", r.ok);
            check("send names the conversation", r.message.contains("deploy"));
            String req = fake.request();
            check("posts to /ask", req.startsWith("POST /ask "));
            check("carries the question", req.contains("who wrote this?"));
        } finally {
            fake.close();
        }
    }

    private static void testAClosedConversationIsNotACrash() throws Exception {
        FakeListener fake = new FakeListener(200,
                "{\"ok\":false,\"asked\":false,\"reason\":\"deploy has closed\"}");
        try {
            AskRequest.Result r = AskRequest.send(Server.loopback(fake.port()), "why?", "");
            check("a refusal is not ok", !r.ok);
            check("a refusal reads as an answer", r.message.equals("deploy has closed"));
        } finally {
            fake.close();
        }
    }

    private static void testNoListenerIsAMessageNotACrash() throws Exception {
        ServerSocket probe = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"));
        int dead = probe.getLocalPort();
        probe.close();
        AskRequest.Result r = AskRequest.send(Server.loopback(dead), "why?", "");
        check("a dead port is not ok", !r.ok);
        check("a dead port names the service", r.message.contains("media-share"));
        AskRequest.Status s = AskRequest.status(Server.loopback(dead), "speech");
        check("and status is not live either", !s.live);
        check("and says it could not be reached", !s.reachable);
    }

    private static void testARefusalSaysSo() throws Exception {
        FakeListener fake = new FakeListener(401, "{\"ok\":false,\"error\":\"bad token\"}");
        try {
            AskRequest.Result r = AskRequest.send(Server.loopback(fake.port()), "why?", "");
            check("a 401 is not ok", !r.ok);
            check("a 401 points at Settings", r.message.equals(Loopback.REFUSED));
        } finally {
            fake.close();
        }
    }

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
