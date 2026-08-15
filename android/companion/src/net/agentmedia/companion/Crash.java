package net.agentmedia.companion;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.net.Uri;
import android.os.Environment;
import android.provider.MediaStore;

import java.io.File;
import java.io.OutputStream;
import java.io.FileOutputStream;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * The app's own crash record, because nothing else on this phone keeps one.
 *
 * `logcat` from Termux shows only Termux's uid, adb cannot reach p8a, and the
 * in-memory event log dies with the process that holds it. So "agent-media keeps
 * stopping" arrived with no stack trace, no line number and no way to get one —
 * the same blind spot the /log readout was built to close, one level down.
 *
 * It writes in two places, and the second is the one that matters.
 *
 * The app's own storage, served back by {@code /crash} — useless for the crash
 * that stops the app coming up, because the server that would serve it dies
 * with the process. That was the first version, and it answered nothing.
 *
 * And a file in **Downloads**, via MediaStore, which needs no permission and
 * which Termux can read on the other side of the uid boundary — the same
 * directory the APK is copied into. A crash loop is legible from red5 without
 * the app running at all, which is the whole point.
 */
final class Crash {

    private static final String FILE = "crash.log";
    /** How much of the event log to keep with the trace — what led up to it. */
    private static final int CONTEXT_LINES = 40;

    private Crash() { }

    /**
     * Install the recorder. Chains to whatever handler was there (Android's,
     * which is what actually shows the dialog and kills the process) so the
     * behaviour is unchanged apart from the record.
     */
    static void install(final Context ctx) {
        install(ctx, ctx.getFilesDir());
    }

    static void install(final Context ctx, final File dir) {
        final Thread.UncaughtExceptionHandler prev =
                Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler((thread, error) -> {
            try {
                String record = record(thread, error);
                write(dir, record);
                publishToDownloads(ctx, record);
            } catch (Throwable ignored) {
                // A crash handler that throws would replace a diagnosable crash
                // with an undiagnosable one.
            }
            if (prev != null) prev.uncaughtException(thread, error);
        });
    }

    private static String record(Thread thread, Throwable error) {
        StringWriter sw = new StringWriter();
        PrintWriter pw = new PrintWriter(sw);
        pw.println("---- " + new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
                .format(new Date()) + "  thread=" + thread.getName());
        error.printStackTrace(pw);
        pw.println();
        pw.println("last " + CONTEXT_LINES + " events before the crash:");
        pw.println(CompanionService.dump(CONTEXT_LINES));
        pw.flush();
        return sw.toString();
    }

    /**
     * Append, so a crash loop shows its own shape rather than only its last
     * turn — three identical traces and one different first one is a different
     * story from three different ones.
     */
    private static void write(File dir, String record) throws Exception {
        try (FileOutputStream out = new FileOutputStream(new File(dir, FILE), true)) {
            out.write(record.getBytes(StandardCharsets.UTF_8));
        }
    }

    /**
     * The copy that can be read while the app is down.
     *
     * MediaStore's Downloads collection is writable by any app with no
     * permission at all, and lands in /storage/emulated/0/Download — which is
     * where the APK is scp'd to, so it is already known to be readable from
     * Termux. One file per crash, named by the clock, because a loop is a
     * sequence and overwriting would hide it.
     */
    private static void publishToDownloads(Context ctx, String record) {
        try {
            String name = "agent-media-crash-"
                    + new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date())
                    + ".txt";
            ContentValues v = new ContentValues();
            v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
            v.put(MediaStore.MediaColumns.MIME_TYPE, "text/plain");
            v.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
            ContentResolver cr = ctx.getContentResolver();
            Uri uri = cr.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
            if (uri == null) return;
            try (OutputStream out = cr.openOutputStream(uri)) {
                if (out != null) out.write(record.getBytes(StandardCharsets.UTF_8));
            }
        } catch (Throwable ignored) {
            // The in-app copy is still written; this is the bonus route.
        }
    }

    /** What /crash serves. Newest last, since a trace reads downwards. */
    static String read(File dir) {
        File f = new File(dir, FILE);
        if (!f.exists()) return "(no crash recorded)";
        try {
            return new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "crash.log unreadable: " + e;
        }
    }

    static void clear(File dir) {
        new File(dir, FILE).delete();
    }
}
