package net.agentmedia.sessionspike;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.view.KeyEvent;

/**
 * The classic media-button path. Run 1 of the spike omitted this entirely and
 * saw no events at all; MediaSession has historically needed an explicit
 * button receiver for keys to reach it while the app is backgrounded.
 *
 * Logged separately from MediaSession.Callback so run 2 can tell which layer
 * delivered — broadcast, session callback, both, or neither.
 */
public class SpikeButtonReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_MEDIA_BUTTON.equals(intent.getAction())) {
            SpikeService.log("RECEIVER got " + intent.getAction());
            return;
        }
        KeyEvent ev = intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent.class);
        if (ev == null) {
            SpikeService.log("RECEIVER media button, no KeyEvent");
            return;
        }
        SpikeService.log("RECEIVER " + KeyEvent.keyCodeToString(ev.getKeyCode())
                + " action=" + (ev.getAction() == KeyEvent.ACTION_DOWN ? "DOWN" : "UP"));
    }
}
