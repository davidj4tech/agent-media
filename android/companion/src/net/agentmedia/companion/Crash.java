package net.agentmedia.companion;

import java.io.File;
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
 * The handler writes the trace and the tail of the event log to a file in the
 * app's own storage, and {@code /crash} serves it back on the next start. It
 * survives the process precisely because it is on disk before the process dies.
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
    static void install(final File dir) {
        final Thread.UncaughtExceptionHandler prev =
                Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler((thread, error) -> {
            try {
                write(dir, thread, error);
            } catch (Throwable ignored) {
                // A crash handler that throws would replace a diagnosable crash
                // with an undiagnosable one.
            }
            if (prev != null) prev.uncaughtException(thread, error);
        });
    }

    private static void write(File dir, Thread thread, Throwable error) throws Exception {
        StringWriter sw = new StringWriter();
        PrintWriter pw = new PrintWriter(sw);
        pw.println("---- " + new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
                .format(new Date()) + "  thread=" + thread.getName());
        error.printStackTrace(pw);
        pw.println();
        pw.println("last " + CONTEXT_LINES + " events before the crash:");
        pw.println(CompanionService.dump(CONTEXT_LINES));
        pw.flush();

        // Append, so a crash loop shows its own shape rather than only its last
        // turn — three identical traces and one different first one is a
        // different story from three different ones.
        File f = new File(dir, FILE);
        try (FileOutputStream out = new FileOutputStream(f, true)) {
            out.write(sw.toString().getBytes(StandardCharsets.UTF_8));
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
