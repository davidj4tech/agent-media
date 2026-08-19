package net.agentmedia.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * The revive door that opens no window at all.
 *
 * {@link WakeActivity} works and is still here, but an activity cannot be
 * started without touching the activity stack: it makes a task, that task goes
 * above the launcher, and when it finishes the system resumes the topmost
 * standard task rather than whatever was actually in front. Measured on p8a on
 * 2026-08-19 — launcher in front, knock, and Termux (the caller) came forward.
 * Nothing of ours was raised, which is what {@code b9d6939}'s task affinity
 * fixed, but the foreground still moved, and a revive is meant to be invisible.
 *
 * A broadcast has no task. Nothing is raised, nothing is resumed, and the
 * knock costs the screen nothing.
 *
 * <b>The catch, and why the activity stays.</b> Since Android 12 an app in the
 * background may not start a foreground service unless an exemption applies —
 * an activity in a task on Recents is one, a battery-optimisation exemption is
 * another, and this app has neither reliably. So the start can be refused, and
 * a refused revive is worse than a visible one: barge-in stays dead and nothing
 * says so. call_guard therefore knocks here first and falls back to the
 * activity if the readout is still not answering. See its
 * {@code _DEFAULT_MIC_REVIVE_CMD}.
 *
 * Exported because the knock comes from Termux, which is another uid. What it
 * grants any app on the device is "start agent-media's own service", which is
 * what the launcher icon does too.
 */
public class WakeReceiver extends BroadcastReceiver {

    /** What call_guard broadcasts. Also spelled out in the manifest. */
    static final String ACTION = "net.agentmedia.companion.WAKE";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !ACTION.equals(intent.getAction())) return;
        try {
            context.startForegroundService(new Intent(context, CompanionService.class));
            CompanionService.log("wake: started by broadcast");
        } catch (Throwable e) {
            // ForegroundServiceStartNotAllowedException on a device where no
            // exemption applies. Logged rather than thrown: the process would
            // die for it, and the fallback knock is already on its way.
            CompanionService.log("wake: broadcast could not start the service: " + e);
        }
    }
}
