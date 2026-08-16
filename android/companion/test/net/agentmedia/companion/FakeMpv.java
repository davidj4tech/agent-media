package net.agentmedia.companion;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;

/**
 * A stand-in for mpv's JSON IPC, speaking the same line protocol over TCP.
 *
 * Exists so the client can be tested on the build host. That is not a
 * convenience: p8a has no adb, so anything not proven before the APK is
 * sideloaded gets debugged by squinting at a phone screen.
 */
final class FakeMpv implements AutoCloseable {

    private final ServerSocket server;
    private final Thread acceptor;
    /**
     * Every live connection, not just the newest. The OS reuses ports briefly,
     * so a client from an earlier test that has not finished shutting down can
     * land here — if only one socket were tracked, dropClient() would close
     * that stray and leave the connection under test open.
     */
    private final List<Socket> clients =
            Collections.synchronizedList(new ArrayList<Socket>());
    private volatile boolean running = true;

    /** Every command object the client sent, in order. */
    final BlockingQueue<Map<String, Object>> received =
            new ArrayBlockingQueue<Map<String, Object>>(256);
    /** Property values this fake will answer get_property with. */
    final Map<String, Object> properties = new ConcurrentHashMap<String, Object>();
    /** observe_property registrations: id -> name. */
    final Map<Integer, String> observers = new ConcurrentHashMap<Integer, String>();
    /** When true, reply to nothing — used to exercise the request timeout. */
    volatile boolean mute = false;

    FakeMpv() throws IOException {
        server = new ServerSocket(0);
        acceptor = new Thread(this::accept, "fake-mpv");
        acceptor.setDaemon(true);
        acceptor.start();
    }

    int port() {
        return server.getLocalPort();
    }

    private void accept() {
        while (running) {
            try {
                final Socket s = server.accept();
                clients.add(s);
                Thread t = new Thread(() -> serve(s), "fake-mpv-conn");
                t.setDaemon(true);
                t.start();
            } catch (IOException e) {
                if (running) continue;
                return;
            }
        }
    }

    private void serve(Socket s) {
        try {
            BufferedReader r = new BufferedReader(
                    new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
            String line;
            while (running && (line = r.readLine()) != null) {
                Map<String, Object> msg = Json.parseObject(line);
                received.offer(msg);
                handle(s, msg);
            }
        } catch (IOException ignored) {
            // client went away; the acceptor loop takes the next one
        } finally {
            clients.remove(s);
            try { s.close(); } catch (IOException ignored) { }
        }
    }

    private void handle(Socket s, Map<String, Object> msg) {
        Object cmdObj = msg.get("command");
        if (!(cmdObj instanceof List)) return;
        List<?> cmd = (List<?>) cmdObj;
        if (cmd.isEmpty()) return;
        String verb = String.valueOf(cmd.get(0));
        int id = (int) Json.asDouble(msg.get("request_id"), 0);

        if ("observe_property".equals(verb) && cmd.size() >= 3) {
            observers.put((int) Json.asDouble(cmd.get(1), 0), String.valueOf(cmd.get(2)));
            reply(s, id, null, "success");
            return;
        }
        if ("get_property".equals(verb) && cmd.size() >= 2) {
            String name = String.valueOf(cmd.get(1));
            if (properties.containsKey(name)) {
                reply(s, id, properties.get(name), "success");
            } else {
                reply(s, id, null, "property unavailable");
            }
            return;
        }
        if ("set_property".equals(verb) && cmd.size() >= 3) {
            String name = String.valueOf(cmd.get(1));
            properties.put(name, cmd.get(2));
            reply(s, id, null, "success");
            publish(name, cmd.get(2));
            return;
        }
        reply(s, id, null, "success");
    }

    /** Send a property-change event for every observer registered on `name`. */
    void publish(String name, Object value) {
        properties.put(name, value);
        for (Map.Entry<Integer, String> e : observers.entrySet()) {
            if (!e.getValue().equals(name)) continue;
            Map<String, Object> ev = new LinkedHashMap<String, Object>();
            ev.put("event", "property-change");
            ev.put("id", e.getKey());
            ev.put("name", name);
            ev.put("data", value);
            send(Json.write(ev));
        }
    }

    /**
     * Publish without the "name" field, which is how some mpv builds report a
     * change — the client has to resolve it from the observe id.
     */
    void publishIdOnly(String name, Object value) {
        properties.put(name, value);
        for (Map.Entry<Integer, String> e : observers.entrySet()) {
            if (!e.getValue().equals(name)) continue;
            Map<String, Object> ev = new LinkedHashMap<String, Object>();
            ev.put("event", "property-change");
            ev.put("id", e.getKey());
            ev.put("data", value);
            send(Json.write(ev));
        }
    }

    void sendEvent(String event) {
        Map<String, Object> ev = new LinkedHashMap<String, Object>();
        ev.put("event", event);
        send(Json.write(ev));
    }

    private void reply(Socket s, int id, Object data, String error) {
        if (mute) return;
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("data", data);
        m.put("request_id", id);
        m.put("error", error);
        writeTo(s, Json.write(m));
    }

    /** Send to every connected client — events are not request-scoped. */
    void send(String line) {
        for (Socket s : new ArrayList<Socket>(clients)) writeTo(s, line);
    }

    private void writeTo(Socket s, String line) {
        try {
            OutputStream o = s.getOutputStream();
            synchronized (s) {
                o.write((line + "\n").getBytes(StandardCharsets.UTF_8));
                o.flush();
            }
        } catch (IOException ignored) { }
    }

    /**
     * Drop every live connection, leaving the listener up — "mpv went away".
     *
     * The wait is the point. A client's {@code connect()} returns as soon as the
     * TCP handshake completes, which is before this side's acceptor thread has
     * run and put the socket in {@link #clients} — so a test that connects and
     * immediately drops could close nothing at all, leave the connection alive,
     * and then fail waiting for a disconnect that was never going to come. It
     * looked like a flaky reconnect test and it was this: on 2026-08-17 the
     * music channel grew three more observed properties, the extra handshake
     * traffic shifted the timing, and a race that had been losing occasionally
     * started losing half the time.
     */
    void dropClient() {
        observers.clear();
        long deadline = System.currentTimeMillis() + 2000;
        while (clients.isEmpty() && System.currentTimeMillis() < deadline) {
            try {
                Thread.sleep(5);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
        }
        closeClients();
    }

    /** Close what has been accepted, without waiting for anything to arrive. */
    private void closeClients() {
        for (Socket s : new ArrayList<Socket>(clients)) {
            clients.remove(s);
            try { s.close(); } catch (IOException ignored) { }
        }
    }

    Map<String, Object> nextCommand(long timeoutMs) throws InterruptedException {
        return received.poll(timeoutMs, TimeUnit.MILLISECONDS);
    }

    @Override
    public void close() {
        running = false;
        // Not dropClient(): shutting down must never sit waiting two seconds
        // for a connection that is not coming.
        observers.clear();
        closeClients();
        try { server.close(); } catch (IOException ignored) { }
    }
}
