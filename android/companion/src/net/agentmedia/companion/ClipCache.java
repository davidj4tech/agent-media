package net.agentmedia.companion;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

/**
 * The clip files a reply is played from, fetched once each.
 *
 * Split out of {@link BuiltinSpeech} — and importing nothing from
 * {@code android.*} — because the bug it exists to prevent is a race, and a
 * race is only really fixed by a test that used to lose it. p8a has no adb, so
 * anything left inside the player is verified by sideloading and listening.
 *
 * <h4>The race</h4>
 *
 * Three callers want the same clip the moment a reply is queued: the
 * whole-reply warmup, the first {@code startCurrent}, and the
 * {@code prepareNext} that runs one sentence ahead. Each used to download to
 * the same {@code <name>.clip.part}, so on a slow link two of them interleaved
 * bytes into one file, and whichever renamed second was left holding a name the
 * winner had already moved:
 *
 * <pre>
 *   builtin-speech: ...--000.mp3 failed: FileNotFoundException
 *                   .../640a02fc.clip.part: ENOENT
 * </pre>
 *
 * A listener hears that twice over: a losing {@code startCurrent} skips the
 * sentence, and a losing {@code prepareNext} leaves nothing for
 * {@code setNextMediaPlayer}, so the reply stops between two sentences for as
 * long as it takes to fetch and prepare the clip again — the pause in the
 * middle of a reply that sounds like buffering.
 *
 * <h4>What replaces it</h4>
 *
 * One download per URL: whoever asks second waits on the first and is handed
 * its file. And a private temp file per attempt, so that even if a duplicate
 * download does start, the two cannot write each other's bytes or rename each
 * other's file away — the lock saves the bandwidth, the temp name is the
 * correctness.
 */
final class ClipCache {

    /** Downloads in flight, keyed by URL. Present only while running. */
    private final ConcurrentMap<String, Object> fetching =
            new ConcurrentHashMap<String, Object>();

    private final File dir;

    ClipCache(File dir) {
        this.dir = dir;
    }

    /** Where a URL's bytes live once fetched. */
    File fileFor(String url) {
        return new File(dir, Integer.toHexString(url.hashCode()) + ".clip");
    }

    /**
     * The clip as a complete file on disk, downloading it if it is not there.
     *
     * Blocks — for the download, or for the one already running.
     */
    File fetch(String url) throws Exception {
        dir.mkdirs();
        File out = fileFor(url);
        if (out.length() > 0) return out;
        // The map's value is the monitor, so every caller for this URL waits on
        // one object even though their `url` strings are separate instances.
        Object lock = fetching.putIfAbsent(url, url);
        if (lock == null) lock = url;
        synchronized (lock) {
            try {
                // It may have landed while we waited on the lock.
                if (out.length() > 0) return out;
                File part = File.createTempFile(out.getName(), ".part", dir);
                try {
                    download(url, part);
                } catch (Exception e) {
                    part.delete();
                    throw e;
                }
                // Rename last: a half-written file that is never renamed is
                // retried, where a half-written file at the real name would be
                // played. If the rename loses anyway, what it lost to is this
                // same clip, complete — prefer that and drop ours. Only when
                // there is no such file do we return the temp, which is
                // complete too and, being ours alone, is still there.
                if (!part.renameTo(out)) {
                    if (out.length() > 0) {
                        part.delete();
                        return out;
                    }
                    return part;
                }
                return out;
            } finally {
                fetching.remove(url, lock);
            }
        }
    }

    private void download(String url, File into) throws Exception {
        HttpURLConnection c = (HttpURLConnection) URI.create(url).toURL()
                .openConnection();
        c.setConnectTimeout(5000);
        c.setReadTimeout(20000);
        try (InputStream in = c.getInputStream();
             OutputStream os = new FileOutputStream(into)) {
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
        } finally {
            c.disconnect();
        }
    }
}
