package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Host-side tests for the control screen's state and rendering.
 *
 * The screen is a window onto three channels that can each be missing, idle,
 * paused, or playing something with no length. Every one of those has to draw
 * *something* — a blank panel reads as a broken app, and on p8a the only way to
 * find that out otherwise is a sideload and a squint at the phone.
 */
public final class ChannelsTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    private static final String FULL =
            "{\"ok\":true,\"channels\":{"
            + "\"speech\":{\"channel\":\"speech\",\"idle\":false,\"playing\":true,"
            + "\"paused\":false,\"title\":\"Yes — and there's a catch\","
            + "\"pos_ms\":4000,\"dur_ms\":21000,\"muted_panes\":2},"
            + "\"music\":{\"channel\":\"music\",\"idle\":false,\"playing\":true,"
            + "\"paused\":false,\"title\":\"A Long Set\",\"chapter\":\"Second Movement\","
            + "\"pos_ms\":724000,\"dur_ms\":3511000,\"speed\":1.25,\"volume\":130},"
            + "\"book\":{\"channel\":\"book\",\"idle\":true,\"playing\":false}"
            + "}}";

    public static void main(String[] args) {
        testParsesEachChannel();
        testHeadingIsNeverEmpty();
        testClock();
        testProgress();
        testSpeedLabel();
        testDetailLine();
        testMissingChannelsAreIdle();
        testRubbishIsThreeIdleChannels();
        testChaptersOnlyForLiveMusic();
        testControlBody();
        testChapterLabels();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testParsesEachChannel() {
        Map<String, Channels.Channel> m = Channels.parse(FULL);
        check("all three present", m.size() == 3);
        check("speech title", m.get("speech").title.contains("catch"));
        check("music chapter", "Second Movement".equals(m.get("music").chapter));
        check("music volume", m.get("music").volume.intValue() == 130);
        check("book is idle", m.get("book").idle);
    }

    private static void testHeadingIsNeverEmpty() {
        Map<String, Channels.Channel> m = Channels.parse(FULL);
        check("an idle channel says so", m.get("book").heading().equals("nothing playing"));
        check("a titled one shows its title",
                m.get("music").heading().equals("A Long Set"));
        // Loaded but untitled: the worst case, and it still has to say something.
        Channels.Channel bare = Channels.parse(
                "{\"channels\":{\"music\":{\"idle\":false}}}").get("music");
        check("untitled but loaded", bare.heading().equals("(untitled)"));
    }

    private static void testClock() {
        Map<String, Channels.Channel> m = Channels.parse(FULL);
        check("under an hour is m:ss", m.get("speech").clock().equals("0:04 / 0:21"));
        check("past an hour is h:mm:ss",
                m.get("music").clock().equals("12:04 / 58:31"));
        check("nothing playing has no clock", m.get("book").clock().isEmpty());
        // A live stream has a position and no length.
        Channels.Channel live = Channels.parse(
                "{\"channels\":{\"music\":{\"idle\":false,\"pos_ms\":65000}}}").get("music");
        check("a stream shows just the position", live.clock().equals("1:05"));
    }

    private static void testProgress() {
        Map<String, Channels.Channel> m = Channels.parse(FULL);
        float f = m.get("music").progress();
        check("progress is a fraction", f > 0.20f && f < 0.21f);
        check("no length means no bar", m.get("book").progress() < 0);
        // A position past the end (a rounding artefact mid-track change) must
        // not paint outside the bar.
        Channels.Channel over = Channels.parse(
                "{\"channels\":{\"music\":{\"pos_ms\":99,\"dur_ms\":10}}}").get("music");
        check("progress is clamped", over.progress() == 1.0f);
    }

    private static void testSpeedLabel() {
        Map<String, Channels.Channel> m = Channels.parse(FULL);
        check("a changed speed is shown", m.get("music").speedLabel().equals("1.25×"));
        check("normal speed is not", m.get("speech").speedLabel().isEmpty());
        Channels.Channel one = Channels.parse(
                "{\"channels\":{\"book\":{\"speed\":1.0}}}").get("book");
        check("exactly 1x is silent", one.speedLabel().isEmpty());
        Channels.Channel half = Channels.parse(
                "{\"channels\":{\"book\":{\"speed\":1.5}}}").get("book");
        check("1.5 does not render as 1.50", half.speedLabel().equals("1.5×"));
    }

    private static void testDetailLine() {
        Map<String, Channels.Channel> m = Channels.parse(FULL);
        String music = m.get("music").detail();
        check("detail carries chapter, speed and volume",
                music.contains("Second Movement"), music.contains("1.25×"),
                music.contains("vol 130"));
        check("muted panes are surfaced",
                m.get("speech").detail().contains("2 muted"));
        check("an idle channel has nothing to add", m.get("book").detail().isEmpty());
    }

    private static void testMissingChannelsAreIdle() {
        // The listener could answer with two channels if one blew up entirely.
        Map<String, Channels.Channel> m =
                Channels.parse("{\"channels\":{\"music\":{\"idle\":false}}}");
        check("still three panels", m.size() == 3);
        check("the absent one is idle", m.get("speech").idle && m.get("book").idle);
    }

    private static void testRubbishIsThreeIdleChannels() {
        for (String junk : new String[]{"", "<html>", "{}", "{\"channels\":5}"}) {
            Map<String, Channels.Channel> m = Channels.parse(junk);
            check("junk still draws three panels (" + junk + ")", m.size() == 3);
        }
    }

    private static void testChaptersOnlyForLiveMusic() {
        Map<String, Channels.Channel> m = Channels.parse(FULL);
        check("live music may have chapters", m.get("music").mayHaveChapters());
        check("an idle channel may not", !m.get("book").mayHaveChapters());
        check("speech never does", !m.get("speech").mayHaveChapters());
    }

    private static void testControlBody() {
        String withArg = Channels.controlBody("music", "seek", "+30");
        check("body carries channel and action",
                withArg.contains("music"), withArg.contains("seek"));
        check("and the argument", withArg.contains("+30"));
        // A verb with no argument must not send an empty one: the listener
        // refuses a value-taking verb with nothing in it, and a stray "" would
        // turn a working press into a 422.
        check("no argument key when there is no argument",
                !Channels.controlBody("music", "toggle", "").contains("arg"));
    }

    private static void testChapterLabels() {
        List<Chapters.Chapter> rows = Chapters.parse(
                "{\"rows\":[{\"number\":1,\"title\":\"Intro\",\"start_ms\":0,"
                + "\"current\":false},{\"number\":2,\"title\":\"Second\","
                + "\"start_ms\":252000,\"current\":true}]}");
        check("both chapters parse", rows.size() == 2);
        check("the current one is marked", rows.get(1).label().startsWith("▸ 2"));
        check("the others are not", rows.get(0).label().startsWith("  1"));
        check("the start time is shown", rows.get(1).label().contains("4:12"));
        check("junk is no chapters", Chapters.parse("nope").isEmpty());
        // An untitled chapter is still tappable.
        check("untitled chapters get a name",
                Chapters.parse("{\"rows\":[{\"number\":3}]}").get(0)
                        .label().contains("Chapter 3"));
    }

    private static void check(String what, boolean... oks) {
        boolean ok = true;
        for (boolean b : oks) ok = ok && b;
        if (ok) {
            passed++;
            System.out.println("  ok   " + what);
        } else {
            failures.add(what);
            System.out.println("  FAIL " + what);
        }
    }
}
