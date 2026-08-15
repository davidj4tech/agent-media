package net.agentmedia.companion;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * An mpv JSON IPC client speaking over loopback TCP.
 *
 * Why TCP and not the unix socket: mpv's socket lives inside com.termux's
 * private UID sandbox, which this app cannot open. A socat listener on
 * 127.0.0.1 is the only route in. See
 * docs/proposals/2026-08-13-android-companion-app.md.
 *
 * Contains no android.* imports on purpose — see {@link Json}. Everything here
 * is exercised by test/run.sh against a fake mpv on the build host.
 *
 * Threading: one reader thread owns the socket lifecycle and reconnects with
 * backoff forever; callbacks fire on that thread and callers marshal to their
 * own looper. Writes go to a second, single-threaded executor — MediaSession
 * transport callbacks arrive on the main looper, and touching a socket there
 * is an instant NetworkOnMainThreadException. Sending is therefore always
 * asynchronous, and always in submission order.
 */
final class MpvIpc {

    /** What the music connection keeps a live subscription to. */
    static final String[] OBSERVED = {
        "idle-active", "pause", "media-title", "duration", "path", "speed", "volume",
    };

    /**
     * The coordinator on red5 sets this on the speech mpv for the length of a
     * response — `agent_media_core.sinks.speech.SPEAKING_PROPERTY`. It is the
     * only reliable answer to "is this focus loss ours"; everything else here
     * is a fallback for a coordinator too old to set it.
     */
    static final String SPEAKING_PROPERTY = "user-data/agent-media/speaking";

    /**
     * How much interruption this reply is worth —
     * `agent_media_core.sinks.speech.PRIORITY_PROPERTY`. One of low / normal /
     * high / urgent, written with the speaking flag rather than at play time,
     * because the decision it feeds has to be made before the first word.
     */
    static final String PRIORITY_PROPERTY = "user-data/agent-media/priority";

    /**
     * What the speech connection subscribes to — enough to answer "is a clip
     * running" and "was one just staged". {@code path} is in the set because
     * the focus loss for a clip arrives when mpv *opens* the file, which on p8a
     * ran 11 s ahead of the first audio (20:16:29 vs 20:16:40, 2026-08-14):
     * asking whether speech is playing at loss time answers no for a loss that
     * is entirely ours. A new path is the staging signal.
     *
     * {@code media-title} joined them on 2026-08-15. It was excluded because a
     * rendered clip's title is its filename — {@code
     * remote-20260814T190922-18480.mp3} — but the coordinator now sets
     * {@code force-media-title} to the reply's own name, the same string the
     * popup shows (sinks/speech.py, set_media_title). When it does not, the
     * card falls back to its label rather than to the filename.
     */
    static final String[] OBSERVED_SPEECH = {
        "idle-active", "pause", "path", "media-title", SPEAKING_PROPERTY,
        PRIORITY_PROPERTY,
    };

    /**
     * time-pos is deliberately NOT observed: mpv fires it continuously, and a
     * PlaybackState carries a position plus a speed that the system
     * extrapolates from. Poll it at state changes instead — see
     * CompanionService.
     */
    static final String POSITION_PROPERTY = "time-pos";

    interface Listener {
        /** An observed property changed (or was read at connect time). */
        void onProperty(String name, Object value);
        /** An mpv event that is not a property change, e.g. "end-file". */
        void onEvent(String event, Map<String, Object> message);
        void onConnected();
        /** Called once per lost/failed connection, with a short reason. */
        void onDisconnected(String why);
        /** Human-readable trace for the on-screen log; adb is unavailable here. */
        void onLog(String line);
    }

    private final String host;
    private final int port;
    private final Listener listener;
    private final String[] observed;

    private final AtomicInteger nextRequestId = new AtomicInteger(1);
    private final Map<Integer, CompletableFuture<Object>> pending =
            new ConcurrentHashMap<Integer, CompletableFuture<Object>>();
    /** observe_property id -> property name. */
    private final Map<Integer, String> observeIds = new ConcurrentHashMap<Integer, String>();

    /** All socket writes happen here, never on the caller's thread. */
    private final java.util.concurrent.ExecutorService sender =
            java.util.concurrent.Executors.newSingleThreadExecutor(r -> {
                Thread t = new Thread(r, "mpv-ipc-send");
                t.setDaemon(true);
                return t;
            });
    private volatile Socket socket;
    private volatile OutputStream out;
    private volatile boolean running = false;
    private volatile boolean connected = false;
    private Thread thread;
    /** Diagnostic: which thread last wrote to the socket. See writeLine(). */
    volatile String lastWriteThread = null;

    /** Connect/read timeouts and the reconnect backoff, in milliseconds. */
    private static final int CONNECT_TIMEOUT_MS = 2000;
    private static final int READ_TIMEOUT_MS = 0;         // block; mpv is idle for long stretches
    private static final int BACKOFF_MIN_MS = 500;
    private static final int BACKOFF_MAX_MS = 15000;
    private static final long REQUEST_TIMEOUT_MS = 3000;

    MpvIpc(String host, int port, Listener listener) {
        this(host, port, listener, OBSERVED);
    }

    MpvIpc(String host, int port, Listener listener, String[] observed) {
        this.host = host;
        this.port = port;
        this.listener = listener;
        this.observed = observed;
    }

    boolean isConnected() {
        return connected;
    }

    /** Which bridge this one talks to; for the log lines that name it. */
    int port() {
        return port;
    }

    synchronized void start() {
        if (running) return;
        running = true;
        thread = new Thread(this::loop, "mpv-ipc");
        thread.setDaemon(true);
        thread.start();
    }

    synchronized void stop() {
        running = false;
        sender.shutdownNow();
        closeSocket("stopped");
        if (thread != null) {
            thread.interrupt();
            thread = null;
        }
    }

    // ---- sending ---------------------------------------------------------

    /**
     * Send a command and ignore the reply. Used for every transport action:
     * mpv answers, but there is nothing useful to do with success, and the
     * property observers report the consequence anyway.
     */
    void command(Object... args) {
        final int id = nextRequestId.getAndIncrement();
        final List<Object> list = Arrays.asList(args);
        submit(() -> writeCommand(id, list));
    }

    void setProperty(String name, Object value) {
        command("set_property", name, value);
    }

    /**
     * Send a command and complete when mpv replies. Completes exceptionally on
     * error or timeout, so callers must attach a handler; nothing here throws
     * into the caller's thread.
     */
    CompletableFuture<Object> request(Object... args) {
        final int id = nextRequestId.getAndIncrement();
        final List<Object> list = Arrays.asList(args);
        final CompletableFuture<Object> f = new CompletableFuture<Object>();
        pending.put(id, f);
        f.orTimeout(REQUEST_TIMEOUT_MS, java.util.concurrent.TimeUnit.MILLISECONDS)
         .whenComplete((v, e) -> pending.remove(id));
        submit(() -> {
            if (!writeCommand(id, list)) {
                pending.remove(id);
                f.completeExceptionally(new IOException("not connected"));
            }
        });
        return f;
    }

    private void submit(Runnable task) {
        try {
            sender.execute(task);
        } catch (java.util.concurrent.RejectedExecutionException e) {
            // stop() has been called; there is nothing left to send to.
        }
    }

    CompletableFuture<Object> getProperty(String name) {
        return request("get_property", name);
    }

    private boolean writeCommand(int id, List<Object> args) {
        Map<String, Object> msg = new LinkedHashMap<String, Object>();
        msg.put("command", args);
        msg.put("request_id", id);
        return writeLine(Json.write(msg));
    }

    private boolean writeLine(String line) {
        // Only the sender thread reaches here, so no lock is needed. The name
        // is recorded so a test can prove writes never happen on the caller's
        // thread — on Android that mistake is fatal, not slow.
        lastWriteThread = Thread.currentThread().getName();
        OutputStream o = out;
        if (o == null) return false;
        try {
            o.write((line + "\n").getBytes(StandardCharsets.UTF_8));
            o.flush();
            return true;
        } catch (IOException e) {
            closeSocket("write failed: " + e.getMessage());
            return false;
        }
    }

    // ---- the connection loop --------------------------------------------

    private void loop() {
        int backoff = BACKOFF_MIN_MS;
        while (running) {
            try {
                Socket s = new Socket();
                s.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
                s.setSoTimeout(READ_TIMEOUT_MS);
                s.setTcpNoDelay(true);
                socket = s;
                out = s.getOutputStream();
                connected = true;
                backoff = BACKOFF_MIN_MS;
                listener.onLog("connected to " + host + ":" + port);
                listener.onConnected();
                subscribe();
                read(s);
            } catch (IOException e) {
                closeSocket(String.valueOf(e.getMessage()));
            }
            if (!running) break;
            try {
                Thread.sleep(backoff);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
            backoff = Math.min(BACKOFF_MAX_MS, backoff * 2);
        }
        connected = false;
    }

    /**
     * Subscribe to the observed properties and read each one once.
     *
     * The initial read matters: observe_property does fire immediately for a
     * property that has a value, but not for one that is currently unavailable
     * (mpv sends null), so without the explicit get we would sit on stale
     * defaults after a mid-playback reconnect.
     */
    private void subscribe() {
        observeIds.clear();
        int id = 1;
        for (String prop : observed) {
            observeIds.put(id, prop);
            command("observe_property", id, prop);
            id++;
        }
        for (final String prop : observed) {
            getProperty(prop).whenComplete((v, e) -> {
                if (e == null) listener.onProperty(prop, v);
            });
        }
    }

    private void read(Socket s) throws IOException {
        BufferedReader r = new BufferedReader(
                new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
        String line;
        while (running && (line = r.readLine()) != null) {
            if (line.isEmpty()) continue;
            try {
                dispatch(Json.parseObject(line));
            } catch (Json.ParseException e) {
                listener.onLog("unparseable line: " + e.getMessage());
            }
        }
        closeSocket("mpv closed the connection");
    }

    private void dispatch(Map<String, Object> msg) {
        Object rid = msg.get("request_id");
        if (rid instanceof Number) {
            CompletableFuture<Object> f = pending.remove(Integer.valueOf(((Number) rid).intValue()));
            if (f != null) {
                String err = Json.asString(msg.get("error"));
                if (err != null && !"success".equals(err)) {
                    f.completeExceptionally(new IOException("mpv: " + err));
                } else {
                    f.complete(msg.get("data"));
                }
            }
            return;
        }

        String event = Json.asString(msg.get("event"));
        if (event == null) return;

        if ("property-change".equals(event)) {
            String name = Json.asString(msg.get("name"));
            if (name == null) {
                Object oid = msg.get("id");
                if (oid instanceof Number) {
                    name = observeIds.get(Integer.valueOf(((Number) oid).intValue()));
                }
            }
            if (name != null) listener.onProperty(name, msg.get("data"));
            return;
        }

        listener.onEvent(event, msg);
    }

    private void closeSocket(String why) {
        boolean wasConnected = connected;
        connected = false;
        out = null;
        Socket s = socket;
        socket = null;
        if (s != null) {
            try { s.close(); } catch (IOException ignored) { }
        }
        for (Map.Entry<Integer, CompletableFuture<Object>> e :
                new ArrayList<Map.Entry<Integer, CompletableFuture<Object>>>(pending.entrySet())) {
            pending.remove(e.getKey());
            e.getValue().completeExceptionally(new IOException("disconnected: " + why));
        }
        if (wasConnected) {
            listener.onLog("disconnected: " + why);
            listener.onDisconnected(why);
        }
    }
}
