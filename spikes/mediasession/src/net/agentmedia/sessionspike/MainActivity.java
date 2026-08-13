package net.agentmedia.sessionspike;

import android.Manifest;
import android.app.Activity;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * The readout. adb cannot reach this phone from red5 (adbd binds wlan0 only),
 * so the event log is rendered on screen rather than to logcat — press the
 * earbud/car buttons, then read or screenshot this.
 */
public class MainActivity extends Activity {

    private TextView logView;
    private Button focusButton;
    private SpikeService service;
    private final Handler handler = new Handler(Looper.getMainLooper());

    private final ServiceConnection conn = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((SpikeService.LocalBinder) binder).service();
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            service = null;
        }
    };

    private final Runnable tick = new Runnable() {
        @Override public void run() {
            logView.setText(SpikeService.dump());
            handler.postDelayed(this, 500);
        }
    };

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);

        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(24, 24, 24, 24);

        TextView head = new TextView(this);
        head.setText("MediaSession spike — publishes a session, plays nothing.\n"
                + "Press play/pause, next, prev on earbuds or the car.\n"
                + "Newest event first.");
        head.setTextSize(14f);
        root.addView(head);

        focusButton = new Button(this);
        focusButton.setText("Toggle audio focus (step 4)");
        focusButton.setOnClickListener(v -> {
            if (service != null) {
                boolean held = service.toggleFocus();
                focusButton.setText(held ? "Audio focus HELD — tap to release"
                                         : "Toggle audio focus (step 4)");
            }
        });
        root.addView(focusButton);

        // No "list active sessions" button: enumerating sessions needs a
        // NotificationListenerService, which Play Protect refuses to install
        // when sideloaded. The media-button receiver is the real experiment.

        logView = new TextView(this);
        logView.setTypeface(Typeface.MONOSPACE);
        logView.setTextSize(12f);
        logView.setTextColor(Color.WHITE);
        logView.setBackgroundColor(Color.BLACK);
        logView.setPadding(12, 12, 12, 12);

        ScrollView scroller = new ScrollView(this);
        scroller.addView(logView, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(scroller, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);

        Intent svc = new Intent(this, SpikeService.class);
        startForegroundService(svc);
        bindService(svc, conn, Context.BIND_AUTO_CREATE);
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(tick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(tick);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        try { unbindService(conn); } catch (IllegalArgumentException ignored) { }
    }
}
