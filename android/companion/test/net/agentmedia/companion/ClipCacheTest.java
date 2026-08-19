package net.agentmedia.companion;

import com.sun.net.httpserver.HttpServer;

import java.io.File;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * A clip is fetched once, and every asker gets all of its bytes.
 *
 * This is the test the ENOENT in p8a's log wanted: three threads asking for one
 * clip at the same instant is not a hypothetical, it is what happens on every
 * reply — the warmup, the first sentence, and the prepare that runs one ahead.
 * The server here answers slowly and in pieces, because a fast local answer
 * finishes before the second asker arrives and the race never runs.
 */
public class ClipCacheTest {

    /** Big enough that a torn write is unmistakable in the length. */
    private static final int SIZE = 400_000;

    public static void main(String[] args) throws Exception {
        int failures = 0;

        AtomicInteger served = new AtomicInteger();
        HttpServer http = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        http.createContext("/clip.mp3", ex -> {
            served.incrementAndGet();
            byte[] chunk = new byte[8192];
            // Every server's bytes differ, so a file woven from two downloads
            // fails the content check even at the right length.
            byte fill = (byte) served.get();
            java.util.Arrays.fill(chunk, fill);
            ex.sendResponseHeaders(200, SIZE);
            try (OutputStream os = ex.getResponseBody()) {
                for (int sent = 0; sent < SIZE; sent += chunk.length) {
                    os.write(chunk, 0, Math.min(chunk.length, SIZE - sent));
                    os.flush();
                    try { Thread.sleep(2); } catch (InterruptedException ignored) { }
                }
            }
        });
        http.setExecutor(java.util.concurrent.Executors.newCachedThreadPool());
        http.start();
        String url = "http://127.0.0.1:" + http.getAddress().getPort() + "/clip.mp3";

        File dir = Files.createTempDirectory("clipcache").toFile();
        ClipCache cache = new ClipCache(dir);

        // The three askers of a real reply, released together.
        final int askers = 3;
        CountDownLatch go = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(askers);
        final File[] got = new File[askers];
        final Exception[] failed = new Exception[askers];
        for (int i = 0; i < askers; i++) {
            final int me = i;
            new Thread(() -> {
                try {
                    go.await();
                    got[me] = cache.fetch(url);
                } catch (Exception e) {
                    failed[me] = e;
                } finally {
                    done.countDown();
                }
            }, "asker-" + i).start();
        }
        go.countDown();
        done.await();

        for (int i = 0; i < askers; i++) {
            failures += check("asker " + i + " got a file, not an exception "
                    + (failed[i] == null ? "" : failed[i].toString()),
                    failed[i] == null && got[i] != null);
            if (got[i] == null) continue;
            failures += check("asker " + i + "'s file exists when it is handed back",
                    got[i].exists());
            failures += check("asker " + i + " got the whole clip, not a torn one",
                    got[i].length() == SIZE);
            failures += check("asker " + i + "'s bytes come from one download",
                    oneFill(got[i]));
        }
        failures += check("one download served all three askers, not three",
                served.get() == 1);

        // A second reply mentioning the same clip is a cache hit, not a fetch.
        cache.fetch(url);
        failures += check("an already-fetched clip is not downloaded again",
                served.get() == 1);

        // Nothing half-written is left where a later run would play it.
        List<String> leftovers = new ArrayList<String>();
        for (File f : dir.listFiles()) {
            if (f.getName().contains(".part")) leftovers.add(f.getName());
        }
        failures += check("no .part files survive a finished fetch " + leftovers,
                leftovers.isEmpty());

        // An unreachable clip fails loudly and leaves no debris behind for the
        // retry to trip over.
        String missing = "http://127.0.0.1:" + http.getAddress().getPort() + "/gone.mp3";
        boolean threw = false;
        try {
            cache.fetch(missing);
        } catch (Exception e) {
            threw = true;
        }
        failures += check("a missing clip throws rather than returning a ghost",
                threw);
        leftovers.clear();
        for (File f : dir.listFiles()) {
            if (f.getName().contains(".part")) leftovers.add(f.getName());
        }
        failures += check("a failed fetch leaves no .part behind " + leftovers,
                leftovers.isEmpty());

        http.stop(0);
        System.out.println(failures == 0 ? "ClipCacheTest: ok"
                : "ClipCacheTest: " + failures + " failure(s)");
        if (failures != 0) System.exit(1);
    }

    /** True when every byte is the same value: one download wrote this file. */
    private static boolean oneFill(File f) throws Exception {
        byte[] b = Files.readAllBytes(f.toPath());
        for (byte x : b) if (x != b[0]) return false;
        return true;
    }

    private static int check(String what, boolean ok) {
        System.out.println((ok ? "  ok   " : "  FAIL ") + what);
        return ok ? 0 : 1;
    }
}
