package net.agentmedia.companion;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.view.View;

/**
 * The way out of a full-bleed screen.
 *
 * {@link CanvasActivity} hides the system bars, which takes the gesture bar
 * with them — so the one affordance every other screen inherits for free is
 * not there, and "how do I get out of this" becomes a real question with a
 * blind swipe for an answer. This is the answer instead.
 *
 * <h4>Drawn, not a glyph</h4>
 *
 * No {@code ‹}, no emoji, no PNG at three densities. canvas.html made the same
 * call for the same reason — a text chevron is a different shape in every
 * system font, and a pictograph arrives coloured. A Path at a 2.2dp round-cap
 * stroke matches the page's own icon set exactly, and it is sharp at any size
 * because there is no bitmap involved.
 *
 * <h4>It dims rather than disappears</h4>
 *
 * Nothing is allowed to write on the picture — the reply dock and the agents
 * pill both ride clear of it — and a control parked at full strength over the
 * top-left corner of every figure would break that. But a control that fades
 * out entirely has to be summoned back, and the only gesture available for
 * summoning is a tap, which belongs to the page: the canvas has its own touch
 * controller, and stealing taps to reveal a button would cost a control to
 * gain one.
 *
 * So it settles to a quarter alpha and stays there. Legible when looked for,
 * close to nothing when not, and it never takes a tap that was meant for the
 * canvas.
 */
final class BackChevron extends View {

    /** How long it stays at full strength after arriving or being touched. */
    private static final long BRIGHT_MS = 2500;
    /** What it settles to: present, not asserting. */
    private static final float DIM = 0.25f;
    private static final long FADE_MS = 600;

    private final Paint scrim = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint stroke = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path chevron = new Path();

    private final Runnable settle = new Runnable() {
        @Override public void run() {
            animate().alpha(DIM).setDuration(FADE_MS).start();
        }
    };

    BackChevron(Context context) {
        super(context);

        // A disc dark enough to carry the stroke over a bright figure. Not
        // Style.SUNKEN: this one has to be translucent, and the picture under
        // it should still read.
        scrim.setColor(Color.argb(0x8A, 0x08, 0x09, 0x0B));
        scrim.setStyle(Paint.Style.FILL);

        stroke.setColor(Style.INK);
        stroke.setStyle(Paint.Style.STROKE);
        stroke.setStrokeCap(Paint.Cap.ROUND);
        stroke.setStrokeJoin(Paint.Join.ROUND);
        stroke.setStrokeWidth(2.2f * getResources().getDisplayMetrics().density);

        setClickable(true);
        setFocusable(true);
        setContentDescription("Back");
    }

    /** Bring it up to full, then let it settle again. */
    void wake() {
        removeCallbacks(settle);
        animate().alpha(1f).setDuration(120).start();
        postDelayed(settle, BRIGHT_MS);
    }

    @Override
    protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        wake();
    }

    @Override
    protected void onDetachedFromWindow() {
        super.onDetachedFromWindow();
        removeCallbacks(settle);
    }

    @Override
    protected void onSizeChanged(int w, int h, int oldW, int oldH) {
        super.onSizeChanged(w, h, oldW, oldH);
        // Proportions, so the shape is the same at whatever size it is given.
        chevron.reset();
        chevron.moveTo(w * 0.58f, h * 0.30f);
        chevron.lineTo(w * 0.40f, h * 0.50f);
        chevron.lineTo(w * 0.58f, h * 0.70f);
    }

    @Override
    protected void onDraw(Canvas c) {
        float r = Math.min(getWidth(), getHeight()) / 2f;
        c.drawCircle(getWidth() / 2f, getHeight() / 2f, r, scrim);
        c.drawPath(chevron, stroke);
    }
}
