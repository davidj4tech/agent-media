package net.agentmedia.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.view.KeyEvent;

/**
 * The broadcast half of the media-button path.
 *
 * Buttons normally arrive at MediaSession.Callback and never come here; this
 * receiver exists because the session must name one (setMediaButtonBroadcastReceiver)
 * and because, when something does go wrong on a new headunit, "the key was
 * delivered as a broadcast but the session callback never fired" is a diagnosis
 * we can only make if this line appears in the log. It handles nothing itself.
 */
public class MediaButtonReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_MEDIA_BUTTON.equals(intent.getAction())) return;
        KeyEvent ev = intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent.class);
        CompanionService.log("broadcast: "
                + (ev == null ? "media button, no KeyEvent"
                              : KeyEvent.keyCodeToString(ev.getKeyCode())
                                + " " + (ev.getAction() == KeyEvent.ACTION_DOWN ? "DOWN" : "UP")));
    }
}
