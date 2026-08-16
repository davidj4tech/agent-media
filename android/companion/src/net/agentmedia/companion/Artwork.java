package net.agentmedia.companion;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.drawable.Icon;

import java.util.HashMap;
import java.util.Map;

/**
 * The picture on a channel's card, drawn rather than shipped.
 *
 * <h4>Why the cards needed this</h4>
 *
 * All three sessions published no album art, the same stock
 * {@code ic_media_play} triangle, and the subtitle "agent-media". The shade
 * draws media cards art-first, so three channels arrived as three identical
 * grey tiles that differed only in a title — and that title was being scrolled
 * thirty characters at a time by our own marquee, so the one distinguishing
 * field was also the one that moved. "Which of these is the book" took reading,
 * on a surface whose whole point is that it does not.
 *
 * Android also tints a media card's background from its artwork, so this is the
 * cheapest change with the largest effect: give each session a picture and the
 * shade colours the whole card for us.
 *
 * <h4>Drawn, not shipped</h4>
 *
 * No PNGs, no vector drawables, no res/drawable at all. Partly the toolchain —
 * aapt2 and d8 and nothing else, so anything in res/ is one more thing to
 * regenerate — but mostly because the marks are three shapes a Canvas can make
 * exactly: bars for music, a settling utterance for speech, a bookmark for the
 * book. Geometry needs no font on the device to be present, which a glyph does;
 * an audiobook card falling back to a tofu box would be worse than no mark.
 *
 * Cached per channel: a bitmap for the card, an {@link Icon} for the status
 * bar. Both are asked for on every notification rebuild, and the cards rebuild
 * on every mpv property change.
 */
final class Artwork {

    /** The card picture, in px. Bigger than any shade draws; scaled down well. */
    private static final int ART = 256;
    /** The status-bar mark, in px. Tinted white by the system whatever we do. */
    private static final int GLYPH = 96;

    private static final Map<String, Bitmap> ART_CACHE = new HashMap<String, Bitmap>();
    private static final Map<String, Icon> ICON_CACHE = new HashMap<String, Icon>();

    private Artwork() { }

    /** The card picture for a channel: its gradient, with its mark on top. */
    static synchronized Bitmap art(String channel) {
        Bitmap cached = ART_CACHE.get(channel);
        if (cached != null) return cached;

        Bitmap bmp = Bitmap.createBitmap(ART, ART, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bmp);

        Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
        // Down the diagonal rather than straight down: a vertical gradient on a
        // square reads as a fade, a diagonal one reads as a surface with a
        // light on it, and the shade sits these next to real album covers.
        fill.setShader(new LinearGradient(0, 0, ART, ART,
                Style.accent(channel), Style.deep(channel), Shader.TileMode.CLAMP));
        canvas.drawRect(0, 0, ART, ART, fill);

        // The mark is punched in the ground colour, so it reads as a hole in
        // the tile rather than a sticker on it — and so it stays legible when
        // the shade tints the card behind it.
        Paint mark = new Paint(Paint.ANTI_ALIAS_FLAG);
        mark.setColor(Style.GROUND);
        drawMark(canvas, channel, ART, mark);
        ART_CACHE.put(channel, bmp);
        return bmp;
    }

    /**
     * The status-bar icon for a channel — the same mark, white on nothing.
     *
     * The small icon is a silhouette by the system's rules, so the channel
     * cannot be told by colour up there; the shape is all there is, which is
     * the other reason the marks are shapes.
     */
    static synchronized Icon icon(String channel) {
        Icon cached = ICON_CACHE.get(channel);
        if (cached != null) return cached;

        Bitmap bmp = Bitmap.createBitmap(GLYPH, GLYPH, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bmp);
        Paint mark = new Paint(Paint.ANTI_ALIAS_FLAG);
        mark.setColor(Color.WHITE);
        drawMark(canvas, channel, GLYPH, mark);

        Icon icon = Icon.createWithBitmap(bmp);
        ICON_CACHE.put(channel, icon);
        return icon;
    }

    // ---- the three marks -------------------------------------------------

    private static void drawMark(Canvas canvas, String channel, int size, Paint p) {
        // Everything is drawn inside the middle half of the square, so the same
        // routine serves a 256px tile and a 96px silhouette without a second
        // set of numbers.
        float box = size * 0.46f;
        float left = (size - box) / 2f;
        float top = (size - box) / 2f;
        float r = size * 0.018f;

        if ("music".equals(channel)) {
            // Three bars at three heights: a level meter, which is what a music
            // player looks like when it is reduced to three shapes.
            float w = box * 0.22f;
            float step = box * 0.39f;
            float[] heights = { 0.62f, 1.0f, 0.78f };
            for (int i = 0; i < 3; i++) {
                float h = box * heights[i];
                float x = left + i * step;
                canvas.drawRoundRect(new RectF(x, top + box - h, x + w, top + box),
                                     r, r, p);
            }
        } else if ("speech".equals(channel)) {
            // Three lines, each shorter than the last: an utterance ending.
            // Read left-aligned, like the text it stands for.
            float h = box * 0.19f;
            float step = box * 0.405f;
            float[] widths = { 1.0f, 0.72f, 0.44f };
            for (int i = 0; i < 3; i++) {
                float y = top + i * step;
                canvas.drawRoundRect(
                        new RectF(left, y, left + box * widths[i], y + h), r, r, p);
            }
        } else {
            // A bookmark: a ribbon with a notch cut out of the foot.
            //
            // The first version was two panels with a gutter — a book seen from
            // above — and at card size it was a pause button with a gap in it,
            // on a card that also has a real pause button eight millimetres to
            // the right. A bookmark says the same thing (a place kept in
            // something long) in a silhouette nothing else in a media notification
            // shares.
            float w = box * 0.62f;
            float x = left + (box - w) / 2f;
            float notch = box * 0.26f;
            Path ribbon = new Path();
            ribbon.moveTo(x, top);
            ribbon.lineTo(x + w, top);
            ribbon.lineTo(x + w, top + box);
            ribbon.lineTo(x + w / 2f, top + box - notch);
            ribbon.lineTo(x, top + box);
            ribbon.close();
            canvas.drawPath(ribbon, p);
        }
    }
}
