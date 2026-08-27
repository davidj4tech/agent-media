package net.agentmedia.companion;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * The event log, and the switches that are not anybody's daily business.
 *
 * This is the screen the app used to open with. It is still the only way to see
 * inside a phone with no adb and a logcat that shows only Termux's uid, and it
 * still earns its keep the moment something is wrong — it is simply not what
 * you want when you pick the phone up to skip a track.
 *
 * What lives here: the service's own status line, the probe/acting switch for
 * the focus policy, the exits Android has recorded, and the log. What does not:
 * anything you would look at when things are working.
 *
 * The half-second tick came with it, and stays. Here it is honest — this screen
 * exists to watch events arrive — and here it costs nothing, because the log is
 * only being laid out while somebody is reading it.
 */
public class DiagnosticsActivity extends Activity {

    private static final long TICK_MS = 500;

    private final Handler main = new Handler(Looper.getMainLooper());
    private CompanionService service;
    private TextView status;
    private TextView exits;
    private TextView log;
    private TextView focusButton;
    private TextView dndButton;
    private boolean ticking;

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
            if (!ticking) return;
            render();
            main.postDelayed(this, TICK_MS);
        }
    };

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Style.GROUND);
        int pad = dp(Style.gap(4));
        root.setPadding(pad, pad, pad, dp(Style.gap(3)));

        TextView heading = new TextView(this);
        heading.setText("Diagnostics");
        heading.setTextSize(Style.TITLE);
        heading.setTextColor(Style.INK);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(heading);

        status = mono(Style.MUTED);
        status.setPadding(0, dp(Style.gap(3)), 0, 0);
        root.addView(status);

        focusButton = button(new View.OnClickListener() {
            @Override public void onClick(View v) {
                if (service != null) service.setFocusActs(!service.focusActs());
                render();
            }
        });
        LinearLayout.LayoutParams focusParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        focusParams.topMargin = dp(Style.gap(3));
        root.addView(focusButton, focusParams);

        // The Do Not Disturb grant. A button rather than a note in the README
        // because the grant cannot be asked for in a dialog — it is a trip to
        // a system settings screen most people have never opened — and because
        // ungranted is a silent half-feature: alerts still speak through DND
        // and nothing anywhere says why. This row is where that becomes
        // visible on the phone itself.
        dndButton = button(new View.OnClickListener() {
            @Override public void onClick(View v) {
                try {
                    startActivity(new Intent(
                            android.provider.Settings
                                    .ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS));
                } catch (RuntimeException e) {
                    // Some builds have no such screen. Nothing to recover, and
                    // an unhandled throw here takes the diagnostics screen down
                    // with it — which is the one screen you need when something
                    // is wrong.
                    CompanionService.log("dnd settings unavailable: " + e);
                }
            }
        });
        LinearLayout.LayoutParams dndParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        dndParams.topMargin = dp(Style.gap(2));
        root.addView(dndButton, dndParams);

        root.addView(label("How the process last ended"));
        exits = mono(Style.MUTED);
        root.addView(exits);

        root.addView(label("Events"));
        log = mono(Style.MUTED);
        log.setBackgroundColor(Style.SUNKEN);
        int lp = dp(Style.gap(2));
        log.setPadding(lp, lp, lp, lp);
        ScrollView scroller = new ScrollView(this);
        scroller.addView(log);
        root.addView(scroller, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);
        Edges.fit(root);
        bindService(new Intent(this, CompanionService.class), conn,
                    Context.BIND_AUTO_CREATE);
    }

    @Override
    protected void onResume() {
        super.onResume();
        ticking = true;
        main.post(tick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        ticking = false;
        main.removeCallbacks(tick);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        try { unbindService(conn); } catch (IllegalArgumentException ignored) { }
    }

    private void render() {
        status.setText(service == null ? "(service not bound)" : service.status());
        focusButton.setText(service != null && service.focusActs()
                ? "focus: acting on mpv — tap for probe only"
                : "focus: probe only — tap to act on mpv");
        focusButton.setEnabled(service != null);
        focusButton.setAlpha(service != null ? 1f : 0.4f);

        android.app.NotificationManager nm =
                getSystemService(android.app.NotificationManager.class);
        boolean granted = nm != null && nm.isNotificationPolicyAccessGranted();
        dndButton.setText(granted
                ? "Do Not Disturb: visible — alerts stay quiet in DND"
                : "Do Not Disturb: not visible — tap to allow");

        if (service == null || service.exits().isEmpty()) {
            exits.setText("(Android has no record — first run, or a reboot since)");
        } else {
            StringBuilder sb = new StringBuilder();
            for (String line : service.exits()) sb.append(line).append('\n');
            exits.setText(sb.toString().trim());
        }
        log.setText(CompanionService.dump());
    }

    /** The one button shape this screen uses: a full-width tappable card. */
    private TextView button(View.OnClickListener onClick) {
        TextView b = new TextView(this);
        b.setTextSize(Style.BODY);
        b.setTextColor(Style.INK);
        b.setGravity(Gravity.CENTER);
        b.setMinimumHeight(dp(Style.TOUCH));
        GradientDrawable d = new GradientDrawable();
        d.setColor(Style.SURFACE);
        d.setCornerRadius(dp(8));
        d.setStroke(dp(1), Style.RULE);
        b.setBackground(d);
        b.setClickable(true);
        b.setFocusable(true);
        b.setOnClickListener(onClick);
        return b;
    }

    private TextView label(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(Style.LABEL);
        t.setTextColor(Style.FAINT);
        t.setTypeface(Typeface.MONOSPACE);
        t.setAllCaps(true);
        t.setLetterSpacing(0.08f);
        t.setPadding(0, dp(Style.gap(4)), 0, dp(Style.gap(1)));
        return t;
    }

    private TextView mono(int colour) {
        TextView t = new TextView(this);
        t.setTypeface(Typeface.MONOSPACE);
        t.setTextSize(Style.LABEL);
        t.setTextColor(colour);
        return t;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
