package net.agentmedia.companion;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;

/**
 * "Play with agent-media" in the Android share sheet.
 *
 * No UI of its own: it takes the shared text, hands it to Termux over loopback
 * (see {@link ShareRequest}) and toasts what the far side decided — "Some Talk
 * → book (podcast): 90m long". The window never draws, so sharing feels like
 * the share sheet closing rather than an app opening.
 *
 * It finishes the moment the work is handed off, deliberately. Holding the
 * activity alive to show a result would put a dead window over whatever the
 * sharer was reading for as long as a metadata probe takes; a toast outlives
 * the activity and says the same thing.
 */
public class ShareActivity extends Activity {

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        final String text = sharedText(getIntent());
        if (text == null || text.trim().isEmpty()) {
            toast("agent-media: nothing shared");
            finish();
            return;
        }
        // Off the main thread: this is a network round trip, and Android kills
        // an app that does one on the UI thread. The Handler hops the answer
        // back, because a Toast must be raised from a Looper thread.
        final Handler main = new Handler(Looper.getMainLooper());
        new Thread(new Runnable() {
            @Override public void run() {
                final ShareRequest.Result r =
                        ShareRequest.send(ShareRequest.DEFAULT_PORT, text);
                main.post(new Runnable() {
                    @Override public void run() { toast(r.message); }
                });
            }
        }, "share-post").start();
        finish();
    }

    /**
     * The shared text, whatever shape the sending app chose.
     *
     * EXTRA_TEXT is the normal one; some apps send only EXTRA_SUBJECT, and a
     * few send the link as the intent's own data URI. Take whichever is there
     * — pulling the URL back out of it is the listener's job, not ours.
     */
    private static String sharedText(Intent i) {
        if (i == null) return null;
        CharSequence t = i.getCharSequenceExtra(Intent.EXTRA_TEXT);
        if (t != null && t.length() > 0) return t.toString();
        t = i.getCharSequenceExtra(Intent.EXTRA_SUBJECT);
        if (t != null && t.length() > 0) return t.toString();
        return i.getDataString();
    }

    private void toast(String message) {
        Toast.makeText(getApplicationContext(), message, Toast.LENGTH_LONG).show();
    }
}
