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
    private final int icon;
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

    SideChannel(Service ctx, Handler main, String name, String label,
                int port, String[] observed, int notifId, String notifChannel,
                int icon, Watcher watcher) {
        this.ctx = ctx;
        this.main = main;
        this.nm = ctx.getSystemService(NotificationManager.class);
        this.name = name;
        this.label = label;
        this.notifId = notifId;
        this.notifChannel = notifChannel;
        this.icon = icon;
        this.watcher = watcher;
        this.ipc = new MpvIpc(CompanionService.MPV_HOST, port, listener, observed);
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
        CompanionService.log(name + ": ipc -> " + CompanionService.MPV_HOST + ":" + ipc.port());
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

    /** True once a card has thrown; surfaced in /state so it is not silent. */
    boolean failed() { return failed; }

    private void publishOrThrow(boolean show) {
        if (!show) {
            if (visible) {
                visible = false;
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
                .putString(MediaMetadata.METADATA_KEY_TITLE, title())
                .putString(MediaMetadata.METADATA_KEY_ARTIST, FrontChannel.DEFAULT_SUBTITLE)
                .putLong(MediaMetadata.METADATA_KEY_DURATION, state.durationMs())
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

        nm.notify(notifId, card());
    }

    boolean visible() { return visible; }

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
        return (t == null || t.trim().isEmpty()) ? label : t.trim();
    }

    private Notification card() {
        PendingIntent open = PendingIntent.getActivity(ctx, 0,
                new Intent(ctx, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Builder(ctx, notifChannel)
                .setContentTitle(title())
                .setContentText(state.paused ? "paused" : "playing")
                .setSmallIcon(icon)
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
}
