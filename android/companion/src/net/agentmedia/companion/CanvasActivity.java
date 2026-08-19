package net.agentmedia.companion;

import android.app.Activity;
import android.graphics.Typeface;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;

/**
 * The visual canvas, inside the app.
 *
 * <h4>A WebView, not native views</h4>
 *
 * The canvas is already a working client — {@code packages/visual}'s
 * canvas.{html,css,js}, an SSE-fed image surface with a tested mode/focus
 * state machine and a headless harness behind it. Redrawing that against
 * Canvas and ImageView would be weeks of work to arrive back where we started,
 * with the harness thrown away. So the page keeps supplying the pixels and the
 * app supplies the lifecycle, which is the half a browser tab does badly:
 *
 * <ul>
 *   <li>the screen can be woken when a figure arrives (phase 2 — a browser
 *       tab cannot do this at all, and it is the reason a {@code [[reveal:]]}
 *       marker only lands if you happen to be looking);</li>
 *   <li>the stream can be held open by a foreground service that survives
 *       doze, rather than a tab Android discards whenever it likes;</li>
 *   <li>no Vimium — {@code p} stops meaning "open the clipboard as a URL",
 *       which has cost real debugging time twice;</li>
 *   <li>one configuration: the app already knows which agent-media this phone
 *       is a client of, where the browser remembered a URL separately and let
 *       it drift.</li>
 * </ul>
 *
 * See docs/proposals/2026-08-18-canvas-in-the-app.md. This is phase 0 — the
 * spike that decides whether the rest is worth building. It is deliberately
 * thin: no config beyond the server host, no wake-on-arrival, no controls of
 * its own. What it is here to answer is whether the page works under WebView
 * at all, and whether its touch controls carry a surface that grew up with a
 * keyboard.
 *
 * <h4>Full-bleed, and no Edges</h4>
 *
 * Every other screen calls {@link Edges#fit} to keep out from under the status
 * bar. This one does the opposite on purpose: the page declares
 * {@code viewport-fit=cover} and handles its own safe areas in CSS, and a
 * canvas that a figure fills to the edges is the whole point. The system bars
 * are hidden and come back on a swipe.
 */
public class CanvasActivity extends Activity {

    /**
     * Where the canvas listens. Not a setting yet — phase 1 puts it beside the
     * control/music/speech/book ports in {@link Server}, once the spike has
     * said the rest is worth building.
     */
    private static final int CANVAS_PORT = 8781;

    /**
     * The host to fall back on when this phone has no server configured.
     *
     * The stored default is loopback, and the canvas is emphatically not on
     * the phone, so an unconfigured install would spend the spike failing to
     * connect for a reason that has nothing to do with what is being tested.
     * MagicDNS resolves the bare name on the tailnet. Phase 1 deletes this.
     */
    private static final String SPIKE_HOST = "red5";

    private FrameLayout root;
    private WebView web;
    private BackChevron back;
    private TextView trouble;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);

        // A canvas that sleeps mid-figure is worse than the browser tab it
        // replaces. Cleared with the activity, so it costs nothing elsewhere.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        root = new FrameLayout(this);
        root.setBackgroundColor(Style.GROUND);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        // The pairing token lives in localStorage (canvas.py's /pair installs
        // it there). Without DOM storage the device is unpaired on every
        // launch, and /input stays shut for good.
        s.setDomStorageEnabled(true);
        // The canvas plays video in step with the speech. A gesture
        // requirement would stall exactly the arrivals nobody is touching the
        // screen for.
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        web.setBackgroundColor(Style.GROUND);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                return false;   // the page redirects to itself after /pair
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest r,
                                        WebResourceError e) {
                // Only the main document: a missing image should not paint the
                // whole screen with an error.
                if (!r.isForMainFrame()) return;
                say(e.getDescription() + "\n" + r.getUrl());
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                say(null);
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));

        // A canvas with nothing on it is black, and so is a canvas that never
        // connected. Without this the two are indistinguishable, and the spike
        // cannot tell a WebView problem from a quiet afternoon.
        trouble = new TextView(this);
        trouble.setTextSize(Style.BODY);
        trouble.setTextColor(Style.WARN);
        trouble.setTypeface(Typeface.MONOSPACE);
        trouble.setGravity(Gravity.CENTER);
        trouble.setBackgroundColor(Style.SUNKEN);
        int pad = dp(Style.gap(4));
        trouble.setPadding(pad, pad, pad, pad);
        trouble.setVisibility(View.GONE);
        root.addView(trouble, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER));

        // The way out. The system bars are hidden, so the gesture bar is gone
        // with them and there is no inherited affordance for leaving — see
        // BackChevron for why it is drawn, and why it dims instead of hiding.
        back = new BackChevron(this);
        back.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });
        int touch = dp(Style.TOUCH);
        FrameLayout.LayoutParams backParams =
                new FrameLayout.LayoutParams(touch, touch, Gravity.TOP | Gravity.START);
        int margin = dp(Style.gap(3));
        backParams.leftMargin = margin;
        backParams.topMargin = margin;
        root.addView(back, backParams);
        // Even with the bars hidden a punch-hole camera is still there, and on
        // p8a it sits exactly where this does. Take the cutout, nothing else:
        // padding for bars that are not being shown would push it into the
        // picture for no reason.
        back.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override
            public android.view.WindowInsets onApplyWindowInsets(
                    View v, android.view.WindowInsets insets) {
                android.graphics.Insets cut = insets.getInsets(
                        android.view.WindowInsets.Type.displayCutout());
                FrameLayout.LayoutParams p =
                        (FrameLayout.LayoutParams) v.getLayoutParams();
                p.leftMargin = margin + cut.left;
                p.topMargin = margin + cut.top;
                v.setLayoutParams(p);
                return insets;
            }
        });

        setContentView(root);
        // After setContentView, never before: the window has no decor view
        // until the content is set, and PhoneWindow.getInsetsController()
        // dereferences it without checking — so an onCreate that hides the
        // bars first dies with an NPE before the WebView is ever reached.
        // Asking the view rather than the window is the same route Edges
        // takes, and that one returns null instead of throwing.
        hideBars();
        web.loadUrl(url());
    }

    /** The canvas URL for the server this phone is a client of. */
    private String url() {
        Server server = Settings.server(this);
        String host = server.local() ? SPIKE_HOST : server.host;
        return "http://" + host + ":" + CANVAS_PORT + "/";
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private void say(CharSequence problem) {
        if (problem == null) {
            trouble.setVisibility(View.GONE);
        } else {
            trouble.setText(problem);
            trouble.setVisibility(View.VISIBLE);
        }
    }

    private void hideBars() {
        if (root == null) return;
        WindowInsetsController bars = root.getWindowInsetsController();
        if (bars == null) return;
        bars.hide(android.view.WindowInsets.Type.systemBars());
        bars.setSystemBarsBehavior(
                WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideBars();
        back.wake();
        web.onResume();
    }

    @Override
    public void onWindowFocusChanged(boolean focused) {
        super.onWindowFocusChanged(focused);
        // The bars come back on their own: a transient swipe shows them, and
        // so does anything that takes focus away and hands it back. Hiding
        // once in onCreate leaves them sitting over the canvas afterwards.
        if (focused) hideBars();
    }

    @Override
    protected void onPause() {
        super.onPause();
        // Paused, not destroyed: coming back should not mean reconnecting the
        // stream and re-fetching the figure that is already on screen.
        web.onPause();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        web.destroy();
    }
}
