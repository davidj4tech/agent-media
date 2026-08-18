package net.agentmedia.companion;

/**
 * The second line of a channel's card.
 *
 * Every card in the shade used to say the same thing under its title:
 * {@code agent-media}. That is the app talking about itself on the one surface
 * where space is scarcest — three cards, three identical subtitles, and the
 * field that could have told you which is which spent on a name you already
 * know because you installed it.
 *
 * So each channel says the most useful true thing it has:
 *
 * <ul>
 *   <li><b>Music</b> — who it is by, which is what a person calls a track
 *       when the title is not enough. Failing that, where it is in the queue,
 *       because "3 of 12" is the other thing a listener wants.</li>
 *   <li><b>Speech</b> — how much Sam has waiting, which is the only question
 *       that card answers that nothing else does. The clip itself is named in
 *       the title.</li>
 *   <li><b>Book</b> — how much is left. Not elapsed: an audiobook listener is
 *       deciding whether to start another chapter, and elapsed does not answer
 *       that.</li>
 * </ul>
 *
 * An empty string is a legitimate answer everywhere here — a card with a title
 * and nothing under it reads as complete, where one saying "unknown" reads as
 * broken.
 *
 * android.*-free, so test/run.sh covers it.
 */
final class CardText {

    private CardText() { }

    /** The music card's second line. */
    static String music(String artist, int playlistPos, int playlistCount) {
        if (artist != null && !artist.trim().isEmpty()) return artist.trim();
        // playlist-pos is zero-based and -1 when nothing is loaded; a queue of
        // one is just "the track", and saying "1 of 1" about it is noise.
        if (playlistCount > 1 && playlistPos >= 0) {
            return "track " + (playlistPos + 1) + " of " + playlistCount;
        }
        return "";
    }

    /**
     * The speech card's second line: which conversation, and what is stacked up.
     *
     * The conversation leads, because the title now carries the words and the
     * question the words leave open is "who was that to". The pile follows only
     * when there is one.
     *
     * <h4>What a pile is, and what it is not</h4>
     *
     * The count is mpv's {@code playlist-count}, and mpv keeps the entry of a
     * clip that has finished — it parks the last one open rather than clearing
     * it. So a player sitting idle after a reply reports one, and this card
     * used to read that as "1 reply waiting" and say so for the rest of the
     * day. Nothing was waiting; the reply had been given hours ago.
     *
     * A pile is therefore only claimed when the player is holding one: mid-
     * reply with clips after this one, or paused with entries behind it, which
     * is what a hold looks like. Loaded-and-running-and-not-speaking is the
     * parked case, and it means nothing at all.
     */
    static String speech(int queued, boolean speaking, boolean loaded,
                         boolean paused, String conversation) {
        String pile = pile(queued, speaking, loaded, paused);
        String who = conversation == null ? "" : conversation.trim();
        if (who.isEmpty()) return pile;
        if (pile.isEmpty()) return who;
        return who + " · " + pile;
    }

    private static String pile(int queued, boolean speaking, boolean loaded,
                               boolean paused) {
        if (!loaded) return "";
        // Mid-reply the count includes the clip being spoken, so the honest
        // reading of one is "this one, nothing after".
        int waiting = speaking ? queued - 1 : queued;
        if (!speaking && !paused) return "";   // parked, not held
        if (waiting == 1) return "1 more waiting";
        if (waiting > 1) return waiting + " more waiting";
        return "";
    }

    /** The book card's second line. */
    static String book(long durationMs, long positionMs) {
        long left = durationMs - positionMs;
        if (durationMs <= 0 || left <= 0) return "";
        return duration(left) + " left";
    }

    /**
     * A span of time as a person would say it aloud.
     *
     * Two units at most, and never a unit that rounds to nothing: "1h 13m",
     * "13m", "44s". Books run to twenty hours and replies to eight seconds, so
     * this has to hold both ends without a format that suits neither.
     */
    static String duration(long ms) {
        long total = ms / 1000L;
        long hours = total / 3600L;
        long minutes = (total % 3600L) / 60L;
        long seconds = total % 60L;
        if (hours > 0) return minutes > 0 ? hours + "h " + minutes + "m" : hours + "h";
        if (minutes > 0) return minutes + "m";
        return seconds + "s";
    }
}
