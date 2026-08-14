package net.agentmedia.companion;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.os.Handler;

/**
 * The android.* half of audio focus: request it, abandon it, forward the
 * callbacks. All the deciding lives in {@link FocusPolicy}, which imports
 * nothing from android.* so it can be tested on the build host.
 *
 * <h4>setWillPauseWhenDucked, and why it is not optional</h4>
 *
 * From API 26 the framework ducks the focus loser's own players itself and
 * <em>does not call the listener</em>. Left at the default, this app would
 * never see {@code AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK}: Android would duck our
 * stream of zeros — a no-op — and mpv would play straight through the
 * navigation prompt at full volume. AudioFocusRequest's own javadoc:
 *
 * <blockquote>If your application requires pausing instead of ducking for any
 * other reason than playing speech, you can also declare so with
 * {@code Builder#setWillPauseWhenDucked(boolean)}, which will cause the system
 * to call your focus listener instead of automatically ducking.</blockquote>
 *
 * The flag buys us the callback, not an obligation: having been told, we are
 * free to duck mpv rather than pause it, which is what we do.
 */
final class FocusControl {

    interface Callback {
        /** One of FocusPolicy's GAIN/LOSS constants. Delivered on the handler. */
        void onFocusChange(int change);
    }

    private final AudioManager am;
    private final AudioFocusRequest request;
    private boolean held = false;

    FocusControl(Context ctx, Handler handler, final Callback cb) {
        assertConstantsMatch();
        am = ctx.getSystemService(AudioManager.class);

        // Matching the silent AudioTrack's attributes: one app, one kind of
        // output, so the system's idea of what we are playing agrees with the
        // stream it can actually see.
        AudioAttributes attrs = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build();

        request = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(attrs)
                .setWillPauseWhenDucked(true)
                .setOnAudioFocusChangeListener(new AudioManager.OnAudioFocusChangeListener() {
                    @Override public void onAudioFocusChange(int change) {
                        cb.onFocusChange(change);
                    }
                }, handler)
                .build();
    }

    boolean held() {
        return held;
    }

    /**
     * Take focus, if we do not already hold it. Returns true when we hold it
     * afterwards.
     *
     * This is the app's first outward-facing act: an AUDIOFOCUS_GAIN tells
     * whatever else is playing to stop. That is the intent — mpv is the player
     * and should own the output — but it is a real change in how the phone
     * behaves towards other apps.
     */
    boolean request() {
        if (held) return true;
        int result = am.requestAudioFocus(request);
        held = (result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED);
        return held;
    }

    void abandon() {
        if (!held) return;
        held = false;
        am.abandonAudioFocusRequest(request);
    }

    /** Human-readable, for the on-screen log — there is no adb on this phone. */
    static String name(int change) {
        switch (change) {
            case FocusPolicy.GAIN: return "GAIN";
            case FocusPolicy.LOSS: return "LOSS";
            case FocusPolicy.LOSS_TRANSIENT: return "LOSS_TRANSIENT";
            case FocusPolicy.LOSS_TRANSIENT_CAN_DUCK: return "LOSS_TRANSIENT_CAN_DUCK";
            default: return "change(" + change + ")";
        }
    }

    /**
     * FocusPolicy duplicates AudioManager's constants so it can be compiled
     * without android.jar. The duplication needs a tripwire, and this is it.
     */
    private static void assertConstantsMatch() {
        check(FocusPolicy.GAIN, AudioManager.AUDIOFOCUS_GAIN, "GAIN");
        check(FocusPolicy.LOSS, AudioManager.AUDIOFOCUS_LOSS, "LOSS");
        check(FocusPolicy.LOSS_TRANSIENT,
              AudioManager.AUDIOFOCUS_LOSS_TRANSIENT, "LOSS_TRANSIENT");
        check(FocusPolicy.LOSS_TRANSIENT_CAN_DUCK,
              AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK, "LOSS_TRANSIENT_CAN_DUCK");
    }

    private static void check(int ours, int theirs, String what) {
        if (ours != theirs) {
            throw new IllegalStateException(
                    "FocusPolicy." + what + " is " + ours + ", AudioManager says " + theirs);
        }
    }
}
