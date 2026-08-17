package net.agentmedia.companion;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Window;
import android.widget.Toast;

/**
 * "Play with agent-media" in the Android share sheet.
 *
 * It takes the shared text, hands it to Termux over loopback (see
 * {@link ShareRequest}) and toasts what the far side decided — "Some Talk →
 * book (podcast): 90m long".
 *
 * <h4>One question first</h4>
 *
 * The far side classifies well, and when it is wrong it is wrong about the
 * sharer rather than about the link: the same hour-long upload is a DJ set on
 * the way to work and a lecture at a desk, and only one party to this knows
 * which. So the sheet asks — three items, "decide for me" first and one tap
 * away, then the two channels a link can land on. Speech is not among them;
 * nothing external is ever spoken.
 *
 * It still finishes the moment the work is handed off. Holding the activity
 * alive to show a result would put a dead window over whatever the sharer was
 * reading for as long as a metadata probe takes; a toast outlives the activity
 * and says the same thing.
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
        ask(text);
    }

    /** What a link can land on, and letting the far side pick. */
    private static final String[] CHOICES = {"decide for me", "music", "book"};
    private static final String[] CHANNELS = {"", "music", "book"};

    private void ask(final String text) {
        AlertDialog d = new AlertDialog.Builder(
                this, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle("play with agent-media")
                .setItems(CHOICES, new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface dlg, int which) {
                        send(text, CHANNELS[which]);
                    }
                })
                // Dismissed rather than chosen — back, or a tap outside. The
                // share is dropped silently: the sharer has just said no, and a
                // toast telling them so would be the app arguing about it.
                .setOnCancelListener(new DialogInterface.OnCancelListener() {
                    @Override public void onCancel(DialogInterface dlg) { finish(); }
                })
                .create();
        Window w = d.getWindow();
        if (w != null) {
            GradientDrawable bg = new GradientDrawable();
            bg.setColor(Style.SURFACE);
            bg.setCornerRadius(
                    Math.round(12 * getResources().getDisplayMetrics().density));
            w.setBackgroundDrawable(bg);
        }
        d.show();
    }

    private void send(final String text, final String channel) {
        // Off the main thread: this is a network round trip, and Android kills
        // an app that does one on the UI thread. The Handler hops the answer
        // back, because a Toast must be raised from a Looper thread.
        final Handler main = new Handler(Looper.getMainLooper());
        new Thread(new Runnable() {
            @Override public void run() {
                final ShareRequest.Result r = ShareRequest.send(
                        Settings.server(ShareActivity.this), text, channel);
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
