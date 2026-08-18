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
            + "\"pos_ms\":4000,\"dur_ms\":21000,\"muted_elsewhere\":2},"
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
        testTheCardSaysWhichConversation();
        testChaptersOnlyForLiveMusic();
        testControlBody();
        testChapterLabels();
        testTheButtonAsksAboutDirection();
        testAChannelPublishesItsVerbs();

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
        check("mutes elsewhere are surfaced, and say where",
                m.get("speech").detail().contains("muted in 2 places"));
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

    private static void testTheCardSaysWhichConversation() {
        // The shade's card has said this on its second line since the words
        // moved to the title; the app's card showed the words alone, so the two
        // surfaces had half the fact each.
        String payload = "{\"channels\":{\"speech\":{\"idle\":false,"
                + "\"title\":\"a reply\","
                + "\"conversation\":\"add C function\"}}}";
        Channels.Channel c = Channels.parse(payload).get("speech");
        check("the conversation parses", "add C function".equals(c.conversation));
        check("and leads the second line",
                c.detail().startsWith("add C function"));

        // Settings still follow it, in the order they were in.
        String withMore = "{\"channels\":{\"speech\":{\"idle\":false,"
                + "\"conversation\":\"add C function\",\"speed\":1.5,"
                + "\"volume\":150}}}";
        check("then the settings",
                Channels.parse(withMore).get("speech").detail()
                        .equals("add C function  ·  1.5×  ·  vol 150"));

        // A channel that has none — every channel but speech, and speech
        // before anything has been said — is unchanged.
        check("no conversation, no line",
                Channels.parse("{\"channels\":{\"music\":{\"idle\":false}}}")
                        .get("music").detail().isEmpty());
        check("and it is null rather than empty",
                Channels.parse("{\"channels\":{\"music\":{}}}")
                        .get("music").conversation == null);
    }

    private static void testChaptersOnlyForLiveMusic() {
        Map<String, Channels.Channel> m = Channels.parse(FULL);
        check("live music may have chapters", m.get("music").mayHaveChapters());
        check("an idle channel may not", !m.get("book").mayHaveChapters());
        // Speech's picker is its clips, and idle is when you most want them —
        // the idle test would be exactly backwards on that channel.
        check("speech always has clips to offer",
                m.get("speech").mayHaveChapters());
    }

    private static void testTheButtonAsksAboutDirection() {
        // The bug this exists for: on the speech channel `playing` means "a
        // clip is being spoken", and sink-speech parks a finished clip open and
        // unpaused — so the transport button, which asked `playing`, showed ▶
        // through both pause and resume.
        Channels.Channel parked = new Channels.Channel("speech", false, false,
                false, "a reply", null, Long.valueOf(76065), Long.valueOf(90360),
                Double.valueOf(1.0), null, false, 0);
        check("loaded and unheld means the next press pauses", parked.advancing());

        Channels.Channel held = new Channels.Channel("speech", false, false,
                true, "a reply", null, Long.valueOf(76619), Long.valueOf(90360),
                Double.valueOf(1.0), null, false, 0);
        check("paused means the next press plays", !held.advancing());

        Channels.Channel idle = new Channels.Channel("book", true, false, false,
                null, null, null, null, null, null, false, 0);
        check("nothing loaded means the next press plays", !idle.advancing());
    }

    private static void testAChannelPublishesItsVerbs() {
        String payload = "{\"channels\":{\"book\":{\"idle\":false,"
                + "\"verbs\":[\"toggle\",\"seek\",\"speed\"]}}}";
        Channels.Channel book = Channels.parse(payload).get("book");
        check("a published verb is taken", book.takes("seek"));
        check("an absent one is not", !book.takes("mute"));

        // An older listener says nothing, and nothing means "do not second-guess
        // me" — the app drew every button before this existed and must not stop.
        Channels.Channel quiet = Channels.parse("{\"channels\":{\"book\":{}}}")
                .get("book");
        check("no list means every button stays", quiet.takes("mute"));
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
        // A chapter is picked by its number...
        check("a chapter is its number", rows.get(1).ref().equals("2"));
        // ...a speech clip by the history id the listener sent, because the
        // clip list is newest-first and renumbers under the finger.
        List<Chapters.Chapter> clips = Chapters.parse(
                "{\"rows\":[{\"number\":1,\"title\":\"14:02  a reply\","
                + "\"current\":true,\"ref\":\"91\"}]}");
        check("a clip is its ref", clips.get(0).ref().equals("91"));
        check("and still reads as a row", clips.get(0).label().startsWith("▸ 1"));
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
