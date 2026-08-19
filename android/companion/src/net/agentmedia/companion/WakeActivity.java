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
 * inside `onCreate`.
 *
 * <b>And its own task affinity, which took longer to learn.</b> No window of
 * its own is not the same as not appearing: an activity started with NEW_TASK
 * under the app's default affinity raises the app's existing task, so a knock
 * brought whatever screen was last opened — usually the diagnostic one — to the
 * front. Silent revives were therefore visible ones, up to one every five
 * minutes, and David asked for it to stop on 2026-08-19. With {@code :wake}
 * affinity and {@code singleInstance} the knock lands in a task of its own,
 * finishes, and leaves the foreground alone. An activity rather than a service target because Termux
 * lives under a different uid and Android's background-start rules let it
 * launch an exported activity of ours and not much else.
 *
 * Knocking when the app is already up is harmless only because
 * `onStartCommand` posts the notification on every start. It was NOT harmless
 * while that call lived in `onCreate` alone: a start delivered to a running
 * service skips `onCreate`, so nothing satisfied this start's own obligation to
 * call `startForeground`, and Android killed the app ten seconds later. Five
 * deaths on 2026-08-17 name this line as the caller. Do not move that call back
 * into `onCreate` only.
 */
public class WakeActivity extends Activity {

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        startForegroundService(new Intent(this, CompanionService.class));
        finish();
    }
}
