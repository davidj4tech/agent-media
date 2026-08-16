package net.agentmedia.companion;

import android.content.Context;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * One channel, drawn the same way everywhere.
 *
 * The card in the shade and the block on the home screen were two different
 * things describing the same track — different type, different colours, one
 * with artwork and one with a bare title — and the app asks you to move between
 * them constantly: glance at the shade, open the app to seek, glance again. So
 * there is one component, at two sizes.
 *
 * <b>Row</b> is a channel you are not driving: mark, title, second line.
 * <b>Driver</b> is the one you are: the same, larger, with a progress bar and a
 * clock under it. Same mark, same hue, same order of information — what changes
 * is how much of it there is.
 *
 * Hand-built views, like everything here: no AndroidX, no layout XML. The
 * builder returns the root and hangs the pieces off it by tag, so a caller can
 * update a card in place without rebuilding the view tree twice a second.
 */
final class ChannelCard {

    /** The pieces of a built card, kept so the poll can update them in place. */
    static final class Views {
        final LinearLayout root;
        final ImageView art;
        final TextView title;
        final TextView subtitle;
        final View barTrack;
        final View barFill;
        final TextView clock;

        Views(LinearLayout root, ImageView art, TextView title, TextView subtitle,
              View barTrack, View barFill, TextView clock) {
            this.root = root;
            this.art = art;
            this.title = title;
            this.subtitle = subtitle;
            this.barTrack = barTrack;
            this.barFill = barFill;
            this.clock = clock;
        }
    }

    private ChannelCard() { }

    /**
     * Build a card for {@code channel}.
     *
     * @param driver true for the channel being driven — bigger mark, progress
     *               bar and clock; false for a row you can tap to promote.
     */
    static Views build(Context ctx, String channel, boolean driver) {
        int accent = Style.accent(channel);
        int pad = dp(ctx, Style.gap(3));
        int mark = dp(ctx, driver ? 56 : 34);

        LinearLayout root = new LinearLayout(ctx);
        root.setOrientation(LinearLayout.HORIZONTAL);
        root.setGravity(Gravity.CENTER_VERTICAL);
        root.setPadding(pad, pad, pad, pad);
        root.setBackground(surface(ctx, driver ? accent : 0));
        // A row is a target, and a target below the thumb minimum is a target
        // that gets missed at arm's length in the dark.
        root.setMinimumHeight(dp(ctx, Style.TOUCH));

        ImageView art = new ImageView(ctx);
        art.setImageBitmap(Artwork.art(channel));
        LinearLayout.LayoutParams artParams = new LinearLayout.LayoutParams(mark, mark);
        artParams.rightMargin = dp(ctx, Style.gap(3));
        root.addView(art, artParams);

        LinearLayout lines = new LinearLayout(ctx);
        lines.setOrientation(LinearLayout.VERTICAL);

        TextView title = new TextView(ctx);
        title.setTextSize(driver ? Style.TITLE : Style.HEAD);
        title.setTextColor(Style.INK);
        title.setTypeface(driver ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
        title.setSingleLine(true);
        title.setEllipsize(android.text.TextUtils.TruncateAt.END);
        lines.addView(title);

        TextView subtitle = new TextView(ctx);
        subtitle.setTextSize(Style.BODY);
        subtitle.setTextColor(Style.MUTED);
        subtitle.setSingleLine(true);
        subtitle.setEllipsize(android.text.TextUtils.TruncateAt.END);
        lines.addView(subtitle);

        View barTrack = null;
        View barFill = null;
        TextView clock = null;
        if (driver) {
            // The bar is two plain views rather than a ProgressBar: a
            // ProgressBar drags in the platform theme's accent, which is not
            // one of ours, and cannot be told otherwise without a style
            // resource — which is the thing this app does not have.
            LinearLayout track = new LinearLayout(ctx);
            track.setOrientation(LinearLayout.HORIZONTAL);
            track.setBackground(bar(ctx, Style.RULE));
            LinearLayout.LayoutParams trackParams = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, dp(ctx, 3));
            trackParams.topMargin = dp(ctx, Style.gap(2));
            View fill = new View(ctx);
            fill.setBackground(bar(ctx, accent));
            track.addView(fill, new LinearLayout.LayoutParams(0, dp(ctx, 3), 1f));
            lines.addView(track, trackParams);
            barTrack = track;
            barFill = fill;

            clock = new TextView(ctx);
            clock.setTextSize(Style.LABEL);
            clock.setTextColor(Style.FAINT);
            clock.setTypeface(Typeface.MONOSPACE);
            clock.setPadding(0, dp(ctx, Style.gap(1)), 0, 0);
            lines.addView(clock);
        }

        root.addView(lines, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        if (!driver) {
            // A row that takes the wheel has to look like it does something.
            // The rows were tappable from the day the two screens merged and
            // nothing on screen said so, which is a control you have to be told
            // about — asked twice on 2026-08-17, which is twice more than a
            // control should need.
            TextView chevron = new TextView(ctx);
            chevron.setText("›");
            chevron.setTextSize(Style.TITLE);
            chevron.setTextColor(Style.FAINT);
            chevron.setPadding(dp(ctx, Style.gap(2)), 0, 0, 0);
            root.addView(chevron);
        }
        return new Views(root, art, title, subtitle, barTrack, barFill, clock);
    }

    /**
     * Put a channel's current state into a built card.
     *
     * {@code c} may be null — the listener answers with what it could read, and
     * a channel it could not read should cost its own card, not the screen.
     */
    static void apply(Views v, String channel, Channels.Channel c) {
        if (c == null) {
            v.title.setText("…");
            v.subtitle.setText("");
            if (v.barTrack != null) v.barTrack.setVisibility(View.INVISIBLE);
            if (v.clock != null) v.clock.setText("");
            return;
        }
        v.title.setText(c.heading());
        v.title.setAlpha(c.idle ? 0.55f : 1f);
        v.art.setAlpha(c.idle ? 0.4f : 1f);
        v.subtitle.setText(c.detail());

        if (v.barTrack != null) {
            float f = c.progress();
            v.barTrack.setVisibility(f < 0 ? View.INVISIBLE : View.VISIBLE);
            if (f >= 0 && v.barFill != null) {
                LinearLayout.LayoutParams lp =
                        (LinearLayout.LayoutParams) v.barFill.getLayoutParams();
                lp.weight = Math.max(0.001f, f);
                v.barFill.setLayoutParams(lp);
                ((View) v.barFill.getParent()).requestLayout();
            }
        }
        if (v.clock != null) v.clock.setText(c.clock());
    }

    // ---- the shapes, since there is no drawable resource to point at --------

    /** A card's ground: the surface colour, rounded, with an optional edge. */
    private static GradientDrawable surface(Context ctx, int edge) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(Style.SURFACE);
        d.setCornerRadius(dp(ctx, 10));
        if (edge != 0) {
            // The driving channel wears its own hue as a hairline. Enough to
            // say "this one", not enough to compete with the artwork.
            d.setStroke(dp(ctx, 1), withAlpha(edge, 0x99));
        }
        return d;
    }

    private static GradientDrawable bar(Context ctx, int colour) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(colour);
        d.setCornerRadius(dp(ctx, 2));
        return d;
    }

    static int withAlpha(int colour, int alpha) {
        return Color.argb(alpha, Color.red(colour), Color.green(colour),
                          Color.blue(colour));
    }

    static int dp(Context ctx, int value) {
        return Math.round(value * ctx.getResources().getDisplayMetrics().density);
    }
}
