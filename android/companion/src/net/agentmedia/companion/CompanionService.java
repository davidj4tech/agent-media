package net.agentmedia.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * The whole app: publish one MediaSession for the music channel, and drive the
 * Termux mpv underneath it over loopback TCP.
 *
 * It plays nothing. The only audio it opens is a stream of zeros, and that
 * exists solely because Android will not hand the Bluetooth addressed-player
 * slot to a session with no open stream — proven over three spike runs, see
 * docs/proposals/2026-08-13-mediasession-spike.md. Everything audible still
 * comes from mpv in Termux.
 */
public class CompanionService extends Service {

    /**
     * mpv's IPC socket is inside com.termux's private sandbox and cannot be
     * opened from here; a socat listener on loopback is the only route. This is
     * the *second* listener — the tailnet one that agent-media on red5 uses is
     * a separate service and is not disturbed.
     */
    static final String MPV_HOST = "127.0.0.1";
    static final int MPV_PORT = 6601;

    /**
     * The focus policy is allowed to touch mpv only when this is on. Off by
     * default, which makes a fresh install a *probe*: it takes focus and logs
     * every callback but changes nothing, so the first sideload can answer what
     * Android actually delivers — and whether taking focus disturbs the
     * pulseaudio stream underneath — before any behaviour change can muddy it.
     * One APK does both jobs; every install here is a sideload and a tap
     * through a chooser, so a build-time flag would cost a round trip.
     */
    private static final String PREFS = "companion";
    private static final String KEY_FOCUS_ACTS = "focus_acts";

    private static final String CHANNEL = "agent-media";
    private static final int NOTIF_ID = 1;
    /** How often to refresh position while playing. The session extrapolates between. */
    private static final long POSITION_POLL_MS = 5000;
    private static final int LOG_LINES = 200;

    private static final List<String> EVENTS = new ArrayList<String>();

    private final Handler main = new Handler(Looper.getMainLooper());
    private final MpvState state = new MpvState();
    private final FocusPolicy focus = new FocusPolicy();
    private MpvIpc ipc;
    private MediaSession session;
    private NotificationManager nm;
    private Silence silence;
    private FocusControl focusControl;
    private SharedPreferences prefs;
    private boolean focusActs = false;

    // ---- on-screen log (adb cannot reach this phone) ---------------------

    static void log(String line) {
        String stamp = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        synchronized (EVENTS) {
            EVENTS.add(stamp + "  " + line);
            while (EVENTS.size() > LOG_LINES) EVENTS.remove(0);
        }
    }

    static String dump() {
        synchronized (EVENTS) {
            if (EVENTS.isEmpty()) return "(no events yet)";
            StringBuilder sb = new StringBuilder();
            for (int i = EVENTS.size() - 1; i >= 0; i--) sb.append(EVENTS.get(i)).append('\n');
            return sb.toString();
        }
    }

    String status() {
        return state
                + "\nfocus=" + (focusControl != null && focusControl.held() ? "held" : "none")
                + " mode=" + (focusActs ? "acting" : "probe (logs only)")
                + (focus.owesResume() ? " owes:resume" : "")
                + (focus.owesUnduck() ? " owes:unduck" : "");
    }

    boolean focusActs() {
        return focusActs;
    }

    /** Flip between the probe and the acting build. Survives a restart. */
    void setFocusActs(boolean acts) {
        if (acts == focusActs) return;
        focusActs = acts;
        prefs.edit().putBoolean(KEY_FOCUS_ACTS, acts).apply();
        // Whatever the old mode owed does not carry across the switch.
        focus.reset();
        log("focus: mode -> " + (acts ? "acting" : "probe (logs only)"));
    }

    // ---- lifecycle -------------------------------------------------------

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        focusActs = prefs.getBoolean(KEY_FOCUS_ACTS, false);
        focusControl = new FocusControl(this, main, this::onFocusChange);

        nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(
                CHANNEL, "agent-media", NotificationManager.IMPORTANCE_LOW));

        session = new MediaSession(this, "agent-media music");
        session.setCallback(callback);
        session.setMediaButtonBroadcastReceiver(
                new ComponentName(this, MediaButtonReceiver.class));
        session.setActive(true);

        // Foreground first, before anything that can block: Android kills a
        // service that takes too long to post its notification.
        startForeground(NOTIF_ID, buildNotification(),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        pushSessionState();

        ipc = new MpvIpc(MPV_HOST, MPV_PORT, listener);
        ipc.start();
        main.postDelayed(positionPoll, POSITION_POLL_MS);
        log("service started; mpv ipc -> " + MPV_HOST + ":" + MPV_PORT);
        log("focus: mode " + (focusActs ? "acting" : "probe (logs only)"));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        main.removeCallbacks(positionPoll);
        if (ipc != null) ipc.stop();
        if (focusControl != null) focusControl.abandon();
        stopSilence();
        if (session != null) {
            session.setActive(false);
            session.release();
        }
        super.onDestroy();
    }

    // ---- mpv -> session --------------------------------------------------

    private final MpvIpc.Listener listener = new MpvIpc.Listener() {
        @Override public void onProperty(final String name, final Object value) {
            main.post(() -> {
                if (state.apply(name, value)) {
                    log("mpv " + name + " = " + value);
                    // The two guards the policy needs from the outside world:
                    // a resume from anywhere else cancels a resume we owe, and
                    // a volume we did not write means something else owns it
                    // now (call_guard during a call is the live example).
                    switch (name) {
                        case "pause":       focus.onPauseChanged(state.paused); break;
                        case "volume":      focus.onVolumeChanged(state.volume); break;
                        case "idle-active": if (state.idleActive) focus.reset(); break;
                        default: break;
                    }
                    pushSessionState();
                }
            });
        }

        @Override public void onEvent(String event, Map<String, Object> message) {
            // Property observers carry everything the session needs; events are
            // logged only so the on-screen readout can show mpv is alive.
            if ("end-file".equals(event) || "start-file".equals(event)) log("mpv event " + event);
        }

        @Override public void onConnected() {
            main.post(() -> { state.connected = true; pushSessionState(); });
        }

        @Override public void onDisconnected(String why) {
            main.post(() -> { state.connected = false; pushSessionState(); });
        }

        @Override public void onLog(String line) {
            log(line);
        }
    };

    /** Ask mpv where it is, then refresh the session's position. */
    private final Runnable positionPoll = new Runnable() {
        @Override public void run() {
            if (ipc != null && ipc.isConnected() && state.playing()) {
                ipc.getProperty(MpvIpc.POSITION_PROPERTY).whenComplete((v, e) -> {
                    if (e != null) return;
                    main.post(() -> {
                        state.apply(MpvIpc.POSITION_PROPERTY, v);
                        pushSessionState();
                    });
                });
            }
            main.postDelayed(this, POSITION_POLL_MS);
        }
    };

    /** Re-publish metadata, playback state and the notification from {@link #state}. */
    private void pushSessionState() {
        // The silent track follows *loaded*, not *playing*. Dropping it while
        // paused would surrender the addressed-player slot, and the press that
        // would win it back is exactly the play button we would no longer
        // receive. It stops when mpv goes idle, which is the case that would
        // otherwise cost battery all day.
        if (state.loaded()) startSilence(); else stopSilence();

        // Focus follows the same predicate, and for a related reason:
        // abandoning it on our own pause would forfeit the GAIN that is
        // supposed to tell us to resume.
        if (state.loaded()) {
            if (!focusControl.held() && focusControl.request()) log("focus: granted");
        } else if (focusControl.held()) {
            focusControl.abandon();
            focus.reset();
            log("focus: abandoned (mpv idle)");
        }

        MediaMetadata.Builder md = new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, state.title())
                .putString(MediaMetadata.METADATA_KEY_ARTIST, "agent-media")
                .putLong(MediaMetadata.METADATA_KEY_DURATION, state.durationMs());
        session.setMetadata(md.build());

        int playbackState;
        if (!state.connected) playbackState = PlaybackState.STATE_ERROR;
        else if (!state.loaded()) playbackState = PlaybackState.STATE_STOPPED;
        else if (state.paused) playbackState = PlaybackState.STATE_PAUSED;
        else playbackState = PlaybackState.STATE_PLAYING;

        PlaybackState.Builder pb = new PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_PLAY
                        | PlaybackState.ACTION_PAUSE
                        | PlaybackState.ACTION_PLAY_PAUSE
                        | PlaybackState.ACTION_STOP
                        | PlaybackState.ACTION_SKIP_TO_NEXT
                        | PlaybackState.ACTION_SKIP_TO_PREVIOUS
                        | PlaybackState.ACTION_SEEK_TO)
                .setState(playbackState, state.positionMs(),
                          state.playing() ? (float) state.speed : 0f);
        if (!state.connected) {
            // The (code, message) overload is AndroidX-only; the platform
            // Builder takes a bare CharSequence.
            pb.setErrorMessage("mpv unreachable");
        }
        session.setPlaybackState(pb.build());

        nm.notify(NOTIF_ID, buildNotification());
    }

    private Notification buildNotification() {
        PendingIntent open = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        String text;
        if (!state.connected) text = "mpv unreachable on " + MPV_HOST + ":" + MPV_PORT;
        else if (!state.loaded()) text = "idle";
        else text = state.paused ? "paused" : "playing";

        return new Notification.Builder(this, CHANNEL)
                .setContentTitle(state.loaded() ? state.title() : "agent-media")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setStyle(new Notification.MediaStyle().setMediaSession(session.getSessionToken()))
                .setContentIntent(open)
                .setOngoing(true)
                .build();
    }

    // ---- session -> mpv --------------------------------------------------

    private final MediaSession.Callback callback = new MediaSession.Callback() {
        @Override public void onPlay() {
            log("transport: play");
            ipc.setProperty("pause", Boolean.FALSE);
        }

        @Override public void onPause() {
            log("transport: pause");
            ipc.setProperty("pause", Boolean.TRUE);
        }

        @Override public void onStop() {
            log("transport: stop");
            ipc.command("stop");
        }

        @Override public void onSkipToNext() {
            log("transport: next");
            ipc.command("playlist-next", "weak");
        }

        @Override public void onSkipToPrevious() {
            log("transport: previous");
            ipc.command("playlist-prev", "weak");
        }

        @Override public void onSeekTo(long positionMs) {
            log("transport: seek " + positionMs);
            ipc.setProperty("time-pos", positionMs / 1000.0);
        }
    };

    // ---- focus -> mpv ----------------------------------------------------

    /**
     * Android has moved audio focus. Delivered on the main looper (see
     * FocusControl), so it is safe to touch state here; the IPC writes go to
     * MpvIpc's sender thread.
     */
    private void onFocusChange(int change) {
        log("focus: " + FocusControl.name(change) + " [" + state + "]");
        if (!focusActs) {
            log("focus: probe mode, no action");
            return;
        }
        for (FocusPolicy.Action action : focus.onFocusChange(change, state)) {
            perform(action);
        }
    }

    private void perform(FocusPolicy.Action action) {
        switch (action) {
            case PAUSE:
                log("focus: pause");
                ipc.setProperty("pause", Boolean.TRUE);
                break;
            case RESUME:
                log("focus: resume");
                ipc.setProperty("pause", Boolean.FALSE);
                break;
            case DUCK:
                log("focus: duck -> " + FocusPolicy.DUCK_VOLUME);
                ipc.setProperty("volume", Double.valueOf(FocusPolicy.DUCK_VOLUME));
                break;
            case UNDUCK:
                double restore = focus.volumeToRestore();
                log("focus: unduck -> " + (int) restore);
                ipc.setProperty("volume", Double.valueOf(restore));
                break;
            default:
                break;
        }
    }

    // ---- the stream of zeros --------------------------------------------

    private void startSilence() {
        if (silence != null) return;
        silence = new Silence();
        silence.start();
        log("silent track started");
    }

    private void stopSilence() {
        if (silence == null) return;
        silence.stop();
        silence = null;
        log("silent track stopped");
    }

    /**
     * A full-volume all-zero PCM stream. Full volume on purpose: the buffer is
     * silent either way, and a zero-*volume* track risks being optimised out of
     * the mix — which would quietly cost us the controls.
     */
    private static final class Silence {
        private AudioTrack track;
        private Thread writer;
        private volatile boolean running = false;

        void start() {
            int rate = 44100;
            int min = AudioTrack.getMinBufferSize(rate,
                    AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
            final int buf = Math.max(min, 4096);

            track = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                            .build())
                    .setAudioFormat(new AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(rate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .build())
                    .setBufferSizeInBytes(buf)
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build();
            track.play();
            running = true;

            final AudioTrack t = track;
            writer = new Thread(() -> {
                short[] zeros = new short[buf / 2];
                while (running) {
                    // The blocking write paces this loop at real time.
                    if (t.write(zeros, 0, zeros.length) < 0) break;
                }
            }, "silence");
            writer.setDaemon(true);
            writer.start();
        }

        void stop() {
            running = false;
            if (writer != null) {
                try { writer.join(500); } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                writer = null;
            }
            if (track != null) {
                try { track.stop(); } catch (IllegalStateException ignored) { }
                track.release();
                track = null;
            }
        }
    }

    // ---- binding ---------------------------------------------------------

    public class LocalBinder extends android.os.Binder {
        CompanionService service() { return CompanionService.this; }
    }

    private final IBinder binder = new LocalBinder();

    @Override
    public IBinder onBind(Intent intent) {
        return binder;
    }
}
