package net.agentmedia.companion;

/**
 * MpvState's memory of what it last played.
 *
 * The card outlives the clip now, which is only worth anything if it can still
 * name it — and mpv has cleared media-title and path by the time anyone asks.
 */
public final class StateTest {

    public static void main(String[] args) {
        int failures = 0;
        failures += forgetsNothingOnEndFile();
        failures += fallsBackToTheFilenameOnly();
        failures += emptyIsNotANewTitle();
        failures += aNewClipReplacesTheOld();
        failures += priorityDefaultsToNormal();
        failures += theQueueIsCounted();
        if (failures > 0) {
            System.out.println(failures + " failure(s)");
            System.exit(1);
        }
        System.out.println("StateTest: ok");
    }

    /** The clip ends, mpv clears everything, the title survives. */
    private static int forgetsNothingOnEndFile() {
        MpvState s = new MpvState();
        s.connected = true;
        s.apply("path", "/tmp/remote-20260814T190922-18480.mp3");
        s.apply("media-title", "the popup's help table, read aloud");
        s.apply("idle-active", Boolean.FALSE);

        s.apply("media-title", null);
        s.apply("path", null);
        s.apply("idle-active", Boolean.TRUE);

        return is("the popup's help table, read aloud", s.title(),
                  "idle state keeps the last title");
    }

    /** No media-title at all: the filename is what there is, so remember it. */
    private static int fallsBackToTheFilenameOnly() {
        MpvState s = new MpvState();
        s.connected = true;
        s.apply("path", "/music/Rothfuss/01 - The Name of the Wind.mp3");
        s.apply("path", null);
        return is("01 - The Name of the Wind.mp3", s.title(),
                  "the basename stands in when nothing named the clip");
    }

    /** An empty title is a clip ending, not a clip starting. */
    private static int emptyIsNotANewTitle() {
        MpvState s = new MpvState();
        s.connected = true;
        s.apply("media-title", "something");
        s.apply("media-title", "   ");
        return is("something", s.title(), "whitespace does not overwrite");
    }

    private static int aNewClipReplacesTheOld() {
        MpvState s = new MpvState();
        s.connected = true;
        s.apply("media-title", "first");
        s.apply("media-title", null);
        s.apply("media-title", "second");
        s.apply("media-title", null);
        return is("second", s.title(), "the newest clip is the one kept");
    }

    /**
     * A coordinator too old to state a priority, and an ordinary answer to a
     * question, deserve the same treatment — so absent reads as normal rather
     * than as nothing.
     */
    private static int priorityDefaultsToNormal() {
        MpvState s = new MpvState();
        int f = is("normal", s.priority, "unset is normal");
        s.apply(MpvIpc.PRIORITY_PROPERTY, "urgent");
        f += is("urgent", s.priority, "and follows what it is told");
        s.apply(MpvIpc.PRIORITY_PROPERTY, null);
        f += is("normal", s.priority, "a cleared flag falls back, not blank");
        return f;
    }

    /** How many replies are stacked behind a hold — mpv's own playlist-count. */
    private static int theQueueIsCounted() {
        MpvState s = new MpvState();
        int f = (s.queued == 0) ? 0 : fail("starts empty", "0", "" + s.queued);
        boolean changed = s.apply(MpvIpc.QUEUE_PROPERTY, Double.valueOf(3));
        f += (s.queued == 3) ? 0 : fail("counts three", "3", "" + s.queued);
        f += changed ? 0 : fail("a new count is a change", "true", "false");
        f += s.apply(MpvIpc.QUEUE_PROPERTY, Double.valueOf(3))
                ? fail("the same count is not", "false", "true") : 0;
        return f;
    }

    private static int fail(String what, String want, String got) {
        System.out.println("FAIL " + what + ": want [" + want + "] got [" + got + "]");
        return 1;
    }

    /** Nothing ever played: the app's own name, as before. */
    private static int is(String want, String got, String what) {
        if (want.equals(got)) return 0;
        System.out.println("FAIL " + what + ": want [" + want + "] got [" + got + "]");
        return 1;
    }
}
