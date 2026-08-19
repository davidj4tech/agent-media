package net.agentmedia.companion;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * The app answering as mpv, tested with what the server actually sends.
 *
 * The sequences below are lifted from {@code sinks/speech.py} rather than
 * invented: a single {@code play} is a loadfile plus the pause/mute reset, and
 * a reply is {@code play_playlist}'s batch — stop, playlist-clear, a loadfile
 * append per sentence, then pause/mute off and playlist-pos 0. If this app is
 * going to sit on that socket, the test that matters is that the real traffic
 * produces the real behaviour; anything else tests a protocol we made up.
 */
public class MpvServerTest {

    public static void main(String[] args) throws Exception {
        int failures = 0;
        FakePlayer p = new FakePlayer();
        MpvServer s = new MpvServer("127.0.0.1", 0, p, null);
        // Port 0: let the OS choose, so a stray mpv or a parallel test run
        // cannot make this fail for a reason that is not about the code.
        s.start();
        Thread.sleep(300);
        int port = s.boundPort();
        failures += check("bound", port > 0);

        try (Conn c = new Conn(port)) {
            // ---- what SinkSpeech.play sends -------------------------------
            failures += check("loadfile replace",
                    c.call("{\"command\":[\"loadfile\",\"/tmp/a.mp3\",\"replace\"],"
                            + "\"request_id\":1}").contains("\"error\":\"success\""));
            failures += check("the clip reached the player",
                    "/tmp/a.mp3".equals(p.path()) && p.loads.size() == 1);
            c.call("{\"command\":[\"set_property\",\"pause\",false],\"request_id\":2}");
            c.call("{\"command\":[\"set_property\",\"mute\",false],\"request_id\":3}");
            failures += check("pause and mute cleared", !p.paused() && !p.muted());

            // ---- what play_playlist sends, in one batch --------------------
            p.reset();
            c.send("{\"command\":[\"set_property\",\"gapless-audio\",\"yes\"],\"request_id\":10}");
            c.send("{\"command\":[\"stop\"],\"request_id\":11}");
            c.send("{\"command\":[\"playlist-clear\"],\"request_id\":12}");
            c.send("{\"command\":[\"loadfile\",\"/tmp/1.mp3\",\"append\"],\"request_id\":13}");
            c.send("{\"command\":[\"loadfile\",\"/tmp/2.mp3\",\"append\"],\"request_id\":14}");
            c.send("{\"command\":[\"set_property\",\"pause\",false],\"request_id\":15}");
            c.send("{\"command\":[\"set_property\",\"playlist-pos\",0],\"request_id\":16}");
            List<String> replies = c.read(7);
            failures += check("every command in the batch answered",
                    replies.size() == 7);
            failures += check("all of them succeeded",
                    replies.stream().allMatch(r -> r.contains("\"error\":\"success\"")));
            failures += check("gapless-audio is accepted, not refused",
                    replies.get(0).contains("success"));
            failures += check("both sentences queued", p.playlistCount() == 2);
            failures += check("and it started at the first", p.playlistPos() == 0);

            // ---- what the coordinator reads back ---------------------------
            failures += check("playlist-pos reads back",
                    c.call("{\"command\":[\"get_property\",\"playlist-pos\"],"
                            + "\"request_id\":20}").contains("\"data\":0"));
            failures += check("idle-active is false while playing",
                    c.call("{\"command\":[\"get_property\",\"idle-active\"],"
                            + "\"request_id\":21}").contains("\"data\":false"));
            String timePos = c.call("{\"command\":[\"get_property\",\"time-pos\"],"
                    + "\"request_id\":22}");
            failures += check("time-pos answers while playing",
                    timePos.contains("\"data\":"));

            // ---- the metadata the card is built from -----------------------
            c.call("{\"command\":[\"set_property\",\"force-media-title\","
                    + "\"Sam on the player spike\"],\"request_id\":30}");
            failures += check("media-title follows force-media-title",
                    c.call("{\"command\":[\"get_property\",\"media-title\"],"
                            + "\"request_id\":31}").contains("Sam on the player spike"));
            failures += check("the speaking flag is stored",
                    c.call("{\"command\":[\"set_property\","
                            + "\"user-data/agent-media/speaking\",true],"
                            + "\"request_id\":32}").contains("success")
                            && s.storedFlag("user-data/agent-media/speaking"));
            failures += check("and read back",
                    c.call("{\"command\":[\"get_property\","
                            + "\"user-data/agent-media/speaking\"],\"request_id\":33}")
                            .contains("\"data\":true"));

            // ---- an unknown property fails the way old mpv fails ------------
            failures += check("unknown property is 'property not found'",
                    c.call("{\"command\":[\"set_property\",\"sub-visibility\",false],"
                            + "\"request_id\":40}").contains("property not found"));
            failures += check("and the connection survives it",
                    c.call("{\"command\":[\"get_property\",\"pause\"],"
                            + "\"request_id\":41}").contains("success"));
            failures += check("so does a malformed line",
                    c.call("not json at all").contains("error")
                            && c.call("{\"command\":[\"get_property\",\"pause\"],"
                                    + "\"request_id\":42}").contains("success"));

            // ---- observing, which is how the reply is followed --------------
            String first = c.call("{\"command\":[\"observe_property\",7,"
                    + "\"playlist-pos\"],\"request_id\":50}");
            failures += check("observe answers success", first.contains("success"));
            String initial = c.readOne();
            failures += check("observing sends the current value at once",
                    initial.contains("property-change")
                            && initial.contains("\"id\":7"));
            p.advance();
            s.changed("playlist-pos");
            String change = c.readOne();
            failures += check("and an advance is volunteered",
                    change.contains("property-change")
                            && change.contains("\"data\":1"));

            // A follower is entitled to treat every event as news. The first
            // end-to-end reply sent playlist-pos twice per sentence, because
            // the advance and the next clip's start both announced it.
            s.changed("playlist-pos");
            s.changed("playlist-pos");
            // Nothing may have been sent for those two. The proof is that the
            // next line off the socket is the next real change, not a repeat.
            p.playlistPos(0);
            s.changed("playlist-pos");
            String afterRepeats = c.readOne();
            failures += check("an unchanged value is not repeated",
                    afterRepeats.contains("\"data\":0"));
        }
        s.stop();
        Thread.sleep(200);
        failures += check("stops", s.boundPort() == -1);

        System.out.println(failures == 0 ? "MpvServerTest ok" : failures + " failed");
        if (failures != 0) System.exit(1);
    }

    // ---- a player that only remembers what it was told ---------------------

    static final class FakePlayer implements MpvServer.Player {
        final List<String> loads = new ArrayList<String>();
        private final List<String> playlist = new ArrayList<String>();
        private int pos = -1;
        private boolean paused, muted;
        private double volume = 100, speed = 1.0;

        void reset() {
            loads.clear();
            playlist.clear();
            pos = -1;
        }

        void advance() {
            if (pos + 1 < playlist.size()) pos++;
        }

        @Override public void load(String uri, String mode) {
            loads.add(mode + " " + uri);
            if ("replace".equals(mode)) {
                playlist.clear();
                playlist.add(uri);
                pos = 0;
            } else {
                playlist.add(uri);
            }
        }
        @Override public void playlistClear() {
            String current = pos >= 0 && pos < playlist.size() ? playlist.get(pos) : null;
            playlist.clear();
            pos = -1;
            if (current != null) {
                playlist.add(current);
                pos = 0;
            }
        }
        @Override public void stop() {
            playlist.clear();
            pos = -1;
        }
        @Override public void playlistPos(int index) { pos = index; }
        @Override public void playlistNext() { advance(); }
        @Override public void playlistPrev() { if (pos > 0) pos--; }
        @Override public void pause(boolean p) { paused = p; }
        @Override public void mute(boolean m) { muted = m; }
        @Override public void volume(double v) { volume = v; }
        @Override public void speed(double s) { speed = s; }
        @Override public boolean paused() { return paused; }
        @Override public boolean muted() { return muted; }
        @Override public double volume() { return volume; }
        @Override public double speed() { return speed; }
        @Override public int playlistPos() { return pos; }
        @Override public int playlistCount() { return playlist.size(); }
        @Override public double timePos() { return pos < 0 ? -1 : 1.5; }
        @Override public double duration() { return pos < 0 ? -1 : 4.0; }
        @Override public String path() {
            return pos >= 0 && pos < playlist.size() ? playlist.get(pos) : null;
        }
        @Override public boolean idle() { return pos < 0; }
    }

    // ---- a client that talks the line protocol -----------------------------

    static final class Conn implements AutoCloseable {
        private final Socket socket;
        private final OutputStream out;
        private final BufferedReader in;

        Conn(int port) throws Exception {
            socket = new Socket("127.0.0.1", port);
            socket.setSoTimeout(3000);
            out = socket.getOutputStream();
            in = new BufferedReader(new InputStreamReader(
                    socket.getInputStream(), StandardCharsets.UTF_8));
        }

        void send(String line) throws Exception {
            out.write((line + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
        }

        String call(String line) throws Exception {
            send(line);
            return in.readLine();
        }

        String readOne() throws Exception {
            return in.readLine();
        }

        List<String> read(int n) throws Exception {
            List<String> lines = new ArrayList<String>();
            for (int i = 0; i < n; i++) lines.add(in.readLine());
            return lines;
        }

        @Override public void close() throws Exception {
            socket.close();
        }
    }

    private static int check(String what, boolean ok) {
        System.out.println((ok ? "  ok   " : "  FAIL ") + what);
        return ok ? 0 : 1;
    }
}
