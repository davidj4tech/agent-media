package net.agentmedia.sessionspike;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.IBinder;
import android.view.KeyEvent;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * Publishes a MediaSession while producing NO audio, to find out whether
 * Android will make it the Bluetooth addressed player.
 *
 * Everything observable is appended to EVENTS and rendered by MainActivity —
 * adb is unreachable from red5 (adbd binds wlan0 only), so the phone screen is
 * the readout.
 */
public class SpikeService extends Service {

    public static final String TITLE = "SPIKE-TITLE-8a";
    private static final String CHANNEL = "spike";
    private static final int NOTIF_ID = 1;

    /** Shared with MainActivity; guarded by its own monitor. */
    private static final List<String> EVENTS = new ArrayList<>();

    private MediaSession session;
    private AudioManager audioManager;
    private AudioFocusRequest focusRequest;
    private boolean holdingFocus = false;
    private AudioTrack silence;
    private volatile boolean silenceRunning = false;

    public static void log(String line) {
        String stamp = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        synchronized (EVENTS) {
            EVENTS.add(stamp + "  " + line);
        }
    }

    public static String dump() {
        synchronized (EVENTS) {
            if (EVENTS.isEmpty()) return "(no events yet)";
            StringBuilder sb = new StringBuilder();
            for (int i = EVENTS.size() - 1; i >= 0; i--) {
                sb.append(EVENTS.get(i)).append('\n');
            }
            return sb.toString();
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        audioManager = (AudioManager) getSystemService(Context.AUDIO_SERVICE);

        session = new MediaSession(this, "AgentMediaSpike");
        session.setCallback(new MediaSession.Callback() {
            @Override public void onPlay() { log("onPlay"); }
            @Override public void onPause() { log("onPause"); }
            @Override public void onStop() { log("onStop"); }
            @Override public void onSkipToNext() { log("onSkipToNext"); }
            @Override public void onSkipToPrevious() { log("onSkipToPrevious"); }
            @Override public void onSeekTo(long pos) { log("onSeekTo " + pos); }

            @Override
            public boolean onMediaButtonEvent(Intent intent) {
                // The raw AVRCP path. Logged separately from the transport
                // callbacks above so we can tell which layer delivered.
                KeyEvent ev = intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent.class);
                log("onMediaButtonEvent " + (ev == null ? "null" : ev.toString()));
                return super.onMediaButtonEvent(intent);
            }
        });

        session.setMetadata(new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, TITLE)
                .putString(MediaMetadata.METADATA_KEY_ARTIST, "agent-media spike")
                .putString(MediaMetadata.METADATA_KEY_ALBUM, "not actually playing")
                .putLong(MediaMetadata.METADATA_KEY_DURATION, 300000L)
                .build());

        // STATE_PLAYING is the point: we claim to be playing while emitting
        // no audio at all, and see whether Bluetooth addresses us.
        session.setPlaybackState(new PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_PLAY
                        | PlaybackState.ACTION_PAUSE
                        | PlaybackState.ACTION_PLAY_PAUSE
                        | PlaybackState.ACTION_STOP
                        | PlaybackState.ACTION_SKIP_TO_NEXT
                        | PlaybackState.ACTION_SKIP_TO_PREVIOUS
                        | PlaybackState.ACTION_SEEK_TO)
                .setState(PlaybackState.STATE_PLAYING, 0L, 1.0f)
                .build());
        // Run 2: route media buttons through an explicit receiver. Run 1 set
        // none and saw nothing at all -- this is the likeliest cause.
        session.setMediaButtonBroadcastReceiver(
                new android.content.ComponentName(this, SpikeButtonReceiver.class));

        session.setActive(true);

        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(
                CHANNEL, "Spike", NotificationManager.IMPORTANCE_LOW));

        Notification n = new Notification.Builder(this, CHANNEL)
                .setContentTitle(TITLE)
                .setContentText("MediaSession spike — no audio")
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setStyle(new Notification.MediaStyle().setMediaSession(session.getSessionToken()))
                .setOngoing(true)
                .build();

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        } else {
            startForeground(NOTIF_ID, n);
        }

        startSilence();

        log("service started; session active, state=PLAYING, silent track");
    }

    /**
     * Run 3: hold a real AudioTrack that writes digital silence.
     *
     * Runs 1 and 2 published a session claiming STATE_PLAYING while opening no
     * audio stream at all, and Bluetooth never addressed us. The remaining
     * difference from a real player is the stream itself -- Android's
     * addressed-player selection may require an active track, not just an
     * active session.
     *
     * Volume is left at 1.0 deliberately: the buffer is all zeros, so output is
     * inaudible either way, and a zero-VOLUME track risks being optimised out
     * of the mix -- which would silently invalidate the experiment.
     */
    private void startSilence() {
        int rate = 44100;
        int min = AudioTrack.getMinBufferSize(rate,
                AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
        final int buf = Math.max(min, 4096);

        silence = new AudioTrack.Builder()
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
        silence.play();
        silenceRunning = true;

        new Thread(() -> {
            short[] zeros = new short[buf / 2];
            while (silenceRunning) {
                // Blocking write paces the loop at real time.
                if (silence.write(zeros, 0, zeros.length) < 0) break;
            }
        }, "spike-silence").start();

        log("silent AudioTrack started (USAGE_MEDIA, zeros at full volume)");
    }

    private void stopSilence() {
        silenceRunning = false;
        if (silence != null) {
            try { silence.stop(); } catch (IllegalStateException ignored) { }
            silence.release();
            silence = null;
        }
    }

    /**
     * Step 4 of the spike: does holding audio focus change session priority,
     * and does grabbing it silence the Termux mpv playing underneath?
     * Toggled from the UI so this needs no rebuild.
     */
    public boolean toggleFocus() {
        if (holdingFocus) {
            if (focusRequest != null) audioManager.abandonAudioFocusRequest(focusRequest);
            holdingFocus = false;
            log("audio focus ABANDONED");
        } else {
            focusRequest = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                            .build())
                    .setOnAudioFocusChangeListener(change -> log("focus change: " + change))
                    .build();
            int r = audioManager.requestAudioFocus(focusRequest);
            holdingFocus = (r == AudioManager.AUDIOFOCUS_REQUEST_GRANTED);
            log("audio focus requested -> " + (holdingFocus ? "GRANTED" : "result " + r));
        }
        return holdingFocus;
    }

    public class LocalBinder extends android.os.Binder {
        SpikeService service() { return SpikeService.this; }
    }

    private final IBinder binder = new LocalBinder();

    @Override
    public IBinder onBind(Intent intent) {
        return binder;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopSilence();
        if (holdingFocus && focusRequest != null) {
            audioManager.abandonAudioFocusRequest(focusRequest);
        }
        if (session != null) {
            session.setActive(false);
            session.release();
        }
        super.onDestroy();
    }
}
