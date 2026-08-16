package net.agentmedia.companion;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.List;

/**
 * What played lately, tap to play it again.
 *
 * The first thing in this app that is a *view of agent-media's state* rather
 * than a control surface for the phone's own players. The history lives in the
 * SQLite store on the Termux side and arrives over the same loopback door the
 * share sheet uses; nothing here knows how a channel is chosen or where a file
 * comes from.
 *
 * Hand-built views rather than a ListView + adapter: the list is twenty rows,
 * this app has no AndroidX and no layout XML beyond a strings file, and a
 * ScrollView of TextViews is less code than an adapter would be.
 */
public class RecentActivity extends Activity {

    /** Enough to answer "what was that thing", not a library browser. */
    private static final int LIMIT = 25;

    private LinearLayout rows;
    private TextView status;
    private final Handler main = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);
        int pad = dp(12);
        root.setPadding(pad, pad, pad, pad);

        TextView heading = new TextView(this);
        heading.setText("Recently played");
        heading.setTextColor(Color.WHITE);
        heading.setTextSize(20);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(heading);

        status = new TextView(this);
        status.setTextColor(Color.GRAY);
        status.setPadding(0, dp(6), 0, dp(6));
        status.setText("loading…");
        root.addView(status);

        rows = new LinearLayout(this);
        rows.setOrientation(LinearLayout.VERTICAL);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(rows);
        root.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);
    }

    @Override
    protected void onResume() {
        super.onResume();
        // Refreshed on every entry rather than cached: something else on the
        // phone — a share, a spoken reply, David asking Sam — may have played
        // since this was last open, and a stale list is worse than a slow one.
        load();
    }

    private void load() {
        new Thread(new Runnable() {
            @Override public void run() {
                final Loopback.Reply reply =
                        Loopback.get(Loopback.PORT, "/recent?limit=" + LIMIT);
                final List<RecentList.Item> items = reply.ok()
                        ? RecentList.parse(reply.body)
                        : java.util.Collections.<RecentList.Item>emptyList();
                main.post(new Runnable() {
                    @Override public void run() { render(items, reply); }
                });
            }
        }, "recent-fetch").start();
    }

    private void render(List<RecentList.Item> items, Loopback.Reply reply) {
        rows.removeAllViews();
        if (items.isEmpty()) {
            status.setText(RecentList.emptyReason(reply));
            return;
        }
        status.setText(items.size() + " items · tap to play again");
        for (final RecentList.Item item : items) {
            rows.addView(rowView(item));
        }
    }

    private View rowView(final RecentList.Item item) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.VERTICAL);
        row.setPadding(dp(4), dp(10), dp(4), dp(10));

        TextView title = new TextView(this);
        title.setText(item.title());
        title.setTextColor(item.playable() ? Color.WHITE : Color.GRAY);
        title.setTextSize(16);
        // One line: a signed URL or an untitled file can run to hundreds of
        // characters, and a row that wraps six times is not a list any more.
        title.setSingleLine(true);
        title.setEllipsize(android.text.TextUtils.TruncateAt.MIDDLE);
        row.addView(title);

        TextView sub = new TextView(this);
        sub.setText(item.subtitle());
        sub.setTextColor(Color.parseColor("#888888"));
        sub.setTextSize(13);
        row.addView(sub);

        if (item.playable()) {
            row.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { play(item); }
            });
        }
        return row;
    }

    private void play(final RecentList.Item item) {
        // Say something immediately: the listener answers before it plays, and
        // acquisition can take a while. Silence after a tap reads as a dead app.
        toast("playing " + item.title() + "…");
        new Thread(new Runnable() {
            @Override public void run() {
                final String line = RecentList.play(Loopback.PORT, item);
                main.post(new Runnable() {
                    @Override public void run() { toast(line); }
                });
            }
        }, "recent-play").start();
    }

    private void toast(String text) {
        Toast toast = Toast.makeText(getApplicationContext(), text,
                                     Toast.LENGTH_LONG);
        toast.setGravity(Gravity.BOTTOM, 0, dp(48));
        toast.show();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
