package net.agentmedia.companion;

/**
 * The app's colours, sizes and spacing, in one place.
 *
 * There is no theme here to hang these on. No AndroidX, no layout XML, no
 * styles.xml — the whole app is hand-built views against platform APIs, because
 * red5 sits near 90% disk and a Gradle/AGP toolchain costs several GB against a
 * few hundred MB for this one. That is a good trade and it is not up for
 * revisiting; the cost of it is that every screen was picking its own greys, so
 * three surfaces built a fortnight apart agreed about nothing. This file is
 * what a theme would have been.
 *
 * <h4>The channel colours mean one thing each</h4>
 *
 * Music, speech and book each get a hue, and it identifies that channel
 * wherever it appears — the artwork on its card, the dot in the history, the
 * edge of the tile that is currently driving. Nothing else in the app is
 * allowed to be that saturated, so colour never has to be read twice: if it is
 * warm it is the music, and if it is nothing in particular it is chrome.
 *
 * They are pitched for a dark ground and only a dark ground. This is a phone
 * looked at in a dark room more often than not, and a light theme is a second
 * design to maintain for a case that barely happens.
 *
 * <h4>0xAARRGGBB literals rather than Color.parseColor</h4>
 *
 * So this class imports nothing from {@code android.*} and test/run.sh can
 * compile it on the build host with everything else that decides something.
 */
final class Style {

    private Style() { }

    // ---- channel hues ----------------------------------------------------
    //
    // The bright one is for marks, text and edges on the dark ground; the deep
    // one is the far end of the artwork gradient. Same hue, two stops.

    static final int MUSIC = 0xFFE08060;
    static final int MUSIC_DEEP = 0xFFB24F36;
    static final int SPEECH = 0xFF6FB2C2;
    static final int SPEECH_DEEP = 0xFF2F6C7D;
    static final int BOOK = 0xFFA292D8;
    static final int BOOK_DEEP = 0xFF5E4E93;

    // ---- ground and ink --------------------------------------------------
    //
    // Not neutral greys: every one is pulled a little towards blue, which is
    // what keeps the three hues above reading as deliberate against them
    // rather than as colour landing on a default.

    /** Behind everything. */
    static final int GROUND = 0xFF0E1013;
    /** Cards, rows, tiles — one step up from the ground. */
    static final int SURFACE = 0xFF171A1F;
    /** One step down: the log, and anything inset. */
    static final int SUNKEN = 0xFF08090B;
    /** Titles and anything being read. */
    static final int INK = 0xFFE6E9ED;
    /** Second lines, units, subtitles. */
    static final int MUTED = 0xFF98A2AD;
    /** Labels, timestamps, things present but not being read. */
    static final int FAINT = 0xFF6B747F;
    /** Hairlines between rows. */
    static final int RULE = 0xFF262B32;

    /** Nothing to say. */
    static final int OK = 0xFF7FB77E;
    /** Something to say, and it is not urgent enough to be red. */
    static final int WARN = 0xFFD79B57;

    // ---- type ------------------------------------------------------------
    //
    // Four sizes, in sp. A fifth would be a decision nobody could repeat.

    /** A screen's own name, and the title of whatever is driving. */
    static final int TITLE = 22;
    /** Card titles, row titles — the thing being named. */
    static final int HEAD = 16;
    /** Second lines. */
    static final int BODY = 13;
    /** Labels, chips, timestamps. */
    static final int LABEL = 11;

    // ---- spacing ---------------------------------------------------------

    /** The grid everything sits on. Multiples of this, nothing between. */
    static final int UNIT = 4;
    /** The smallest thing a thumb can be asked to hit, in dp. */
    static final int TOUCH = 48;

    /** {@code n} grid units in dp. */
    static int gap(int n) { return UNIT * n; }

    /** The bright hue for a channel name, or {@link #MUTED} for anything else. */
    static int accent(String channel) {
        if ("music".equals(channel)) return MUSIC;
        if ("speech".equals(channel)) return SPEECH;
        if ("book".equals(channel)) return BOOK;
        return MUTED;
    }

    /** The far end of a channel's artwork gradient. */
    static int deep(String channel) {
        if ("music".equals(channel)) return MUSIC_DEEP;
        if ("speech".equals(channel)) return SPEECH_DEEP;
        if ("book".equals(channel)) return BOOK_DEEP;
        return SURFACE;
    }
}
