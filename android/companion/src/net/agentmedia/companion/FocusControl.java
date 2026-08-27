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

    /** What we are holding: nothing, the music claim, or the speech claim. */
    static final int NONE = 0;
    static final int MUSIC = 1;
    static final int SPEECH = 2;
    /**
     * Speech, asking the other app to turn down rather than stop.
     *
     * For the voice-session case: Claude Live answers an ordinary transient
     * claim by pausing itself and putting "Tap to resume" on the screen, which
     * costs a thumb every time Sam says anything. MAY_DUCK asks the framework
     * for the gentler treatment, and {@code setWillPauseWhenDucked(false)} is
     * the half that matters — it lets Android duck the other app itself instead
     * of telling it to pause.
     */
    static final int SPEECH_DUCK = 3;

    private final AudioManager am;
    /** AUDIOFOCUS_GAIN — music: mpv is the player and owns the output. */
    private final AudioFocusRequest musicRequest;
    /** AUDIOFOCUS_GAIN_TRANSIENT — speech: borrow the output, give it back. */
    private final AudioFocusRequest speechRequest;
    /** The SPEECH_DUCK claim; see the constant. */
    private final AudioFocusRequest speechDuckRequest;
    private int held = NONE;

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

        AudioManager.OnAudioFocusChangeListener listener =
                new AudioManager.OnAudioFocusChangeListener() {
                    @Override public void onAudioFocusChange(int change) {
                        cb.onFocusChange(change);
                    }
                };

        musicRequest = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(attrs)
                .setWillPauseWhenDucked(true)
                .setOnAudioFocusChangeListener(listener, handler)
                .build();

        // Transient, and that is the whole difference. A spoken reply is two
        // seconds of borrowing: AUDIOFOCUS_GAIN would stop the listener's
        // podcast for good and Android would never start it again, which is a
        // heavy price for a sentence. Transient asks the same players to hold,
        // and hands the output back when we abandon it.
        speechRequest = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                .setAudioAttributes(attrs)
                .setWillPauseWhenDucked(true)
                .setOnAudioFocusChangeListener(listener, handler)
                .build();

        speechDuckRequest = new AudioFocusRequest.Builder(
                        AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                .setAudioAttributes(attrs)
                .setWillPauseWhenDucked(false)
                .setOnAudioFocusChangeListener(listener, handler)
                .build();
    }

    /** The claim for a kind. One place, because abandon must undo what request did. */
    private AudioFocusRequest requestFor(int kind) {
        if (kind == MUSIC) return musicRequest;
        if (kind == SPEECH_DUCK) return speechDuckRequest;
        return speechRequest;
    }

    boolean held() {
        return held != NONE;
    }

    /** MUSIC, SPEECH or NONE — for the readout, and to decide a swap. */
    int kind() {
        return held;
    }

    /**
     * Take focus of the given kind, swapping if we hold the other. Returns true
     * when we hold it afterwards.
     *
     * This is the app's first outward-facing act: an AUDIOFOCUS_GAIN tells
     * whatever else is playing to stop. That is the intent for music — mpv is
     * the player and should own the output — but it is a real change in how the
     * phone behaves towards other apps, which is why speech asks for less.
     *
     * The music claim wins a tie, so a reply spoken over our own music does not
     * downgrade a claim we already hold; see CompanionService.
     */
    boolean request(int kind) {
        if (kind == NONE) return false;
        if (held == kind) return true;
        if (held != NONE) abandon();
        int result = am.requestAudioFocus(requestFor(kind));
        held = (result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED) ? kind : NONE;
        return held == kind;
    }

    /**
     * Android has taken focus away for good (AUDIOFOCUS_LOSS). The request we
     * registered is dead — the framework will send us nothing more through it —
     * so the bookkeeping has to agree, or the app believes it still holds focus
     * and never asks again.
     *
     * That is not hypothetical: on p8a on 2026-08-15 the YouTube app took the
     * output at 09:09:01 and the app went deaf for the rest of the session,
     * reporting focus_held=true with focus_events frozen at that LOSS. Every
     * later interruption arrived at nobody.
     */
    void lost() {
        abandon();
    }

    void abandon() {
        if (held == NONE) return;
        AudioFocusRequest req = requestFor(held);
        held = NONE;
        am.abandonAudioFocusRequest(req);
    }

    static String kindName(int kind) {
        return kind == MUSIC ? "music" : kind == SPEECH ? "speech"
             : kind == SPEECH_DUCK ? "speech-duck" : "none";
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

        // RingerState duplicates AudioManager's ringer modes and
        // NotificationManager's interruption filters, same reason. Worth the
        // tripwire twice over here: a silently wrong constant does not crash,
        // it withholds somebody's morning alerts.
        check(RingerState.RINGER_SILENT, AudioManager.RINGER_MODE_SILENT,
              "RINGER_SILENT");
        check(RingerState.RINGER_VIBRATE, AudioManager.RINGER_MODE_VIBRATE,
              "RINGER_VIBRATE");
        check(RingerState.RINGER_NORMAL, AudioManager.RINGER_MODE_NORMAL,
              "RINGER_NORMAL");
        check(RingerState.FILTER_UNKNOWN,
              android.app.NotificationManager.INTERRUPTION_FILTER_UNKNOWN,
              "FILTER_UNKNOWN");
        check(RingerState.FILTER_ALL,
              android.app.NotificationManager.INTERRUPTION_FILTER_ALL,
              "FILTER_ALL");
        check(RingerState.FILTER_PRIORITY,
              android.app.NotificationManager.INTERRUPTION_FILTER_PRIORITY,
              "FILTER_PRIORITY");
        check(RingerState.FILTER_NONE,
              android.app.NotificationManager.INTERRUPTION_FILTER_NONE,
              "FILTER_NONE");
        check(RingerState.FILTER_ALARMS,
              android.app.NotificationManager.INTERRUPTION_FILTER_ALARMS,
              "FILTER_ALARMS");

        // ButtonPolicy duplicates KeyEvent's codes for the same reason.
        check(ButtonPolicy.KEYCODE_MEDIA_PLAY_PAUSE,
              android.view.KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, "KEYCODE_MEDIA_PLAY_PAUSE");
        check(ButtonPolicy.KEYCODE_MEDIA_PLAY,
              android.view.KeyEvent.KEYCODE_MEDIA_PLAY, "KEYCODE_MEDIA_PLAY");
        check(ButtonPolicy.KEYCODE_MEDIA_PAUSE,
              android.view.KeyEvent.KEYCODE_MEDIA_PAUSE, "KEYCODE_MEDIA_PAUSE");
    }

    private static void check(int ours, int theirs, String what) {
        if (ours != theirs) {
            throw new IllegalStateException(
                    "FocusPolicy." + what + " is " + ours + ", AudioManager says " + theirs);
        }
    }
}
