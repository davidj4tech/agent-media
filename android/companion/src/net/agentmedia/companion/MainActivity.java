package net.agentmedia.companion;

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
 * Starts the service and shows what it is doing.
 *
 * This is the only readout there is: adb cannot reach p8a from red5 (adbd binds
 * wlan0 only), so state that would normally go to logcat goes on screen —
 * mpv's state on top, newest event first below.
 */
public class MainActivity extends Activity {

    private TextView statusView;
    private TextView logView;
    private Button focusButton;
    private CompanionService service;
    private final Handler handler = new Handler(Looper.getMainLooper());

    private final ServiceConnection conn = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((CompanionService.LocalBinder) binder).service();
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            service = null;
        }
    };

    private final Runnable tick = new Runnable() {
        @Override public void run() {
            statusView.setText(service == null ? "(service not bound)" : service.status());
            logView.setText(CompanionService.dump());
            focusButton.setEnabled(service != null);
            focusButton.setText(service != null && service.focusActs()
                    ? "focus: acting on mpv — tap for probe only"
                    : "focus: probe only — tap to act on mpv");
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
        head.setText("agent-media companion — publishes the MediaSession,\n"
                + "drives Termux mpv over " + CompanionService.MPV_HOST
                + ":" + CompanionService.MPV_PORT + ". Plays nothing itself.");
        head.setTextSize(14f);
        root.addView(head);

        statusView = new TextView(this);
        statusView.setTypeface(Typeface.MONOSPACE);
        statusView.setTextSize(13f);
        statusView.setPadding(0, 12, 0, 12);
        root.addView(statusView);

        // The app always *takes* focus; this only decides whether the policy is
        // allowed to touch mpv. A fresh install starts as a probe so the first
        // sideload can show what Android actually delivers before anything acts
        // on it — and there is no adb here to flip a flag with.
        focusButton = new Button(this);
        focusButton.setAllCaps(false);
        focusButton.setOnClickListener(v -> {
            if (service != null) service.setFocusActs(!service.focusActs());
        });
        root.addView(focusButton);

        // The only screen here that is about agent-media rather than about this
        // app's own plumbing. It lives behind a button because the readout
        // above is what this activity is for — diagnosis — and the history is
        // something you go and look at.
        Button recentButton = new Button(this);
        recentButton.setAllCaps(false);
        recentButton.setText("Recently played");
        recentButton.setOnClickListener(v ->
                startActivity(new Intent(this, RecentActivity.class)));
        root.addView(recentButton);

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

        Intent svc = new Intent(this, CompanionService.class);
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
