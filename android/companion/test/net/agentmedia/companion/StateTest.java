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

    /** Nothing ever played: the app's own name, as before. */
    private static int is(String want, String got, String what) {
        if (want.equals(got)) return 0;
        System.out.println("FAIL " + what + ": want [" + want + "] got [" + got + "]");
        return 1;
    }
}
