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

    /**
     * The tabs. "all" first and selected on entry, because the question this
     * screen answers — "what was that thing" — usually does not know which
     * channel it was on; the filter is for when it does.
     */
    private static final String[] TABS = {"all", "speech", "music", "book"};

    private LinearLayout rows;
    private TextView status;
    private Tabs tabs;
    private String channel = "all";
    /**
     * Which conversations are unfolded, and which you folded yourself.
     *
     * Both are kept across reloads. The live conversation is unfolded on every
     * fetch — it is the one you did not come here looking for — but only until
     * you close it: a list that re-opens what you just shut, every time
     * something plays, is a list arguing with you.
     */
    private final java.util.Set<String> open = new java.util.HashSet<String>();
    private final java.util.Set<String> folded = new java.util.HashSet<String>();
    /** The last list drawn, so a fold can redraw without asking again. */
    private List<RecentList.Item> shown = java.util.Collections.emptyList();
    private Loopback.Reply shownReply;
    private final Handler main = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Style.GROUND);
        int pad = dp(Style.gap(4));
        root.setPadding(pad, pad, pad, 0);

        // A way back that is on the screen. The system gesture works and is
        // invisible, and this is a screen you arrive at from one place and
        // leave to the same place — so it says so, in the corner where every
        // other app puts it.
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);

        TextView back = new TextView(this);
        back.setText("‹");
        back.setTextSize(Style.TITLE);
        back.setTextColor(Style.MUTED);
        back.setGravity(Gravity.CENTER);
        back.setContentDescription("back");
        back.setClickable(true);
        back.setFocusable(true);
        back.setMinWidth(dp(Style.TOUCH));
        back.setMinimumHeight(dp(Style.TOUCH));
        back.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });
        LinearLayout.LayoutParams backParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        // Pulled left by the padding the screen already has, so the arrow sits
        // on the edge where a thumb reaches for it and the title stays where it
        // has always been.
        backParams.leftMargin = -dp(Style.gap(3));
        backParams.rightMargin = dp(Style.gap(1));
        top.addView(back, backParams);

        TextView heading = new TextView(this);
        heading.setText("Recently played");
        heading.setTextSize(Style.TITLE);
        heading.setTextColor(Style.INK);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        top.addView(heading);
        root.addView(top);

        tabs = new Tabs(this, new Tabs.Host() {
            @Override public void drive(String picked) {
                if (picked.equals(channel)) return;
                channel = picked;
                tabs.select(picked);
                status.setText("loading…");
                rows.removeAllViews();
                load();
                restartBeat();   // a new tab restarts the clock, not doubles it
            }
        }, TABS);
        root.addView(tabs.build());

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
        tabs.select(channel);
        Edges.fit(root);
    }

    @Override
    protected void onResume() {
        super.onResume();
        // Refreshed on every entry rather than cached: something else on the
        // phone — a share, a spoken reply, David asking Sam — may have played
        // since this was last open, and a stale list is worse than a slow one.
        load();
        restartBeat();
    }

    @Override
    protected void onPause() {
        super.onPause();
        main.removeCallbacks(beat);
    }

    /**
     * Reload while the screen is open.
     *
     * Entry-only refresh was right for a list of things you played, and wrong
     * for a list of things being said: the turn you are listening to is not in
     * it — history is written when a turn ends — so the screen you opened to
     * watch a conversation on froze on the clip before the one you could hear.
     * A beat is a fetch like any other, so the newest conversation unfolds
     * itself as it always has — and stays shut if that is where you left it,
     * which is what `folded` is for. Nothing you opened or closed moves.
     *
     * The hub's own clip list is cached for twenty seconds, so asking much
     * faster than this would mostly re-read that cache.
     */
    private static final long BEAT_MS = 15000;

    private final Runnable beat = new Runnable() {
        @Override public void run() {
            load();
            main.postDelayed(this, BEAT_MS);
        }
    };

    private void restartBeat() {
        main.removeCallbacks(beat);
        main.postDelayed(beat, BEAT_MS);
    }

    private void load() {
        final String asked = channel;
        new Thread(new Runnable() {
            @Override public void run() {
                final Loopback.Reply reply = Loopback.get(
                        Settings.server(RecentActivity.this),
                        RecentList.path(LIMIT, asked));
                final List<RecentList.Item> items = reply.ok()
                        ? RecentList.parse(reply.body)
                        : java.util.Collections.<RecentList.Item>emptyList();
                main.post(new Runnable() {
                    @Override public void run() {
                        // A tab switched while this was in flight: the answer
                        // is about the old one, and drawing it would leave the
                        // strip pointing at a list nobody asked for.
                        if (asked.equals(channel)) render(items, reply, true);
                    }
                });
            }
        }, "recent-fetch").start();
    }

    private void render(List<RecentList.Item> items, Loopback.Reply reply,
                        boolean fresh) {
        shown = items;
        shownReply = reply;
        boolean speech = "speech".equals(channel);
        rows.removeAllViews();
        if (items.isEmpty()) {
            // An empty tab is not a broken listener: say which of the two it is.
            status.setText(reply != null && reply.ok() && !"all".equals(channel)
                           ? "nothing on the " + channel + " channel yet"
                           : RecentList.emptyReason(reply));
            return;
        }
        status.setText(items.size() + (speech ? " clips · tap to hear again"
                                             : " items · tap to play again"));
        // Speech groups by where it was said — tmux session, then the pane's
        // conversation — and everything else by day: the day is the bucket you
        // remember playing something in, and the place is the one you remember
        // it being said in.
        List<RecentRows.Entry> entries = speech
                ? RecentRows.byConversation(items)
                : RecentRows.group(items, System.currentTimeMillis());
        // Folded, except the one you are in — at both levels. The rows arrive
        // newest first, so the first session and the first conversation inside
        // it hold the newest clip: that is what is being spoken, or what just
        // was, and it is the only thing here you did not come looking for. The
        // rest are a name and a count until you ask, which is what makes a long
        // history scannable rather than a wall with headings in it.
        if (speech && fresh) {
            int wanted = 0;                      // the first heading at each depth
            for (RecentRows.Entry e : entries) {
                if (!e.isHeading() || e.depth != wanted) continue;
                if (!folded.contains(e.key)) open.add(e.key);
                wanted++;
            }
        }
        for (RecentRows.Entry e : entries) {
            if (!visible(e)) continue;
            rows.addView(e.isHeading() ? headingView(e) : rowView(e));
        }
        // Something to stop the last row sitting against the bottom edge.
        View tail = new View(this);
        rows.addView(tail, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(Style.gap(6))));
    }

    /** Drawn only when everything it sits inside is open. */
    private boolean visible(RecentRows.Entry entry) {
        for (String key : entry.ancestry()) {
            if (!open.contains(key)) return false;
        }
        return true;
    }

    private View headingView(final RecentRows.Entry entry) {
        TextView t = new TextView(this);
        boolean folds = entry.key != null;
        boolean shownOpen = folds && open.contains(entry.key);
        // The marker leads, so the column of triangles is what the eye runs
        // down — the names are all different lengths and none of them line up.
        t.setText(folds ? (shownOpen ? "▾  " : "▸  ") + entry.heading
                        : entry.heading);
        t.setAllCaps(true);
        t.setLetterSpacing(0.1f);
        t.setTextSize(Style.LABEL);
        t.setTextColor(shownOpen ? Style.MUTED : Style.FAINT);
        t.setTypeface(Typeface.MONOSPACE);
        // A nested heading is a step in, and a top one is bolder: the two
        // levels are a place and a conversation inside it, not two lists.
        // No indent for the nesting. On a phone the level costs width the
        // clips need more — the marker and the weight already say which is
        // inside which, and a wall of text pushed right says it worse.
        t.setPadding(0, dp(Style.gap(entry.depth == 0 ? 5 : 3)),
                     0, dp(Style.gap(1)));
        if (entry.depth == 0) t.setTextColor(shownOpen ? Style.INK : Style.MUTED);
        if (folds) {
            // A closed group is a control, so it has to be worth hitting.
            t.setMinimumHeight(dp(Style.TOUCH));
            t.setClickable(true);
            t.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) {
                    if (open.remove(entry.key)) {
                        folded.add(entry.key);      // shut on purpose: stay shut
                    } else {
                        open.add(entry.key);
                        folded.remove(entry.key);
                    }
                    render(shown, shownReply, false);
                }
            });
        }
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

        // No time column. It held five characters, cost their width on every
        // row including the ones with no time at all, and took it from the
        // line beside it — a sentence somebody said, ellipsised to fit. The
        // clock moved into the second line, where it reads the same.

        // The channel, as a colour rather than a word. It also gives the list a
        // left edge to run the eye down, which a column of times did not.
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
        sub.setText(RecentRows.subtitle(item, entry.clock));
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
        toast(("speech".equals(item.channel) ? "replaying " : "playing ")
              + RecentRows.title(item) + "…");
        new Thread(new Runnable() {
            @Override public void run() {
                final String line = RecentList.play(
                        Settings.server(RecentActivity.this), item);
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
