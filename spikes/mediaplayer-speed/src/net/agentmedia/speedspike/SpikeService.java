package net.agentmedia.speedspike;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

/**
 * A foreground service for the length of a run, so the freezer leaves us alone.
 *
 * {@code mediaPlayback} is the honest type: this plays audio, and it is the
 * same type the shipping player would declare. Its whole job is to be a reason
 * for the process to keep running while the screen shows something else — see
 * {@link Spike} for the logcat that made this necessary.
 */
public class SpikeService extends Service {

    private static final String CHANNEL = "spike";

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(
                CHANNEL, "Speed spike", NotificationManager.IMPORTANCE_LOW));
        startForeground(1, new Notification.Builder(this, CHANNEL)
                .setContentTitle("MediaPlayer speed spike")
                .setContentText("running the trials")
                .setSmallIcon(android.R.drawable.ic_media_play)
                .build());

        if (intent != null && intent.getBooleanExtra("run", false)) {
            final String url = intent.getStringExtra("url");
            final boolean mute = intent.getBooleanExtra("mute", false);
            new Thread(() -> {
                SpeedTrials t = Spike.trials(this);
                t.setMuted(mute);
                Spike.clear();
                t.runAll(url, Spike::report);
                stopSelf();
            }, "speed-trials").start();
        }
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
