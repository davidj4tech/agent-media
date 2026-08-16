package net.agentmedia.companion;

import android.app.Activity;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
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
 * <h4>What the redesign is for</h4>
 *
 * You open this to find one thing you half-remember. The old list gave the eye
 * nothing to sort on — twenty-five rows, one weight, white on black, the
 * channel spelled out in the second line and the time as "3h ago". So the row
 * leads with the two things you actually navigate by: <b>when</b>, on the left
 * in a fixed column, and <b>which channel</b>, as the colour beside it. The
 * title comes third, which is the order you were reading it in anyway.
 *
 * Hand-built views rather than a ListView + adapter: the list is twenty-five
 * rows, this app has no AndroidX and no layout XML beyond a strings file, and a
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
        root.setBackgroundColor(Style.GROUND);
        int pad = dp(Style.gap(4));
        root.setPadding(pad, pad, pad, 0);

        TextView heading = new TextView(this);
        heading.setText("Recently played");
        heading.setTextSize(Style.TITLE);
        heading.setTextColor(Style.INK);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(heading);

        status = new TextView(this);
        status.setTextColor(Style.FAINT);
        status.setTextSize(Style.LABEL);
        status.setTypeface(Typeface.MONOSPACE);
        status.setPadding(0, dp(Style.gap(2)), 0, dp(Style.gap(2)));
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
        for (RecentRows.Entry e : RecentRows.group(items, System.currentTimeMillis())) {
            rows.addView(e.isHeading() ? headingView(e.heading) : rowView(e));
        }
        // Something to stop the last row sitting against the bottom edge.
        View tail = new View(this);
        rows.addView(tail, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(Style.gap(6))));
    }

    private View headingView(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setAllCaps(true);
        t.setLetterSpacing(0.1f);
        t.setTextSize(Style.LABEL);
        t.setTextColor(Style.FAINT);
        t.setTypeface(Typeface.MONOSPACE);
        t.setPadding(0, dp(Style.gap(5)), 0, dp(Style.gap(1)));
        return t;
    }

    private View rowView(final RecentRows.Entry entry) {
        final RecentList.Item item = entry.item;
        boolean playable = item.playable();

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(Style.gap(2)), 0, dp(Style.gap(2)));
        row.setMinimumHeight(dp(Style.TOUCH));

        TextView when = new TextView(this);
        when.setText(entry.clock);
        when.setTextSize(Style.LABEL);
        when.setTextColor(Style.FAINT);
        when.setTypeface(Typeface.MONOSPACE);
        when.setGravity(Gravity.END);
        LinearLayout.LayoutParams whenParams = new LinearLayout.LayoutParams(
                dp(44), ViewGroup.LayoutParams.WRAP_CONTENT);
        whenParams.rightMargin = dp(Style.gap(3));
        row.addView(when, whenParams);

        // The channel, as a colour rather than a word. It also gives the list a
        // left edge to run the eye down, which a column of times does not.
        View dot = new View(this);
        GradientDrawable d = new GradientDrawable();
        d.setColor(Style.accent(item.channel));
        d.setCornerRadius(dp(4));
        dot.setBackground(d);
        LinearLayout.LayoutParams dotParams =
                new LinearLayout.LayoutParams(dp(6), dp(6));
        dotParams.rightMargin = dp(Style.gap(3));
        row.addView(dot, dotParams);

        LinearLayout lines = new LinearLayout(this);
        lines.setOrientation(LinearLayout.VERTICAL);

        TextView title = new TextView(this);
        title.setText(RecentRows.title(item));
        title.setTextColor(Style.INK);
        title.setTextSize(Style.HEAD);
        // One line: a signed URL or an untitled file can run to hundreds of
        // characters, and a row that wraps six times is not a list any more.
        title.setSingleLine(true);
        title.setEllipsize(android.text.TextUtils.TruncateAt.MIDDLE);
        lines.addView(title);

        TextView sub = new TextView(this);
        sub.setText(RecentRows.subtitle(item));
        sub.setTextColor(Style.MUTED);
        sub.setTextSize(Style.BODY);
        sub.setSingleLine(true);
        sub.setEllipsize(android.text.TextUtils.TruncateAt.END);
        lines.addView(sub);

        row.addView(lines, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        if (playable) {
            row.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { play(item); }
            });
        } else {
            // Dimmed *and* labelled. Grey alone was doing two jobs — "second
            // line" and "this will not work" — and you found out which by
            // tapping it.
            row.setAlpha(0.45f);
        }
        return row;
    }

    private void play(final RecentList.Item item) {
        // Say something immediately: the listener answers before it plays, and
        // acquisition can take a while. Silence after a tap reads as a dead app.
        toast("playing " + RecentRows.title(item) + "…");
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
