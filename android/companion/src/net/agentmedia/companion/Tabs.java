package net.agentmedia.companion;

import android.content.Context;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The channel selector: three tabs, one underlined.
 *
 * <h4>Why this and not the rows</h4>
 *
 * The rows under the driver card were the selector for a while, and they were
 * the wrong shape for the job twice over: nothing said they were a control
 * (fixed with a chevron and a label, which is the sound a workaround makes),
 * and even told, a tap that promotes a row and demotes the card above it is a
 * rearrangement rather than a switch. A tab strip is the shape every Android
 * user already knows for "three things, look at one" — where you tap and what
 * changes are in different places, and the strip stays put while the content
 * under it swaps.
 *
 * <h4>What a tab has to carry</h4>
 *
 * The rows doubled as the readout for the two channels you are not driving,
 * and a tab has no room for a title. It keeps the half that is actually
 * glanced at: a dot in the channel's own hue when that channel is playing. So
 * "is the book still going while I fiddle with music" is answerable without
 * switching, which is the question the titles were really being asked.
 *
 * Hand-built like everything here — no AndroidX, so no TabLayout, and no
 * styles.xml for a platform tab to read. The underline is a view.
 */
final class Tabs {

    /** Told when a tab is chosen. */
    interface Host {
        void drive(String channel);
    }

    private static final class Tab {
        final LinearLayout root;
        final TextView label;
        final View dot;
        final View underline;

        Tab(LinearLayout root, TextView label, View dot, View underline) {
            this.root = root;
            this.label = label;
            this.dot = dot;
            this.underline = underline;
        }
    }

    private final Context ctx;
    private final Host host;
    private final String[] names;
    private final Map<String, Tab> tabs = new LinkedHashMap<String, Tab>();
    private String selected = "";

    Tabs(Context ctx, Host host) {
        this(ctx, host, Channels.ORDER);
    }

    /**
     * The same strip over a different set, for a screen whose tabs are not
     * exactly the three channels — the recent list opens on everything, and
     * "everything" is a tab there, not a fourth channel.
     */
    Tabs(Context ctx, Host host, String[] names) {
        this.ctx = ctx;
        this.host = host;
        this.names = names;
    }

    /** The strip, with a hairline under it for the whole row to sit on. */
    View build() {
        LinearLayout wrap = new LinearLayout(ctx);
        wrap.setOrientation(LinearLayout.VERTICAL);

        LinearLayout strip = new LinearLayout(ctx);
        strip.setOrientation(LinearLayout.HORIZONTAL);
        for (final String name : names) {
            Tab tab = tab(name);
            tab.root.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { host.drive(name); }
            });
            strip.addView(tab.root, new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
            tabs.put(name, tab);
        }
        wrap.addView(strip);

        // The rule runs the full width, behind the selected tab's underline:
        // three separate underlines with gaps between them read as three
        // buttons, and the whole point is that they are one control.
        View rule = new View(ctx);
        rule.setBackgroundColor(Style.RULE);
        wrap.addView(rule, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ChannelCard.dp(ctx, 1)));
        return wrap;
    }

    private Tab tab(String channel) {
        LinearLayout root = new LinearLayout(ctx);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_HORIZONTAL);
        root.setMinimumHeight(ChannelCard.dp(ctx, Style.TOUCH));
        root.setClickable(true);
        root.setFocusable(true);

        LinearLayout line = new LinearLayout(ctx);
        line.setOrientation(LinearLayout.HORIZONTAL);
        line.setGravity(Gravity.CENTER);
        line.setPadding(0, ChannelCard.dp(ctx, Style.gap(3)),
                        0, ChannelCard.dp(ctx, Style.gap(2)));

        TextView label = new TextView(ctx);
        label.setText(channel.toUpperCase(java.util.Locale.US));
        label.setTextSize(Style.LABEL);
        label.setTypeface(Typeface.MONOSPACE);
        label.setLetterSpacing(0.1f);
        line.addView(label);

        // The dot sits after the name rather than before it, so the three
        // names stay on one optical column whether or not they are playing.
        View dot = new View(ctx);
        GradientDrawable d = new GradientDrawable();
        d.setShape(GradientDrawable.OVAL);
        d.setColor(Style.accent(channel));
        dot.setBackground(d);
        LinearLayout.LayoutParams dotParams = new LinearLayout.LayoutParams(
                ChannelCard.dp(ctx, 6), ChannelCard.dp(ctx, 6));
        dotParams.leftMargin = ChannelCard.dp(ctx, Style.gap(1));
        dotParams.gravity = Gravity.CENTER_VERTICAL;
        // INVISIBLE, not GONE: a name that shifts sideways when its track
        // starts is a name you have to re-find.
        dot.setVisibility(View.INVISIBLE);
        line.addView(dot, dotParams);

        root.addView(line);

        View underline = new View(ctx);
        root.addView(underline, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ChannelCard.dp(ctx, 2)));

        return new Tab(root, label, dot, underline);
    }

    /** Draw {@code channel} as the selected one. */
    void select(String channel) {
        selected = channel;
        for (Map.Entry<String, Tab> e : tabs.entrySet()) {
            boolean on = e.getKey().equals(channel);
            Tab t = e.getValue();
            t.label.setTextColor(on ? Style.INK : Style.FAINT);
            t.label.setTypeface(Typeface.MONOSPACE, on ? Typeface.BOLD : Typeface.NORMAL);
            t.underline.setBackgroundColor(on ? Style.accent(e.getKey()) : 0x00000000);
        }
    }

    /** Show a dot on every channel that is playing. */
    void apply(Map<String, Channels.Channel> state) {
        for (Map.Entry<String, Tab> e : tabs.entrySet()) {
            Channels.Channel c = state == null ? null : state.get(e.getKey());
            e.getValue().dot.setVisibility(
                    c != null && c.playing ? View.VISIBLE : View.INVISIBLE);
        }
    }

    /** The channel currently drawn as selected. */
    String selected() {
        return selected;
    }
}
