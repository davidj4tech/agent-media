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
 * See docs/proposals/2026-08-18-canvas-in-the-app.md. Phase 0 answered the
 * questions that could sink this — the page renders, the touch ring carries,
 * cleartext is one config away — so this is phase 1: the canvas is addressed
 * from {@link Server} rather than a hardcoded hostname, and the input box is
 * usable, which on a phone means the soft keyboard.
 *
 * <h4>The keyboard</h4>
 *
 * The canvas is not only a picture. {@code POST /input} types into the pane
 * that last spoke, and the page's focus ring has an input mode for exactly
 * that — which is the whole reason to carry the canvas onto the phone rather
 * than only onto the wall. But this window hides the system bars and draws to
 * every edge, and a window like that gets no automatic help when the IME
 * arrives: the keyboard covers the bottom of the page, which is precisely
 * where the input box is.
 *
 * So the IME inset is applied by hand — as a bottom MARGIN on the WebView,
 * which is the part that matters. Padding was the first attempt and it does
 * not work: it offsets what the WebView draws without shrinking the viewport
 * the page lays out against, so every {@code 100vh} layer stays full height
 * and a bottom-pinned dock sits behind the keyboard exactly as before. A
 * margin makes the view itself shorter, the viewport follows, and the dock
 * rides above the IME. Confirmed on p8a on 2026-08-19, against the padding
 * version failing on the same screen — this is not a theory about viewports.
 *
 * {@code adjustResize} in the manifest is what makes the inset animate rather
 * than jump; {@code setDecorFitsSystemWindows(false)} is what stops the
 * framework consuming it before this listener is reached.
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
     * Open a path other than the page — {@code "/pair?c=<code>"}, the one-time
     * exchange that puts the amux token in this device's localStorage and lets
     * its input box send. Settings hands it over; nothing else sets it.
     */
    static final String EXTRA_PATH = "net.agentmedia.companion.CANVAS_PATH";

    /**
     * How long away before coming back re-fetches the page.
     *
     * The WebView is paused rather than destroyed, so returning does not
     * reconnect the stream or re-fetch a figure already on screen — but the
     * cost of that is a page loaded once and kept forever. canvas.py serves
     * the page from memory at startup, so a canvas restarted behind us serves
     * something this screen will never see: on 2026-08-19 that looked exactly
     * like the subtitle work having been lost, and it was not. A browser tab
     * has pull-to-refresh for this; nothing here did.
     *
     * Ten minutes is chosen to be longer than glancing away and shorter than
     * the gap across which a deploy is plausible.
     */
    private static final long STALE_MS = 10 * 60 * 1000L;

    /**
     * The page digest this WebView loaded, as reported by /pageid.
     *
     * Belt to the page's own braces, and the belt is the load-bearing one. The
     * canvas announces a new page over SSE and a running page reloads itself —
     * but only a page that already CONTAINS that handler, which is no use to
     * the WebView that has been holding a document from before it existed. It
     * would sit there forever, and did: "it's reverted back again" was this,
     * after the SSE fix had already shipped and been verified.
     *
     * So the app asks directly, and its answer does not depend on the vintage
     * of what it is currently showing.
     */
    private String pageId;

    private long leftAt;
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
                // Stamp what we just loaded, rather than whatever the first
                // resume happens to see. The difference bit: pageId was being
                // recorded on the first resume, which fires before the load
                // finishes — so a canvas restarted in that window was adopted
                // as "ours" and the screen sat on the old page believing it
                // was current.
                pageId = null;
                checkPage();
            }
        });
        // The keyboard. Nothing else in this window makes room for it: the
        // system bars are hidden and the page is drawn to every edge, so an
        // IME simply covers the bottom of the canvas — where the input box is.
        //
        // Padding was the first attempt, and it left the input box under the
        // keyboard anyway (David, 2026-08-19). A WebView's padding offsets the
        // content it draws; it does not necessarily shrink the viewport the
        // page is laid out against, so `100vh` layers stay full height and a
        // dock pinned to the bottom stays pinned to a bottom that is now
        // behind the IME. Shortening the view itself is unambiguous: a smaller
        // WebView is a smaller viewport, and the page's own safe-area CSS then
        // measures the space that is actually visible.
        web.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override
            public android.view.WindowInsets onApplyWindowInsets(
                    View v, android.view.WindowInsets insets) {
                int ime = insets.getInsets(
                        android.view.WindowInsets.Type.ime()).bottom;
                ViewGroup.LayoutParams lp = v.getLayoutParams();
                if (lp instanceof FrameLayout.LayoutParams) {
                    FrameLayout.LayoutParams f = (FrameLayout.LayoutParams) lp;
                    if (f.bottomMargin != ime) {
                        f.bottomMargin = ime;
                        v.setLayoutParams(f);
                    }
                }
                // Returned unconsumed: the back chevron reads the cutout out
                // of the same pass, and consuming here would leave it under a
                // punch-hole camera the moment a keyboard appeared.
                return insets;
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
        // The manual reload. A long press because the canvas is a picture
        // surface with one visible control, and adding a second button for
        // something needed twice a month would cost the picture more than it
        // is worth. Returning true stops it also being read as a back press.
        back.setOnLongClickListener(new View.OnLongClickListener() {
            @Override public boolean onLongClick(View v) {
                v.performHapticFeedback(
                        android.view.HapticFeedbackConstants.LONG_PRESS);
                reload();
                return true;
            }
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

        // Without this the framework fits the content to the system windows
        // and consumes the IME inset on the way, so the listener above never
        // sees a keyboard. It is also what lets the page draw under the
        // cutout, which is the look this screen is for.
        getWindow().setDecorFitsSystemWindows(false);

        setContentView(root);
        // After setContentView, never before: the window has no decor view
        // until the content is set, and PhoneWindow.getInsetsController()
        // dereferences it without checking — so an onCreate that hides the
        // bars first dies with an NPE before the WebView is ever reached.
        // Asking the view rather than the window is the same route Edges
        // takes, and that one returns null instead of throwing.
        hideBars();

        // A canvas that was never going to connect should say so, not sit
        // there black. The commonest case by far is an install that has never
        // opened Settings, still pointing at loopback, where no canvas listens
        // and none ever will.
        String problem = Settings.server(this).canvasProblem();
        if (problem != null) {
            say(problem);
        } else {
            web.loadUrl(url());
        }
    }

    /** The canvas URL for the server this phone is a client of. */
    private String url() {
        String path = getIntent() == null ? null
                : getIntent().getStringExtra(EXTRA_PATH);
        return Settings.server(this).canvasUrl(path);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    /**
     * Ask the canvas which page it is serving, and reload if it is not ours.
     *
     * Off the main thread and deliberately forgiving: an unreachable canvas,
     * an older one with no /pageid, a timeout — every one of them means "no
     * reason to reload", never an error on screen. The screen already says
     * when it cannot reach the canvas at all.
     */
    private void checkPage() {
        final String url = Settings.server(CanvasActivity.this).canvasUrl("/pageid");
        new Thread(new Runnable() {
            @Override public void run() {
                final String got = fetch(url);
                if (got == null || got.isEmpty()) return;
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        if (pageId == null) { pageId = got; return; }
                        if (!pageId.equals(got)) { pageId = got; reload(); }
                    }
                });
            }
        }).start();
    }

    private static String fetch(String url) {
        java.net.HttpURLConnection c = null;
        try {
            c = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
            c.setConnectTimeout(2500);
            c.setReadTimeout(2500);
            if (c.getResponseCode() != 200) return null;
            java.io.BufferedReader r = new java.io.BufferedReader(
                    new java.io.InputStreamReader(c.getInputStream()));
            String line = r.readLine();
            r.close();
            return line == null ? null : line.trim();
        } catch (Exception e) {           // unreachable, no /pageid, timeout
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** Fetch the page again — the deliberate version of what checkPage decides. */
    private void reload() {
        say(null);
        web.loadUrl(url());
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
        // Every resume, not only after a long absence: the check is one small
        // request against a loopback-or-tailnet host, and STALE_MS was always
        // a guess standing in for the question this actually asks.
        checkPage();
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
        leftAt = System.currentTimeMillis();
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
