package net.agentmedia.companion;

import android.Manifest;
import android.app.Activity;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
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

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * What is playing, and the controls for it.
 *
 * <h4>What this screen used to be</h4>
 *
 * A diagnostic: a status dump over a black monospace event log, both re-rendered
 * twice a second, because there is no adb on this phone and no other way to see
 * inside. That was the right screen while the question was "does any of this
 * work at all". It is the wrong one now that the app is something David picks
 * up — and it was actively harmful, because laying out a thousand-line log on
 * the main thread is what the service's ten seconds to reach startForeground
 * were being spent on. The log moved to {@link DiagnosticsActivity}, where it is
 * still one tap away and no longer in the way.
 *
 * <h4>And what it absorbed</h4>
 *
 * ControlsActivity, which was a second screen that knew the same three channels
 * and put a tab bar on top of them. The tabs were restating what this screen
 * already shows, so the channels themselves became the selector: the one being
 * driven sits at the top with its progress, its clock and every verb the popup
 * has, and the other two are rows underneath. Tap one and it takes the wheel.
 *
 * It opens on whatever is playing, because that is nearly always the answer.
 */
public class MainActivity extends Activity {

    /** The channel poll, while this screen is looking. Same as the popup's. */
    private static final long POLL_MS = 1000;

    private final Handler main = new Handler(Looper.getMainLooper());
    private CompanionService service;
    private boolean polling;

    private String driving = Channels.ORDER[0];
    /** True until the first poll picks the channel that is actually playing. */
    private boolean autoPick = true;
    private Map<String, Channels.Channel> channels;

    private LinearLayout driverSlot;
    private LinearLayout rowSlot;
    private LinearLayout healthStrip;
    private Transport transport;
    private ChannelCard.Views driverCard;
    private final Map<String, ChannelCard.Views> rowCards =
            new LinkedHashMap<String, ChannelCard.Views>();

    private final ServiceConnection conn = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((CompanionService.LocalBinder) binder).service();
            renderHealth();
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            service = null;
            renderHealth();
        }
    };

    private final Runnable tick = new Runnable() {
        @Override public void run() {
            if (!polling) return;
            poll();
            main.postDelayed(this, POLL_MS);
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
        root.setBackgroundColor(Style.GROUND);
        int pad = dp(Style.gap(4));
        root.setPadding(pad, pad, pad, dp(Style.gap(2)));

        root.addView(header());

        driverSlot = new LinearLayout(this);
        driverSlot.setOrientation(LinearLayout.VERTICAL);
        driverSlot.setPadding(0, dp(Style.gap(3)), 0, 0);
        root.addView(driverSlot);

        rowSlot = new LinearLayout(this);
        rowSlot.setOrientation(LinearLayout.VERTICAL);
        rowSlot.setPadding(0, dp(Style.gap(4)), 0, 0);
        root.addView(rowSlot);

        healthStrip = new LinearLayout(this);
        healthStrip.setOrientation(LinearLayout.HORIZONTAL);
        healthStrip.setPadding(0, dp(Style.gap(5)), 0, dp(Style.gap(2)));
        root.addView(healthStrip);

        root.addView(footer());

        ScrollView scroller = new ScrollView(this);
        scroller.addView(root, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        scroller.setBackgroundColor(Style.GROUND);
        setContentView(scroller);

        buildCards();
        renderHealth();

        Intent svc = new Intent(this, CompanionService.class);
        startForegroundService(svc);
        bindService(svc, conn, Context.BIND_AUTO_CREATE);
    }

    @Override
    protected void onResume() {
        super.onResume();
        polling = true;
        main.post(tick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        // A window onto state costs nothing when nobody is looking at it — the
        // bargain the popup makes, and the one the old half-second tick did not.
        polling = false;
        main.removeCallbacks(tick);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        try { unbindService(conn); } catch (IllegalArgumentException ignored) { }
    }

    // ---- the pieces --------------------------------------------------------

    private View header() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);

        TextView name = new TextView(this);
        name.setText("agent-media");
        name.setTextSize(Style.HEAD);
        name.setTextColor(Style.INK);
        name.setTypeface(Typeface.DEFAULT_BOLD);
        bar.addView(name, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        // The build stamp lives up here rather than in the log, because "is the
        // phone running the fix I just made" is asked far more often than it is
        // logged — and on 2026-08-15 inference got it wrong and cost a round
        // trip arguing with a fix that had never been installed.
        TextView build = new TextView(this);
        build.setText(buildStamp());
        build.setTextSize(Style.LABEL);
        build.setTextColor(Style.FAINT);
        build.setTypeface(Typeface.MONOSPACE);
        bar.addView(build);

        return bar;
    }

    private View footer() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);

        bar.addView(link("Recently played", new View.OnClickListener() {
            @Override public void onClick(View v) {
                startActivity(new Intent(MainActivity.this, RecentActivity.class));
            }
        }), weight());
        bar.addView(link("Diagnostics", new View.OnClickListener() {
            @Override public void onClick(View v) {
                startActivity(new Intent(MainActivity.this, DiagnosticsActivity.class));
            }
        }), weight());
        return bar;
    }

    private TextView link(String label, View.OnClickListener onClick) {
        TextView t = new TextView(this);
        t.setText(label);
        t.setTextSize(Style.BODY);
        t.setTextColor(Style.MUTED);
        t.setGravity(Gravity.CENTER);
        t.setMinimumHeight(dp(Style.TOUCH));
        t.setClickable(true);
        t.setFocusable(true);
        t.setOnClickListener(onClick);
        return t;
    }

    /**
     * Build the driver block and the two rows for the channel being driven.
     *
     * Rebuilt when the driven channel changes, and not otherwise: the poll
     * updates the views it made rather than making new ones, so a screen open
     * for an hour is the same view tree it started as.
     */
    private void buildCards() {
        driverSlot.removeAllViews();
        rowSlot.removeAllViews();
        rowCards.clear();

        driverCard = ChannelCard.build(this, driving, true);
        driverSlot.addView(driverCard.root);

        transport = new Transport(this, new Transport.Host() {
            @Override public String channel() { return driving; }
            @Override public void refreshed() { poll(); }
        });
        driverSlot.addView(transport.build());
        transport.accent(driving);

        for (final String name : Channels.ORDER) {
            if (name.equals(driving)) continue;
            ChannelCard.Views row = ChannelCard.build(this, name, false);
            row.root.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { drive(name); }
            });
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.topMargin = dp(Style.gap(2));
            rowSlot.addView(row.root, lp);
            rowCards.put(name, row);
        }
        applyChannels();
    }

    /** Take the wheel. A deliberate choice, so stop guessing from now on. */
    private void drive(String channel) {
        if (channel.equals(driving)) return;
        driving = channel;
        autoPick = false;
        buildCards();
    }

    // ---- state -------------------------------------------------------------

    private void poll() {
        new Thread(new Runnable() {
            @Override public void run() {
                final Map<String, Channels.Channel> got = Channels.fetch(Loopback.PORT);
                main.post(new Runnable() {
                    @Override public void run() {
                        channels = got;
                        // Open on what is playing. Only until the first press:
                        // a screen that reorders itself under a thumb is worse
                        // than one that opened on the wrong channel.
                        if (autoPick) {
                            String playing = firstPlaying(got);
                            if (playing != null && !playing.equals(driving)) {
                                driving = playing;
                                buildCards();
                                return;
                            }
                        }
                        applyChannels();
                        renderHealth();
                    }
                });
            }
        }, "channels-poll").start();
    }

    private static String firstPlaying(Map<String, Channels.Channel> got) {
        for (String name : Channels.ORDER) {
            Channels.Channel c = got.get(name);
            if (c != null && c.playing) return name;
        }
        return null;
    }

    private void applyChannels() {
        Channels.Channel driven = channels == null ? null : channels.get(driving);
        if (driverCard != null) ChannelCard.apply(driverCard, driving, driven);
        if (transport != null) transport.apply(driven);
        for (Map.Entry<String, ChannelCard.Views> e : rowCards.entrySet()) {
            ChannelCard.apply(e.getValue(), e.getKey(),
                              channels == null ? null : channels.get(e.getKey()));
        }
    }

    private void renderHealth() {
        if (healthStrip == null) return;
        healthStrip.removeAllViews();
        List<Health.Pill> pills = Health.strip(
                service != null,
                service != null && service.micWatching(),
                service == null ? 0 : service.bridgesUp(),
                service == null ? 0 : Health.deathsOn(service.exits(), today()));
        for (Health.Pill p : pills) healthStrip.addView(pill(p));
    }

    private View pill(Health.Pill p) {
        int colour = p.level == Health.Level.OK ? Style.OK : Style.WARN;
        TextView t = new TextView(this);
        t.setText(p.text);
        t.setTextSize(Style.LABEL);
        t.setTextColor(colour);
        t.setTypeface(Typeface.MONOSPACE);
        int px = dp(Style.gap(2));
        t.setPadding(px, dp(Style.gap(1)), px, dp(Style.gap(1)));

        GradientDrawable d = new GradientDrawable();
        d.setColor(Style.SURFACE);
        d.setCornerRadius(dp(999));
        d.setStroke(dp(1), ChannelCard.withAlpha(colour, 0x66));
        t.setBackground(d);

        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.rightMargin = dp(Style.gap(2));
        t.setLayoutParams(lp);
        return t;
    }

    private String today() {
        return new SimpleDateFormat("MM-dd", Locale.US).format(new Date());
    }

    private String buildStamp() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Throwable e) {
            return "";
        }
    }

    private LinearLayout.LayoutParams weight() {
        return new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
