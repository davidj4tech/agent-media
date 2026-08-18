package net.agentmedia.speedspike;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

/**
 * The spike's results over loopback HTTP, so the answer does not have to be
 * read off a phone screen and retyped.
 *
 * The companion app learned this lesson already ({@code StatusServer}): p8a has
 * no adb, logcat from Termux shows only Termux's own uid, and the previous
 * spike's readout was "screenshot the activity". A spike whose whole output is
 * a table of numbers should hand that table to whoever is asking —
 * {@code ssh p8a curl 127.0.0.1:8772/} and the run is in the transcript.
 *
 * Bound to 127.0.0.1 and nothing else. Hand-rolled because Android ships no
 * {@code com.sun.net.httpserver}, and {@code android.*}-free so the build host
 * can run it.
 */
final class Readout {

    /** 8770 is the companion's status server, 8771 media-share. This is next. */
    static final int PORT = 8772;

    interface Source {
        /** The whole run so far, newest trial last. */
        String report();
    }

    private final Source source;
    private volatile ServerSocket server;
    private volatile boolean running;

    Readout(Source source) {
        this.source = source;
    }

    synchronized void start() {
        if (running) return;
        running = true;
        Thread t = new Thread(this::loop, "spike-readout");
        t.setDaemon(true);
        t.start();
    }

    synchronized void stop() {
        running = false;
        ServerSocket s = server;
        server = null;
        if (s != null) {
            try { s.close(); } catch (IOException ignored) { }
        }
    }

    /** The port actually bound, or -1 before start / after a bind failure. */
    int boundPort() {
        ServerSocket s = server;
        return (s == null || s.isClosed()) ? -1 : s.getLocalPort();
    }

    private void loop() {
        try {
            ServerSocket s = new ServerSocket();
            s.setReuseAddress(true);
            s.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), PORT));
            server = s;
        } catch (IOException e) {
            running = false;
            return;
        }
        while (running) {
            try (Socket c = server.accept()) {
                serve(c);
            } catch (IOException e) {
                if (!running) return;
            }
        }
    }

    private void serve(Socket c) throws IOException {
        // The request line is read and discarded: every path answers the same
        // thing. A spike with routes is a spike that grew a second job.
        InputStream in = c.getInputStream();
        byte[] scratch = new byte[2048];
        in.read(scratch);
        byte[] body = source.report().getBytes(StandardCharsets.UTF_8);
        OutputStream out = c.getOutputStream();
        out.write(("HTTP/1.1 200 OK\r\n"
                + "Content-Type: text/plain; charset=utf-8\r\n"
                + "Content-Length: " + body.length + "\r\n"
                + "Connection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        out.write(body);
        out.flush();
    }
}
