package net.agentmedia.companion;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Handler;
import android.os.SystemClock;

import java.util.Map;

/**
 * A channel that gets its own card in the shade's media player, but is not the
 * phone's player: speech and book.
 *
 * <h4>Why a second and third session, when the spike said not to</h4>
 *
 * The spike learned that two sessions compete for the Bluetooth addressed-player
 * slot, and the transport fix in 3519172 depends on winning it. That finding
 * stands — what it did not say is that the competition is decided by the silent
 * {@code AudioTrack}. A session with no open stream does not get the slot, which
 * is the whole reason the stream of zeros exists. So the rule this class is
 * built on: <b>only the music session ever opens an AudioTrack.</b> These two
 * publish a card and drive their own mpv, and hold no stream, no media-button
 * receiver, and no audio focus.
 *
 * <h4>What it replaces</h4>
 *
 * The front-channel mechanism, which existed because one session had to describe
 * two channels and could only name one at a time. With a card each, every
 * channel names itself: the music card is a music card again, and Sam has his
 * own pause button rather than borrowing the music one. {@link FrontChannel}
 * survives for the question it is actually good at — whose focus loss is this —
 * which is a different question from what to put on a display.
 *
 * <h4>Visibility</h4>
 *
 * A card exists while {@link #publish} is told to show it. The service decides:
 * a book stays on screen while it is loaded, because that is a thing you resume
 * tomorrow, while a spoken clip's card follows the reply and goes when it does.
 * sink-speech parks the last clip open indefinitely, so "loaded" would leave
 * Sam on the shade all evening.
 */
final class SideChannel {

    /** Told after each property is applied, so the service can do focus work. */
    interface Watcher {
        void onChanged(String property, Object value, MpvState state);
    }

    private final Service ctx;
    private final Handler main;
    private final NotificationManager nm;
    private final String name;
    /** What the card is called when the mpv has nothing better to say. */
    private final String label;
    private final int notifId;
    private final String notifChannel;
    private final MpvState state = new MpvState();
    private final MpvIpc ipc;
    private final Watcher watcher;

    private MediaSession session;
    private boolean visible = false;
    /**
     * This card's own pause button was pressed, and nothing has resumed it
     * since. The card has to outlive the pause it caused or there is no button
     * left to undo it with — the mistake made on the music card at 08:54 on
     * 2026-08-15, when pausing Sam dropped the front channel and the play press
     * three seconds later went to an idle music mpv.
     */
    private boolean heldByUser = false;
    private boolean failed = false;
    /** Where this channel's mpv bridge is. The server's host; see Server. */
    private final String host;

    SideChannel(Service ctx, Handler main, String name, String label,
                String host, int port, String[] observed, int notifId,
                String notifChannel, Watcher watcher) {
        this.host = host;
        this.ctx = ctx;
        this.main = main;
        this.nm = ctx.getSystemService(NotificationManager.class);
        this.name = name;
        this.label = label;
        this.notifId = notifId;
        this.notifChannel = notifChannel;
        this.watcher = watcher;
        this.ipc = new MpvIpc(host, port, listener, observed);
    }

    MpvState state() { return state; }

    MpvIpc ipc() { return ipc; }

    String name() { return name; }

    void start() {
        session = new MediaSession(ctx, "agent-media " + name);
        session.setCallback(callback);
        // No setMediaButtonBroadcastReceiver on purpose: the earbud addresses
        // the phone's player, and that is music. These cards are driven by the
        // hand that can see them.
        ipc.start();
        CompanionService.log(name + ": ipc -> " + host + ":" + ipc.port());
    }

    void stop() {
        ipc.stop();
        publish(false);
        if (session != null) {
            session.setActive(false);
            session.release();
            session = null;
        }
    }

    /**
     * Show or hide this channel's card, and refresh it while shown.
     *
     * The session is deactivated as well as the notification cancelled: an
     * active session with no card is invisible in the shade but still a
     * candidate for the addressed-player slot, which is the one thing these
     * channels must never take from music.
     */
    void publish(boolean show) {
        if (session == null) return;
        try {
            publishOrThrow(show);
        } catch (Throwable e) {
            // A card is an ornament; the focus policy and the transport are the
            // app. Losing the whole process over a notification would trade a
            // missing card for a phone with no companion at all — and on this
            // device a crash is a dialog, a restart loop and no stack trace.
            // Say so in the log instead, which is readable over ssh.
            CompanionService.log(name + ": card failed, carrying on: " + e);
            failed = true;
        }
    }

    /**
     * A line the service wants on this card instead of the usual state word —
     * "3 waiting" while a voice session is holding the pile back. Cleared by
     * passing null. Quiet by design: the shade is where you glance, not where
     * you are interrupted.
     */
    private String note = null;

    void note(String text) {
        String v = (text == null || text.trim().isEmpty()) ? null : text.trim();
        if (!eq(v, note)) {
            note = v;
            lastCard = null;      // the card reads differently now
        }
    }

    private static boolean eq(String a, String b) {
        return (a == null) ? (b == null) : a.equals(b);
    }

    /** True once a card has thrown; surfaced in /state so it is not silent. */
    boolean failed() { return failed; }

    private void publishOrThrow(boolean show) {
        if (!show) {
            if (visible) {
                visible = false;
                lastCard = null;          // a card taken down is posted afresh
                session.setActive(false);
                nm.cancel(notifId);
            }
            return;
        }
        if (!visible) {
            visible = true;
            session.setActive(true);
        }

        session.setMetadata(new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, cardTitle())
                .putString(MediaMetadata.METADATA_KEY_ARTIST, subtitle())
                .putLong(MediaMetadata.METADATA_KEY_DURATION, state.durationMs())
                .putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, Artwork.art(name))
                .build());

        int playbackState;
        if (!state.connected) playbackState = PlaybackState.STATE_ERROR;
        else if (!state.loaded()) playbackState = PlaybackState.STATE_STOPPED;
        else if (state.paused) playbackState = PlaybackState.STATE_PAUSED;
        else playbackState = PlaybackState.STATE_PLAYING;

        session.setPlaybackState(new PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_PLAY
                        | PlaybackState.ACTION_PAUSE
                        | PlaybackState.ACTION_PLAY_PAUSE
                        | PlaybackState.ACTION_STOP)
                .setState(playbackState, state.positionMs(),
                          state.playing() ? (float) state.speed : 0f)
                .build());

        // Re-post only when the card would actually read differently.
        //
        // Not an optimisation: a card that outlives its clip is one the listener
        // can swipe away, and a notify with the same id brings a dismissed card
        // straight back. Every mpv property on any channel pushes session state,
        // so without this the swiped speech card would return the moment the
        // music position moved. Now it stays gone until the next clip changes
        // what the card says. (The marquee still ticks: the windowed title is
        // part of the signature, and a scrolling card is a playing one.)
        // The subtitle is in the signature because it is now information — a
        // book counting down and a queue growing behind Sam both change the
        // card without changing its title.
        String sig = cardTitle() + " " + subtitle() + " " + playbackState
                + " " + state.paused + " " + state.loaded();
        if (sig.equals(lastCard)) return;
        lastCard = sig;
        nm.notify(notifId, card());
    }

    /** What the last posted card said — see the signature check in publish. */
    private String lastCard = null;

    boolean visible() { return visible; }

    /** The title currently crawling, and when it started. See cardTitle(). */
    private String shownTitle = "";
    private long titleSince;

    /** Paused from this card, and owed a resume by the hand that did it. */
    boolean heldByUser() { return heldByUser; }

    /**
     * What the card says: mpv's {@code media-title}, or the channel's label.
     *
     * Deliberately not {@link MpvState#title()}, whose fallback is the
     * filename. For speech that filename is
     * {@code remote-20260814T190922-18480.mp3}, which is worse than saying
     * nothing — so the label ("Sam") stands in until the coordinator names the
     * reply with {@code force-media-title}, which it sets to the same string
     * the popup shows.
     */
    private String title() {
        String t = state.mediaTitle;
        // Then the clip that just played: a card that outlives its clip is only
        // worth having if it still names it. See MpvState#lastTitle.
        if (t == null || t.trim().isEmpty()) t = state.lastTitle();
        return (t == null || t.trim().isEmpty()) ? label : t.trim();
    }

    /** Has this channel played anything worth keeping on the card? */
    boolean remembers() { return state.lastTitle() != null; }

    /**
     * The title as the card should show it *this moment* — windowed if it is
     * too long, whole if it is not.
     *
     * Both the session metadata and the notification take this, and they have
     * to take the same thing: the shade renders from the session and the older
     * lock-screen path from the notification, and a marquee that ran in two
     * places at two offsets would read as a bug in whichever one you were
     * looking at.
     */
    private String cardTitle() {
        String full = title();
        if (!full.equals(shownTitle)) {
            // A new title starts its crawl from column zero rather than
            // arriving halfway scrolled.
            shownTitle = full;
            titleSince = SystemClock.uptimeMillis();
        }
        if (!scrolling()) return full;
        return Marquee.window(full, Marquee.WIDTH,
                              SystemClock.uptimeMillis() - titleSince);
    }

    /**
     * Whether this card is scrolling right now, which is also the question
     * "does it need republishing on the marquee tick?".
     *
     * Only while *playing*. A paused book sits at a fixed frame, which is both
     * the honest reading — nothing is happening, so nothing should move — and
     * what keeps this away from the notification churn already on the open
     * list. A book can be parked in the shade for days.
     */
    boolean scrolling() {
        return visible && state.playing() && Marquee.needed(title(), Marquee.WIDTH);
    }

    /**
     * The second line: what this channel has to say that its title does not.
     *
     * A note the service has set wins — "waiting for the conversation to
     * finish" is more urgent than anything computed here. Otherwise
     * {@link CardText} decides, and an empty answer is left empty rather than
     * padded: "paused" beside a play button was the card saying it twice.
     */
    private String subtitle() {
        if (note != null) return note;
        if ("speech".equals(name)) {
            return CardText.speech(state.queued, state.speaking);
        }
        if ("book".equals(name)) {
            return CardText.book(state.durationMs(), state.positionMs());
        }
        return "";
    }

    private Notification card() {
        PendingIntent open = PendingIntent.getActivity(ctx, 0,
                new Intent(ctx, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Builder(ctx, notifChannel)
                .setContentTitle(cardTitle())
                .setContentText(subtitle())
                // The channel's own mark, in both places the shade shows one:
                // a silhouette in the status bar and the tile on the card. Three
                // cards with the stock play triangle and no art were three cards
                // you had to read to tell apart.
                .setSmallIcon(Artwork.icon(name))
                .setLargeIcon(Artwork.art(name))
                .setStyle(new Notification.MediaStyle()
                        .setMediaSession(session.getSessionToken()))
                .setContentIntent(open)
                .setOngoing(state.playing())
                .build();
    }

    // ---- mpv -> card -----------------------------------------------------

    private final MpvIpc.Listener listener = new MpvIpc.Listener() {
        @Override public void onProperty(final String property, final Object value) {
            main.post(() -> {
                if (!state.apply(property, value)) return;
                CompanionService.log(name + " " + property + " = " + value);
                // Resumed by anyone at all — this card, the popup, the CLI, the
                // coordinator starting the next reply — and the hold is over.
                if ("pause".equals(property) && !state.paused) heldByUser = false;
                if ("idle-active".equals(property) && state.idleActive) heldByUser = false;
                if (watcher != null) watcher.onChanged(property, value, state);
            });
        }

        @Override public void onEvent(String event, Map<String, Object> message) { }

        @Override public void onConnected() {
            main.post(() -> {
                state.connected = true;
                if (watcher != null) watcher.onChanged(null, null, state);
            });
        }

        @Override public void onDisconnected(String why) {
            main.post(() -> {
                state.connected = false;
                if (watcher != null) watcher.onChanged(null, null, state);
            });
        }

        @Override public void onLog(String line) {
            CompanionService.log(name + ": " + line);
        }
    };

    // ---- card -> mpv -----------------------------------------------------

    /**
     * Each card drives its own mpv, which is the simplification the third
     * session buys. The music card used to route play/pause to whichever
     * channel was in front — a necessary trick while one card described two
     * things, and one that could not survive a listener wanting to pause the
     * music *while* Sam talked.
     */
    private final MediaSession.Callback callback = new MediaSession.Callback() {
        @Override public void onPlay() {
            CompanionService.log("transport: play -> " + name);
            if (!state.loaded()) {
                // Nothing to un-pause. Clearing `pause` on an mpv with no file
                // open is a control that does nothing at all, and on speech
                // that is most of the time — sink-speech is idle between
                // replies, so ▶ on Sam's card was a button you could press
                // forever. The listener knows what play means with nothing
                // loaded, because the popup's Space has always known: replay
                // the last thing said. Ask it.
                replayLastTurn();
                return;
            }
            ipc.setProperty("pause", Boolean.FALSE);
        }

        @Override public void onPause() {
            CompanionService.log("transport: pause -> " + name);
            heldByUser = true;
            ipc.setProperty("pause", Boolean.TRUE);
        }

        @Override public void onStop() {
            CompanionService.log("transport: stop -> " + name);
            ipc.command("stop");
        }
    };

    /**
     * Hand an empty ▶ to the listener, which decides what it meant.
     *
     * `toggle` rather than a replay verb, because the decision belongs on the
     * far side and it already makes it: with a reply in flight it pauses, with
     * nothing loaded it plays the last turn back. Only for speech — a book with
     * nothing open has nothing to replay, and the card is not shown then anyway.
     *
     * Off the session thread: this is a network round trip, and a transport
     * callback that blocks is a shade that freezes under the finger.
     */
    private void replayLastTurn() {
        if (!"speech".equals(name)) return;
        new Thread(new Runnable() {
            @Override public void run() {
                String problem = Channels.control(
                        Settings.server(ctx), "speech", "toggle", "");
                if (!problem.isEmpty()) {
                    CompanionService.log("transport: play -> speech refused: "
                                         + problem);
                }
            }
        }, "speech-play").start();
    }
}
