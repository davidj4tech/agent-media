package net.agentmedia.companion;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

/**
 * Start the service and get out of the way.
 *
 * The revive door. Android stops this app whenever it likes — LOW_MEMORY, a
 * package update, an ANR — and since Automate was retired the app is the *only*
 * mic trigger, so every death is a hole in barge-in: the book and Sam talk over
 * David until something starts it again. `call_guard` does the noticing and
 * knocks on this door (`_DEFAULT_MIC_REVIVE_CMD`).
 *
 * It used to knock on {@link MainActivity}, which is a diagnostic screen: it
 * inflates a status view and an event log, then re-renders both twice a second.
 * That is a fine thing to open by hand and a poor thing to open under memory
 * pressure, because it runs on the same main thread the service's `onCreate`
 * has ten seconds to reach `startForeground` on — and on 2026-08-16 it did not
 * reach it three times in an hour.
 *
 * So: no window, no layout, no permission prompt. Start the service, finish
 * inside `onCreate`. An activity rather than a service target because Termux
 * lives under a different uid and Android's background-start rules let it
 * launch an exported activity of ours and not much else.
 *
 * Harmless to knock on when the app is already up: a second
 * `startForegroundService` on a running service is one `onStartCommand` with a
 * null action, which does nothing.
 */
public class WakeActivity extends Activity {

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        startForegroundService(new Intent(this, CompanionService.class));
        finish();
    }
}
