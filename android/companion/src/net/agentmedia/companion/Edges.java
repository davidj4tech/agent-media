package net.agentmedia.companion;

import android.view.View;
import android.view.WindowInsets;

/**
 * Keep the content out from under the system bars.
 *
 * Android 15 draws every app edge-to-edge — the window now starts at the top of
 * the screen rather than under the status bar, and an app that was laying out
 * from y=0 loses its first line to the clock. On p8a that was the home screen's
 * title: "agent-media" and the build stamp sat behind the status bar, which is
 * how it was reported ("the top seems to be cut off, I can't see the title").
 *
 * The usual fix is a theme attribute, and there is no styles.xml here to put
 * one in — no AndroidX, no layout XML. So the insets are taken directly and
 * added to whatever padding the screen already asked for. That is also the more
 * honest version: the bars are a real thing the window has to make room for,
 * and each screen keeps its own padding.
 *
 * Applied to the root of each activity, once, in onCreate.
 */
final class Edges {

    private Edges() { }

    /**
     * Pad {@code root} by the system bars, on top of the padding it already has.
     *
     * The listener is left attached rather than run once: the insets change
     * when the phone rotates, when the keyboard opens, and when a call banner
     * appears, and a value read at build time would be the wrong one by then.
     */
    static void fit(final View root) {
        final int left = root.getPaddingLeft();
        final int top = root.getPaddingTop();
        final int right = root.getPaddingRight();
        final int bottom = root.getPaddingBottom();

        root.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override
            public WindowInsets onApplyWindowInsets(View v, WindowInsets insets) {
                android.graphics.Insets bars =
                        insets.getInsets(WindowInsets.Type.systemBars()
                                | WindowInsets.Type.displayCutout());
                v.setPadding(left + bars.left, top + bars.top,
                             right + bars.right, bottom + bars.bottom);
                return insets;
            }
        });
        root.requestApplyInsets();

        // And light icons in the bars, because the ground under them is ours
        // and it is nearly black. Without this the system picks from its own
        // theme, which on a phone set to light mode paints a dark clock on our
        // dark header — the same lost title by another route.
        android.view.WindowInsetsController bars = root.getWindowInsetsController();
        if (bars != null) {
            bars.setSystemBarsAppearance(0,
                    android.view.WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                            | android.view.WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS);
        }
    }
}
