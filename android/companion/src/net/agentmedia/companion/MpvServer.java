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
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * This app, answering on the port mpv used to answer on.
 *
 * <h4>Why speak mpv's protocol rather than invent one</h4>
 *
 * The server drives the phone by writing JSON IPC into a socket:
 * {@code SinkSpeech.play} is a {@code loadfile}, a reply is a batch of
 * {@code playlist-clear} / {@code loadfile append} / {@code playlist-pos 0},
 * and the coordinator follows along by reading {@code playlist-pos} back. All
 * of that is a year old and has had its failures found and fixed one at a time.
 *
 * An in-app player could have had a new transport — the proposal assumed one,
 * and SSE was the obvious candidate. David's steer was better: <b>keep the
 * socket, move the player</b>. If the app answers on 6602 speaking the verbs
 * mpv answered with, then `sinks/speech` never learns it changed, the
 * coordinator's playlist-following keeps working, and the thing that finally
 * changes is only who makes the noise — which is the whole point, because the
 * noise is what audio focus is about.
 *
 * <h4>What it implements, and what it refuses</h4>
 *
 * The speech channel's vocabulary and nothing else: {@code loadfile},
 * {@code stop}, {@code playlist-clear}, {@code playlist-next}/{@code -prev},
 * and get/set/observe of the properties that channel actually uses. An unknown
 * property answers {@code property not found} exactly as an older mpv does for
 * {@code user-data} — the server's writes there are already best-effort and
 * already tolerate that error, so unimplemented and old-mpv are the same shape
 * of failure rather than a new one.
 *
 * Metadata properties ({@code force-media-title},
 * {@code user-data/agent-media/*}) are stored and observable but drive nothing.
 * They are how the reply names itself on the card, and the app is now both ends
 * of that conversation.
 *
 * <h4>Boundaries</h4>
 *
 * {@code android.*}-free, so {@code test/run.sh} replays the real byte
 * sequences {@code SinkSpeech} sends against a fake player on the build host.
 * The player itself is {@link Player}, implemented on Android by
 * {@code BuiltinSpeech}; nothing here knows what a {@code MediaPlayer} is.
 */
final class MpvServer {

    /** What the protocol needs a player to be able to do. */
    interface Player {
        /** {@code loadfile <uri> replace|append|append-play}. */
        void load(String uri, String mode);
        /** {@code playlist-clear}: everything but the current entry. */
        void playlistClear();
        /** {@code stop}: end playback and empty the playlist. */
        void stop();
        /** Jump to an index; -1 means "nothing playing". */
        void playlistPos(int index);
        void playlistNext();
        void playlistPrev();

        /**
         * Move the playhead to {@code seconds} into the current clip.
         *
         * Absolute and already in seconds: mpv's flags (relative, percent) are
         * arithmetic on {@code timePos}/{@code duration}, which is protocol,
         * so they are done here rather than by every player. Past the end
         * finishes the clip the way running into it does — that is what the
         * popup's `>` is asking for when it seeks to 100%.
         */
        void seek(double seconds);

        void pause(boolean paused);
        void mute(boolean muted);
        /** 0-100, mpv's scale, not Android's. */
        void volume(double volume);
        void speed(double speed);

        boolean paused();
        boolean muted();
        double volume();
        double speed();
        int playlistPos();
        int playlistCount();
        /** Seconds into the current clip, or -1 when nothing is playing. */
        double timePos();
        /** Seconds, or -1 when unknown. */
        double duration();
        /** The current entry's URI, or null. */
        String path();
        /** Nothing loaded and nothing playing — mpv's {@code idle-active}. */
        boolean idle();
    }

    /** Properties the protocol stores but does not act on. */
    private static final String TITLE = "force-media-title";
    private static final String USER_DATA = "user-data/";

    private final int port;
    private final String bindAddress;
    private final Player player;
    private final Listener listener;

    /** Stored-but-inert properties: titles, the reply's text, the flags. */
    private final Map<String, Object> stored =
            java.util.Collections.synchronizedMap(new LinkedHashMap<String, Object>());
    private final List<Client> clients = new CopyOnWriteArrayList<Client>();

    private volatile ServerSocket server;
    private volatile boolean running;
    private Thread thread;
    /**
     * Told after any property moves, whoever moved it.
     *
     * The card is the reason this exists. Metadata the server sets —
     * {@code force-media-title}, the speaking flag, the priority — reaches this
     * class and nowhere else, so a player mirroring only its own playback would
     * show a reply with no name on it. One hook covers both halves: what
     * playback did, and what the server said about it.
     */
    private volatile Runnable observer;

    interface Listener {
        void onLog(String line);
    }

    MpvServer(String bindAddress, int port, Player player, Listener listener) {
        this.bindAddress = bindAddress;
        this.port = port;
        this.player = player;
        this.listener = listener;
    }

    int boundPort() {
        ServerSocket s = server;
        return (s == null || s.isClosed()) ? -1 : s.getLocalPort();
    }

    synchronized void start() {
        if (running) return;
        running = true;
        thread = new Thread(this::loop, "mpv-server");
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
        for (Client c : clients) c.close();
        clients.clear();
        if (thread != null) {
            thread.interrupt();
            thread = null;
        }
    }

    /** What a stored property says now — for the card, which reads them. */
    String storedText(String name) {
        Object v = stored.get(name);
        return v == null ? "" : Json.asString(v);
    }

    boolean storedFlag(String name) {
        return Json.asBool(stored.get(name), false);
    }

    /**
     * Tell every observer a property changed.
     *
     * Called by the player when playback moves it — a clip ending advances
     * {@code playlist-pos}, and the coordinator is watching that to follow the
     * reply sentence by sentence. Without this the app would answer questions
     * correctly and still break highlighting, because mpv volunteers changes.
     */
    void changed(String name) {
        for (Client c : clients) c.notifyChange(name);
        Runnable r = observer;
        if (r != null) r.run();
    }

    /** Watch every change, for a card that has to be redrawn when one lands. */
    void onAnyChange(Runnable r) {
        this.observer = r;
    }

    private void loop() {
        try {
            ServerSocket s = new ServerSocket();
            s.setReuseAddress(true);
            s.bind(new InetSocketAddress(InetAddress.getByName(bindAddress), port));
            server = s;
            log("listening on " + bindAddress + ":" + s.getLocalPort());
        } catch (IOException e) {
            log("bind failed on " + bindAddress + ":" + port + ": " + e);
            running = false;
            return;
        }
        while (running) {
            try {
                Socket sock = server.accept();
                Client c = new Client(sock);
                clients.add(c);
                Thread t = new Thread(c::serve, "mpv-client");
                t.setDaemon(true);
                t.start();
            } catch (IOException e) {
                if (!running) return;
            }
        }
    }

    private void log(String line) {
        if (listener != null) listener.onLog("mpv-server: " + line);
    }

    // ---- one connection ---------------------------------------------------

    private final class Client {
        private final Socket socket;
        private OutputStream out;
        /** Observed property name -> the client's own id for it. */
        private final Map<String, Long> observed =
                java.util.Collections.synchronizedMap(new LinkedHashMap<String, Long>());
        /**
         * The last value sent for each observed property.
         *
         * mpv volunteers a change, not a heartbeat, and a follower is entitled
         * to treat every event as news: the first end-to-end run against a real
         * reply sent playlist-pos twice per sentence, because both the advance
         * and the start of the next clip announced it. Harmless to a reader
         * that re-reads state, and not harmless to one that acts on each event.
         */
        private final Map<String, String> lastSent =
                java.util.Collections.synchronizedMap(new LinkedHashMap<String, String>());

        Client(Socket socket) {
            this.socket = socket;
        }

        void serve() {
            try {
                out = socket.getOutputStream();
                BufferedReader in = new BufferedReader(new InputStreamReader(
                        socket.getInputStream(), StandardCharsets.UTF_8));
                String line;
                while ((line = in.readLine()) != null) {
                    if (line.trim().isEmpty()) continue;
                    handle(line);
                }
            } catch (IOException ignored) {
                // A dropped connection is the normal end of a client.
            } finally {
                close();
                clients.remove(this);
            }
        }

        private void handle(String line) {
            Map<String, Object> req;
            try {
                req = Json.parseObject(line);
            } catch (RuntimeException e) {
                send(error(null, "invalid parameter"));
                return;
            }
            Object id = req.get("request_id");
            Object cmd = req.get("command");
            if (!(cmd instanceof List) || ((List<?>) cmd).isEmpty()) {
                send(error(id, "invalid parameter"));
                return;
            }
            List<?> argv = (List<?>) cmd;
            String verb = Json.asString(argv.get(0));
            try {
                send(dispatch(verb, argv, id));
                // mpv answers the command, and only then volunteers the
                // property's current value. A follower that reads a
                // property-change where it expected its own reply is reading
                // the previous request's answer for the rest of the session,
                // so the order is part of the contract rather than a detail.
                if (verb.startsWith("observe_property") && argv.size() > 2) {
                    notifyChange(Json.asString(argv.get(2)));
                }
            } catch (RuntimeException e) {
                // Never let one malformed command take the connection down:
                // the server's writes are best-effort and it will keep talking.
                send(error(id, "invalid parameter"));
            }
        }

        private Map<String, Object> dispatch(String verb, List<?> argv, Object id) {
            if ("loadfile".equals(verb)) {
                String uri = Json.asString(argv.get(1));
                String mode = argv.size() > 2 ? Json.asString(argv.get(2)) : "replace";
                player.load(uri, mode);
                return ok(id, null);
            }
            if ("stop".equals(verb)) {
                player.stop();
                return ok(id, null);
            }
            if ("playlist-clear".equals(verb)) {
                player.playlistClear();
                return ok(id, null);
            }
            if ("playlist-next".equals(verb)) {
                player.playlistNext();
                return ok(id, null);
            }
            if ("playlist-prev".equals(verb)) {
                player.playlistPrev();
                return ok(id, null);
            }
            if ("get_property".equals(verb) || "get_property_string".equals(verb)) {
                String name = Json.asString(argv.get(1));
                Object v = get(name);
                if (v == NOT_FOUND) return error(id, "property not found");
                return ok(id, "get_property_string".equals(verb) ? asText(v) : v);
            }
            if ("set_property".equals(verb) || "set_property_string".equals(verb)) {
                String name = Json.asString(argv.get(1));
                if (!set(name, argv.size() > 2 ? argv.get(2) : null)) {
                    return error(id, "property not found");
                }
                changed(name);
                return ok(id, null);
            }
            if ("observe_property".equals(verb)
                    || "observe_property_string".equals(verb)) {
                long observeId = (long) Json.asDouble(argv.get(1), 0);
                String name = Json.asString(argv.get(2));
                // The initial value follows the reply, not precedes it; see
                // handle(). Registering here is all this does.
                observed.put(name, observeId);
                return ok(id, null);
            }
            if ("unobserve_property".equals(verb)) {
                long observeId = (long) Json.asDouble(argv.get(1), 0);
                synchronized (observed) {
                    observed.entrySet().removeIf(e -> {
                        if (e.getValue() != observeId) return false;
                        lastSent.remove(e.getKey());
                        return true;
                    });
                }
                return ok(id, null);
            }
            if ("seek".equals(verb)) {
                Double target = seekTarget(argv);
                if (target == null) return error(id, "invalid parameter");
                player.seek(target.doubleValue());
                changed("time-pos");
                return ok(id, null);
            }
            if ("cycle".equals(verb)) {
                // mpv's own flip-this-flag verb. Nothing here sends it any
                // more — a fire-and-forget `cycle` that this server refused
                // was how the popup's Space key stopped pausing speech, so
                // the CLI now writes the value it wants — but an older
                // checkout on another host still does, and refusing a verb
                // mpv answers is the app failing to be the thing it claims
                // to be on this socket.
                String name = Json.asString(argv.get(1));
                Object v = get(name);
                if (v == NOT_FOUND || !(v instanceof Boolean)) {
                    return error(id, "property not found");
                }
                set(name, !((Boolean) v).booleanValue());
                changed(name);
                return ok(id, null);
            }
            if ("client_name".equals(verb)) {
                return ok(id, "agent-media-companion");
            }
            if ("quit".equals(verb) || "quit-watch-later".equals(verb)) {
                // Refused on purpose: quitting mpv meant "stop the player";
                // here it would mean "kill the app", and the app is also the
                // thing holding the phone's audio together.
                player.stop();
                return ok(id, null);
            }
            return error(id, "invalid parameter");
        }

        void notifyChange(String name) {
            Long observeId = observed.get(name);
            if (observeId == null) return;
            Object v = get(name);
            String encoded = v == NOT_FOUND ? "" : Json.write(v);
            if (encoded.equals(lastSent.put(name, encoded))) return;
            Map<String, Object> ev = new LinkedHashMap<String, Object>();
            ev.put("event", "property-change");
            ev.put("id", observeId);
            ev.put("name", name);
            if (v != NOT_FOUND) ev.put("data", v);
            send(ev);
        }

        private void send(Map<String, Object> message) {
            OutputStream o = out;
            if (o == null) return;
            try {
                synchronized (this) {
                    o.write((Json.write(message) + "\n")
                            .getBytes(StandardCharsets.UTF_8));
                    o.flush();
                }
            } catch (IOException e) {
                close();
            }
        }

        void close() {
            try { socket.close(); } catch (IOException ignored) { }
        }
    }

    // ---- properties -------------------------------------------------------

    /**
     * Where {@code seek <value> [flags]} wants the playhead, in seconds.
     *
     * mpv's four modes are all the same jump once you know where you are and
     * how long the clip is, and the exactness modifiers ({@code +exact},
     * {@code +keyframes}) describe how mpv gets there — nothing this player
     * can do differently. @return null when the mode is one we do not know,
     * which is answered as mpv answers a command it cannot make sense of.
     */
    private Double seekTarget(List<?> argv) {
        if (argv.size() < 2) return null;
        double value = Json.asDouble(argv.get(1), Double.NaN);
        if (Double.isNaN(value)) return null;
        String flags = argv.size() > 2 ? Json.asString(argv.get(2)) : "relative";
        if (flags == null || flags.isEmpty()) flags = "relative";
        String mode = flags.split("\\+")[0];
        double pos = Math.max(0, player.timePos());
        double dur = player.duration();
        if ("relative".equals(mode)) return pos + value;
        if ("absolute".equals(mode)) return value;
        if (dur < 0) {
            // A percentage of an unknown length is not a position. Seeking to
            // an invented one would be worse than saying so: `>` would land
            // mid-clip and look like the key had missed.
            return null;
        }
        if ("absolute-percent".equals(mode)) return dur * value / 100.0;
        if ("relative-percent".equals(mode)) return pos + dur * value / 100.0;
        return null;
    }

    /** Distinguishes "no such property" from a property whose value is null. */
    private static final Object NOT_FOUND = new Object();

    private Object get(String name) {
        if ("pause".equals(name)) return player.paused();
        if ("mute".equals(name)) return player.muted();
        if ("volume".equals(name)) return player.volume();
        if ("speed".equals(name)) return player.speed();
        if ("playlist-pos".equals(name)) return player.playlistPos();
        if ("playlist-count".equals(name)) return player.playlistCount();
        if ("idle-active".equals(name)) return player.idle();
        if ("path".equals(name) || "filename".equals(name)) {
            String p = player.path();
            return p == null ? NOT_FOUND : p;
        }
        if ("time-pos".equals(name)) {
            double t = player.timePos();
            return t < 0 ? NOT_FOUND : t;
        }
        if ("duration".equals(name)) {
            double d = player.duration();
            return d < 0 ? NOT_FOUND : d;
        }
        if ("media-title".equals(name)) {
            Object title = stored.get(TITLE);
            if (title != null) return title;
            String p = player.path();
            return p == null ? NOT_FOUND : p.substring(p.lastIndexOf('/') + 1);
        }
        if (isStored(name)) {
            Object v = stored.get(name);
            return v == null ? NOT_FOUND : v;
        }
        return NOT_FOUND;
    }

    /** @return false when there is no such property, as mpv would say. */
    private boolean set(String name, Object value) {
        if ("pause".equals(name)) {
            player.pause(Json.asBool(value, false));
            return true;
        }
        if ("mute".equals(name)) {
            player.mute(Json.asBool(value, false));
            return true;
        }
        if ("volume".equals(name)) {
            player.volume(Json.asDouble(value, 100));
            return true;
        }
        if ("speed".equals(name)) {
            player.speed(Json.asDouble(value, 1.0));
            return true;
        }
        if ("playlist-pos".equals(name)) {
            player.playlistPos((int) Json.asDouble(value, -1));
            return true;
        }
        if (isStored(name)) {
            stored.put(name, value);
            return true;
        }
        // gapless-audio, audio-device and friends: accepted and ignored. The
        // app has one output and no gaps to close, but refusing them would make
        // the sink log a warning per reply about a setting that never mattered.
        if ("gapless-audio".equals(name) || "audio-device".equals(name)
                || "keep-open".equals(name) || "idle".equals(name)) {
            stored.put(name, value);
            return true;
        }
        return false;
    }

    private static boolean isStored(String name) {
        return TITLE.equals(name) || name.startsWith(USER_DATA);
    }

    private static String asText(Object v) {
        if (v instanceof Boolean) return ((Boolean) v) ? "yes" : "no";
        return Json.asString(v);
    }

    private static Map<String, Object> ok(Object id, Object data) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        if (data != null) m.put("data", data);
        m.put("error", "success");
        if (id != null) m.put("request_id", id);
        return m;
    }

    private static Map<String, Object> error(Object id, String message) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("error", message);
        if (id != null) m.put("request_id", id);
        return m;
    }

    /** For tests and diagnostics: the properties being stored, in order. */
    Map<String, Object> storedProperties() {
        synchronized (stored) {
            return new LinkedHashMap<String, Object>(stored);
        }
    }

    /** For diagnostics: how many clients are connected. */
    int clientCount() {
        return clients.size();
    }

    /** Unused today, kept so the field is not dead weight in a review. */
    List<String> observedNames() {
        List<String> names = new ArrayList<String>();
        for (Client c : clients) names.addAll(c.observed.keySet());
        return names;
    }
}
