package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Host-side tests for the parts of the app that are not Android: the JSON
 * codec, the mpv IPC client, and the state model. Run with test/run.sh.
 *
 * Plain main() and hand-rolled assertions on purpose — pulling in JUnit would
 * mean a Maven dependency, which is the thing this project is avoiding.
 */
public final class IpcTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) throws Exception {
        testJsonRoundTrip();
        testJsonParsing();
        testStateTransitions();
        testStateTitleFallback();
        testSubscribeAndSnapshot();
        testPropertyChange();
        testPropertyChangeByIdOnly();
        testTransportCommands();
        testRequestFailsOnDisconnect();
        testReconnect();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    // ---- Json ------------------------------------------------------------

    private static void testJsonRoundTrip() {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("command", java.util.Arrays.asList("set_property", "pause", Boolean.TRUE));
        m.put("request_id", 7);
        is("{\"command\":[\"set_property\",\"pause\",true],\"request_id\":7}",
           Json.write(m), "command serialises the way mpv expects");

        is("\"a\\\"b\\\\c\\nd\"", Json.write("a\"b\\c\nd"), "strings escape");
        is("1.5", Json.write(1.5), "fractional numbers keep their point");
        is("100", Json.write(100.0), "whole numbers lose theirs (mpv volume is an int)");
        is("null", Json.write(null), "null");
    }

    private static void testJsonParsing() {
        Map<String, Object> m = Json.parseObject(
                "{\"data\":{\"a\":[1,2.5,true,null,\"x\"]},\"error\":\"success\"}");
        is("success", m.get("error"), "error field");
        @SuppressWarnings("unchecked")
        Map<String, Object> data = (Map<String, Object>) m.get("data");
        List<?> a = (List<?>) data.get("a");
        is(5, a.size(), "array length");
        is(1.0, Json.asDouble(a.get(0), -1), "int element");
        is(2.5, Json.asDouble(a.get(1), -1), "float element");
        is(Boolean.TRUE, a.get(2), "bool element");
        is(null, a.get(3), "null element");
        is("x", a.get(4), "string element");

        // A title with a quote in it is the realistic hazard here.
        Map<String, Object> t = Json.parseObject(
                "{\"data\":\"He said \\\"hi\\\" \\u263a\"}");
        is("He said \"hi\" \u263a", t.get("data"), "escapes decode");

        is("{}", Json.write(Json.parseObject("[1,2]")), "a non-object parses to an empty map");

        boolean threw = false;
        try { Json.parse("{\"a\":}"); } catch (Json.ParseException e) { threw = true; }
        is(true, threw, "malformed input throws rather than returning junk");
    }

    // ---- MpvState --------------------------------------------------------

    private static void testStateTransitions() {
        MpvState s = new MpvState();
        is(false, s.loaded(), "starts unloaded");
        is(false, s.playing(), "starts not playing");

        s.connected = true;
        is(true, s.apply("idle-active", Boolean.FALSE), "idle-active change is a change");
        is(false, s.apply("idle-active", Boolean.FALSE), "same value is not");
        is(true, s.loaded(), "a file is open");
        is(true, s.playing(), "and running");

        s.apply("pause", Boolean.TRUE);
        is(true, s.loaded(), "paused still counts as loaded");
        is(false, s.playing(), "but not as playing");

        s.connected = false;
        is(false, s.loaded(), "a lost connection means we know nothing");

        s.connected = true;
        s.apply("duration", 212.5);
        is(212500L, s.durationMs(), "duration in ms");
        s.apply("duration", null);
        is(-1L, s.durationMs(), "unknown duration reports -1, not 0");

        is(false, s.apply(MpvIpc.POSITION_PROPERTY, 12.0),
           "position is not a state change (it moves constantly)");
        is(12000L, s.positionMs(), "but it is recorded");
    }

    private static void testStateTitleFallback() {
        MpvState s = new MpvState();
        is("agent-media", s.title(), "a title with nothing to go on");
        s.apply("path", "/data/.../music-offline/dQw4w9WgXcQ.opus");
        is("dQw4w9WgXcQ.opus", s.title(), "falls back to the filename");
        s.apply("media-title", "Never Gonna Give You Up");
        is("Never Gonna Give You Up", s.title(), "media-title wins");
        s.apply("media-title", "");
        is("dQw4w9WgXcQ.opus", s.title(), "an empty title is not a title");
    }

    // ---- MpvIpc ----------------------------------------------------------

    /** Collects everything the client reports, for assertions. */
    private static final class Recorder implements MpvIpc.Listener {
        final MpvState state = new MpvState();
        final List<String> events = new CopyOnWriteArrayList<String>();
        final List<String> logs = new CopyOnWriteArrayList<String>();
        volatile CountDownLatch connected = new CountDownLatch(1);
        volatile CountDownLatch disconnected = new CountDownLatch(1);

        @Override public void onProperty(String name, Object value) {
            state.apply(name, value);
            events.add("prop " + name + "=" + value);
        }
        @Override public void onEvent(String event, Map<String, Object> message) {
            events.add("event " + event);
        }
        @Override public void onConnected() { state.connected = true; connected.countDown(); }
        @Override public void onDisconnected(String why) {
            state.connected = false;
            disconnected.countDown();
        }
        @Override public void onLog(String line) { logs.add(line); }

        boolean awaitProperty(String prefix, long ms) throws InterruptedException {
            long deadline = System.currentTimeMillis() + ms;
            while (System.currentTimeMillis() < deadline) {
                for (String e : events) if (e.startsWith(prefix)) return true;
                Thread.sleep(10);
            }
            return false;
        }
    }

    private static void testSubscribeAndSnapshot() throws Exception {
        try (FakeMpv mpv = new FakeMpv()) {
            mpv.properties.put("idle-active", Boolean.FALSE);
            mpv.properties.put("pause", Boolean.FALSE);
            mpv.properties.put("media-title", "Chasing Cars");
            mpv.properties.put("duration", 267.0);

            Recorder r = new Recorder();
            MpvIpc ipc = new MpvIpc("127.0.0.1", mpv.port(), r);
            ipc.start();
            is(true, r.connected.await(3, TimeUnit.SECONDS), "connects");

            is(true, waitFor(() -> mpv.observers.size() == MpvIpc.OBSERVED.length, 3000),
               "observes every property in OBSERVED");
            is(false, new ArrayList<String>(mpv.observers.values())
                        .contains(MpvIpc.POSITION_PROPERTY),
               "does NOT observe time-pos — it would fire continuously");

            is(true, r.awaitProperty("prop media-title=Chasing Cars", 3000),
               "reads a snapshot at connect, not just future changes");
            is(true, waitFor(() -> r.state.playing(), 3000), "snapshot gives a usable state");
            is("Chasing Cars", r.state.title(), "title from the snapshot");

            // A property mpv has no value for must not blow up the connect path.
            is(true, r.state.connected, "still connected after an unavailable property");
            ipc.stop();
        }
    }

    private static void testPropertyChange() throws Exception {
        try (FakeMpv mpv = new FakeMpv()) {
            mpv.properties.put("idle-active", Boolean.FALSE);
            mpv.properties.put("pause", Boolean.FALSE);
            Recorder r = new Recorder();
            MpvIpc ipc = new MpvIpc("127.0.0.1", mpv.port(), r);
            ipc.start();
            r.connected.await(3, TimeUnit.SECONDS);
            waitFor(() -> mpv.observers.size() == MpvIpc.OBSERVED.length, 3000);

            mpv.publish("pause", Boolean.TRUE);
            is(true, waitFor(() -> r.state.paused, 3000), "a pause from elsewhere reaches us");
            mpv.publish("media-title", "Bloc Party — Banquet");
            is(true, waitFor(() -> "Bloc Party — Banquet".equals(r.state.title()), 3000),
               "non-ASCII titles survive the wire");

            mpv.sendEvent("end-file");
            is(true, waitFor(() -> r.events.contains("event end-file"), 3000),
               "plain events are delivered");
            ipc.stop();
        }
    }

    private static void testPropertyChangeByIdOnly() throws Exception {
        try (FakeMpv mpv = new FakeMpv()) {
            Recorder r = new Recorder();
            MpvIpc ipc = new MpvIpc("127.0.0.1", mpv.port(), r);
            ipc.start();
            r.connected.await(3, TimeUnit.SECONDS);
            waitFor(() -> mpv.observers.size() == MpvIpc.OBSERVED.length, 3000);

            mpv.publishIdOnly("pause", Boolean.TRUE);
            is(true, waitFor(() -> r.state.paused, 3000),
               "a change carrying only the observe id is resolved to its name");
            ipc.stop();
        }
    }

    private static void testTransportCommands() throws Exception {
        try (FakeMpv mpv = new FakeMpv()) {
            mpv.properties.put("volume", 93.0);
            Recorder r = new Recorder();
            MpvIpc ipc = new MpvIpc("127.0.0.1", mpv.port(), r);
            ipc.start();
            r.connected.await(3, TimeUnit.SECONDS);
            waitFor(() -> mpv.observers.size() == MpvIpc.OBSERVED.length, 3000);
            mpv.received.clear();

            ipc.setProperty("pause", Boolean.FALSE);
            is(java.util.Arrays.asList("set_property", "pause", Boolean.FALSE),
               nextRealCommand(mpv), "play  -> set_property pause false");

            ipc.command("playlist-next", "weak");
            is(java.util.Arrays.asList("playlist-next", "weak"),
               nextRealCommand(mpv), "next  -> playlist-next weak (matches SinkMusicLocal)");

            ipc.setProperty("time-pos", 42000 / 1000.0);
            is(java.util.Arrays.asList("set_property", "time-pos", 42.0),
               nextRealCommand(mpv), "seek  -> set_property time-pos in seconds");

            // The hazard this guards: MediaSession transport callbacks arrive
            // on the main looper, where any socket write is an instant
            // NetworkOnMainThreadException.
            Thread caller = Thread.currentThread();
            is(true, waitFor(() -> ipc.lastWriteThread != null
                        && !ipc.lastWriteThread.equals(caller.getName()), 3000),
               "commands are written off the calling thread");

            is(93.0, Json.asDouble(ipc.getProperty("volume")
                        .handle((v, e) -> e == null ? v : null)
                        .get(3, TimeUnit.SECONDS), -1),
               "get_property returns mpv's value");
            ipc.stop();
        }
    }

    private static void testRequestFailsOnDisconnect() throws Exception {
        try (FakeMpv mpv = new FakeMpv()) {
            Recorder r = new Recorder();
            MpvIpc ipc = new MpvIpc("127.0.0.1", mpv.port(), r);
            ipc.start();
            r.connected.await(3, TimeUnit.SECONDS);
            waitFor(() -> mpv.observers.size() == MpvIpc.OBSERVED.length, 3000);

            mpv.mute = true;
            java.util.concurrent.CompletableFuture<Object> f = ipc.getProperty("duration");
            mpv.dropClient();
            boolean failed = false;
            try {
                f.get(4, TimeUnit.SECONDS);
            } catch (java.util.concurrent.ExecutionException e) {
                failed = true;
            }
            is(true, failed,
               "an in-flight request fails instead of hanging when mpv goes away");
            ipc.stop();
        }
    }

    private static void testReconnect() throws Exception {
        try (FakeMpv mpv = new FakeMpv()) {
            mpv.properties.put("idle-active", Boolean.FALSE);
            Recorder r = new Recorder();
            MpvIpc ipc = new MpvIpc("127.0.0.1", mpv.port(), r);
            ipc.start();
            is(true, r.connected.await(3, TimeUnit.SECONDS), "connects");

            r.connected = new CountDownLatch(1);
            mpv.dropClient();
            is(true, r.disconnected.await(3, TimeUnit.SECONDS), "notices the drop");
            // Generous: a failed first attempt doubles the backoff, which tops
            // out at 15 s, and this test flaked once at a 10 s bound on a
            // loaded host.
            is(true, r.connected.await(25, TimeUnit.SECONDS),
               "reconnects on its own — mpv-music restarts under runit and the app must not care");
            is(true, waitFor(() -> mpv.observers.size() == MpvIpc.OBSERVED.length, 3000),
               "and re-subscribes");
            ipc.stop();
        }
    }

    // ---- helpers ---------------------------------------------------------

    /** The next command that is not part of the subscribe handshake. */
    private static List<?> nextRealCommand(FakeMpv mpv) throws InterruptedException {
        long deadline = System.currentTimeMillis() + 3000;
        while (System.currentTimeMillis() < deadline) {
            Map<String, Object> m = mpv.nextCommand(500);
            if (m == null) continue;
            Object c = m.get("command");
            if (!(c instanceof List)) continue;
            List<?> cmd = (List<?>) c;
            if (cmd.isEmpty()) continue;
            String verb = String.valueOf(cmd.get(0));
            // Skip the connect handshake: sends are asynchronous, so the
            // subscribe traffic can still be arriving after the queue is cleared.
            if ("observe_property".equals(verb) || "get_property".equals(verb)) continue;
            return cmd;
        }
        return Collections.emptyList();
    }

    private interface Cond { boolean ok(); }

    private static boolean waitFor(Cond c, long ms) {
        long deadline = System.currentTimeMillis() + ms;
        while (System.currentTimeMillis() < deadline) {
            if (c.ok()) return true;
            try { Thread.sleep(10); } catch (InterruptedException e) { return false; }
        }
        return c.ok();
    }

    private static void is(Object expected, Object actual, String what) {
        boolean ok = (expected == null) ? actual == null : expected.equals(actual);
        if (ok) {
            passed++;
            System.out.println("  ok    " + what);
        } else {
            failures.add(what + "\n          expected: " + expected + "\n          actual:   " + actual);
            System.out.println("  FAIL  " + what + "  (expected " + expected + ", got " + actual + ")");
        }
    }
}
