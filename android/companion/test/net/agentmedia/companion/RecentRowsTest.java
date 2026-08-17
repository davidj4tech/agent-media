package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Calendar;
import java.util.List;

/**
 * Host-side tests for the shape of the history list.
 *
 * What is being protected is scannability: a day break where the day breaks, a
 * clock time you can read down a column, and a title that says what a thing was
 * rather than what it was stored as.
 */
public final class RecentRowsTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) {
        testHeadingsMarkTheDays();
        testARowWithNoTimeKeepsItsPlace();
        testHeadingWording();
        testClockIsTwentyFourHour();
        testTitleKeepsARealLabel();
        testTitleRescuesASpeechFile();
        testTitleRescuesABareUrl();
        testTitleRescuesQueryWreckage();
        testAnEmptyLabelFallsBackToTheUri();
        testUnplayableRowsSayWhy();
        testSpeechGroupsByConversation();
        testAConversationWithNoNameIsStillItsOwn();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    // 2026-08-17, 21:40 local — the evening this list was redesigned.
    private static long at(int day, int hour, int minute) {
        Calendar c = Calendar.getInstance();
        c.set(2026, Calendar.AUGUST, day, hour, minute, 0);
        c.set(Calendar.MILLISECOND, 0);
        return c.getTimeInMillis();
    }

    private static RecentList.Item item(String label, String channel, long ms) {
        return new RecentList.Item(label, channel, "", "https://x/y.mp3", "1h",
                                   ms / 1000.0);
    }

    private static void testHeadingsMarkTheDays() {
        long now = at(17, 21, 40);
        List<RecentList.Item> items = Arrays.asList(
                item("A", "book", at(17, 20, 10)),
                item("B", "music", at(17, 9, 5)),
                item("C", "music", at(16, 22, 0)),
                item("D", "book", at(11, 18, 30)));
        List<RecentRows.Entry> rows = RecentRows.group(items, now);

        check("a heading opens the list", rows.get(0).isHeading());
        check("today's two rows share one heading",
                rows.get(0).heading.equals("Today")
                        && !rows.get(1).isHeading() && !rows.get(2).isHeading());
        check("yesterday gets its own", rows.get(3).isHeading()
                && rows.get(3).heading.equals("Yesterday"));
        check("older gets a date", rows.get(5).isHeading()
                && rows.get(5).heading.startsWith("Tue 11 Aug"));
        check("every item is still there", countItems(rows) == 4);
        // A day is not something you open and close, so it carries no key.
        check("day breaks do not fold", rows.get(0).key == null);
    }

    private static void testARowWithNoTimeKeepsItsPlace() {
        // The store had no time for it. That is not a new day, and a 1970
        // section would be the list lying about it.
        long now = at(17, 21, 40);
        List<RecentList.Item> items = Arrays.asList(
                item("A", "book", at(17, 20, 10)),
                item("B", "speech", 0L));
        List<RecentRows.Entry> rows = RecentRows.group(items, now);
        check("one heading only", countHeadings(rows) == 1);
        check("the undated row is kept", countItems(rows) == 2);
        check("with no clock", rows.get(2).clock.isEmpty());
    }

    private static void testHeadingWording() {
        long now = at(17, 21, 40);
        check("this morning is today",
                "Today".equals(RecentRows.heading(at(17, 6, 0), now)));
        check("last night is yesterday",
                "Yesterday".equals(RecentRows.heading(at(16, 23, 50), now)));
        check("a week back is a date",
                RecentRows.heading(at(10, 12, 0), now).endsWith("Aug"));
    }

    private static void testClockIsTwentyFourHour() {
        check("evening", "21:58".equals(RecentRows.clock(at(17, 21, 58))));
        check("and morning pads", "06:05".equals(RecentRows.clock(at(17, 6, 5))));
    }

    private static void testTitleKeepsARealLabel() {
        // The far side's label wins: `media recent` prints the same string, and
        // one history rendered two ways drifts.
        check("a real title is left alone",
                "Talking Drupal #565".equals(
                        RecentRows.title(item("Talking Drupal #565", "book", 0))));
    }

    private static void testTitleRescuesASpeechFile() {
        check("a rendered clip says what it was",
                "a spoken reply".equals(RecentRows.title(
                        item("remote-20260814T190922-18480.mp3", "speech", 0))));
        check("a real title ending in .mp3 is not touched",
                "The Wind in the Willows.mp3".equals(RecentRows.title(
                        item("The Wind in the Willows.mp3", "book", 0))));
        // A real row from p8a, and the reason this is gated on the channel: a
        // podcast filename is a dated filename too.
        check("a dated podcast filename is left alone",
                "td565-video-2026-08-11-15-42-38.mp3".equals(RecentRows.title(
                        item("td565-video-2026-08-11-15-42-38.mp3", "book", 0))));
    }

    private static void testTitleRescuesQueryWreckage() {
        // Also a real row. mpv names a URL with no metadata by unquoting it and
        // taking the tail after the last slash, so `...content-type=audio%2Fmpeg`
        // became the stored title.
        String junk = "mpeg&Expires=1786861383&Signature=WlRhZjKWxkRAVg~EZSNZvSiV"
                + "Yq2xT9UCSJg4XNavw&Key-Pair-Id=K1YS7LZGUP96OI";
        RecentList.Item row = new RecentList.Item(junk, "book", "audiobook",
                "https://content.libsyn.com/p/4/8/7/48750/td565-video-2026-08-11.mp3"
                        + "?c_id=205007380&Expires=1786861383",
                "18h", 0);
        check("the uri gives a better name than the wreckage",
                "td565-video-2026-08-11".equals(RecentRows.title(row)));
        check("an ampersand in a real title is safe",
                "Simon & Garfunkel Live".equals(RecentRows.title(
                        item("Simon & Garfunkel Live", "music", 0))));
    }

    private static void testAnEmptyLabelFallsBackToTheUri() {
        RecentList.Item row = new RecentList.Item("", "music", "", 
                "https://example.com/sets/a-long-set.mp3", "2h", 0);
        check("no label, name it from the uri",
                "a-long-set".equals(RecentRows.title(row)));
    }

    private static void testTitleRescuesABareUrl() {
        // The filename where there is one — it identifies the item...
        check("a bare link is named by its file",
                "td565".equals(RecentRows.title(
                        item("https://traffic.libsyn.com/sac/td565.mp3?x=1",
                             "book", 0))));
        // ...and the host where there is not: "watch" says nothing at all.
        check("a generic path falls back to the host",
                "a link from youtube.com".equals(RecentRows.title(
                        item("https://www.youtube.com/watch?v=aaa", "music", 0))));
    }

    private static void testUnplayableRowsSayWhy() {
        RecentList.Item speech = new RecentList.Item(
                "remote-20260814T190922-18480.mp3", "speech", "", "", "2h", 0);
        check("a speech clip says it is gone",
                "gone — the clip was temporary".equals(RecentRows.subtitle(speech)));

        RecentList.Item playable = item("A Long Set", "music", at(17, 20, 0));
        check("a playable row keeps its own subtitle",
                RecentRows.subtitle(playable).equals(playable.subtitle()));
    }

    private static RecentList.Item clip(String text, long ms, String session,
                                        String window) {
        return new RecentList.Item(text, "speech", "", "", "1h", ms / 1000.0,
                                   1L, session, window);
    }

    private static void testSpeechGroupsByConversation() {
        // Two conversations interleaved clip for clip — the case that decides
        // this: broken at each change it would be six headings and no grouping.
        List<RecentList.Item> items = Arrays.asList(
                clip("newest", at(17, 21, 0), "aaa", "add C function"),
                clip("theirs", at(17, 20, 0), "bbb", "call guard roles"),
                clip("mine again", at(17, 19, 0), "aaa", "add C function"),
                clip("theirs again", at(17, 18, 0), "bbb", "call guard roles"));
        List<RecentRows.Entry> rows = RecentRows.byConversation(items);
        check("one heading per conversation", countHeadings(rows) == 2);
        check("the one heard last comes first",
                rows.get(0).heading.startsWith("add C function"));
        check("and it counts its clips",
                rows.get(0).heading.endsWith("· 2 clips"));
        check("its clips follow it, in order",
                rows.get(1).item.label.equals("newest")
                && rows.get(2).item.label.equals("mine again"));
        check("then the other conversation",
                rows.get(3).heading.startsWith("call guard roles"));
        // The rows keep the clock they would have had under day headings: a
        // conversation is a time bucket too, and you still scan down the times.
        check("rows still carry a clock", rows.get(1).clock.equals("21:00"));
        // A heading and its rows share a key, which is what lets the screen
        // fold a group away by matching rather than by counting rows.
        check("the group is keyed", rows.get(0).key != null
                && rows.get(0).key.equals(rows.get(1).key)
                && rows.get(0).key.equals(rows.get(2).key));
        check("and the other group is keyed differently",
                !rows.get(0).key.equals(rows.get(3).key));
    }

    private static void testAConversationWithNoNameIsStillItsOwn() {
        // Clips predating the window field: calling both "untagged" would merge
        // on screen what the grouping just took the trouble to keep apart.
        List<RecentList.Item> items = Arrays.asList(
                clip("one", at(17, 21, 0), "abcd1234", ""),
                clip("two", at(17, 20, 0), "wxyz9876", ""));
        List<RecentRows.Entry> rows = RecentRows.byConversation(items);
        check("still two groups", countHeadings(rows) == 2);
        check("named by a stub of the session id",
                rows.get(0).heading.startsWith("…1234")
                && rows.get(2).heading.startsWith("…9876"));

        // A spoken reminder from cron has no session at all, only the window it
        // was spoken from — and two machines' reminders are not one conversation.
        List<RecentRows.Entry> cron = RecentRows.byConversation(Arrays.asList(
                clip("moon enters Libra", at(17, 21, 0), "", "red5"),
                clip("describe digest", at(17, 20, 0), "", "agent-digest-pane")));
        check("no session falls back to the window", countHeadings(cron) == 2);
        check("each named for its own", cron.get(0).heading.startsWith("red5"));

        List<RecentRows.Entry> none = RecentRows.byConversation(
                Arrays.asList(clip("orphan", at(17, 21, 0), "", "")));
        check("and an untagged clip says so",
                none.get(0).heading.startsWith("untagged"));
        check("one clip is not 1 clips", none.get(0).heading.endsWith("· 1 clip"));
    }

    private static int countHeadings(List<RecentRows.Entry> rows) {
        int n = 0;
        for (RecentRows.Entry e : rows) if (e.isHeading()) n++;
        return n;
    }

    private static int countItems(List<RecentRows.Entry> rows) {
        int n = 0;
        for (RecentRows.Entry e : rows) if (!e.isHeading()) n++;
        return n;
    }

    private static void check(String what, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  ok   " + what);
        } else {
            failures.add(what);
            System.out.println("  FAIL " + what);
        }
    }
}
