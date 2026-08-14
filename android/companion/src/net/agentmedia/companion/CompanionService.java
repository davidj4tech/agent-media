package net.agentmedia.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.view.KeyEvent;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * The whole app: publish one MediaSession for the music channel, and drive the
 * Termux mpv underneath it over loopback TCP.
 *
 * It plays nothing. The only audio it opens is a stream of zeros, and that
 * exists solely because Android will not hand the Bluetooth addressed-player
 * slot to a session with no open stream — proven over three spike runs, see
 * docs/proposals/2026-08-13-mediasession-spike.md. Everything audible still
 * comes from mpv in Termux.
 */
public class CompanionService extends Service {

    /**
     * mpv's IPC socket is inside com.termux's private sandbox and cannot be
     * opened from here; a socat listener on loopback is the only route. This is
     * the *second* listener — the tailnet one that agent-media on red5 uses is
     * a separate service and is not disturbed.
     */
    static final String MPV_HOST = "127.0.0.1";
    static final int MPV_PORT = 6601;

    /**
     * The speech mpv, behind its own loopback bridge
     * (packages/core/services/mpv-speech-bridge-local). It carries three jobs:
     * the metadata follows whichever channel is in front, the coordinator's
     * speaking flag says whose a focus loss is, and — the other half of David's
     * rule — speech is paused on a focus loss that is not our own. See
     * SpeechPolicy.
     */
    static final int MPV_SPEECH_PORT = 6602;

    /**
     * The focus policy is allowed to touch mpv only when this is on. Off by
     * default, which makes a fresh install a *probe*: it takes focus and logs
     * every callback but changes nothing, so the first sideload can answer what
     * Android actually delivers — and whether taking focus disturbs the
     * pulseaudio stream underneath — before any behaviour change can muddy it.
     * One APK does both jobs; every install here is a sideload and a tap
     * through a chooser, so a build-time flag would cost a round trip.
     */
    private static final String PREFS = "companion";
    private static final String KEY_FOCUS_ACTS = "focus_acts";

    private static final String CHANNEL = "agent-media";
    private static final int NOTIF_ID = 1;
    /** How often to refresh position while playing. The session extrapolates between. */
    private static final long POSITION_POLL_MS = 5000;
    private static final int LOG_LINES = 200;
    /** How long a transient loss waits for the speech mpv to say whose it is. */
    private static final long DUCK_DECISION_DELAY_MS = 300;

    private static final List<String> EVENTS = new ArrayList<String>();

    private final Handler main = new Handler(Looper.getMainLooper());
    private final MpvState state = new MpvState();
    /** The speech mpv's mirror. Feeds the metadata and the speech half of focus. */
    private final MpvState speechState = new MpvState();
    private final FocusPolicy focus = new FocusPolicy();
    /** The speech half of David's rule; drives speechIpc, never the music one. */
    private final SpeechPolicy speechFocus = new SpeechPolicy();
    private MpvIpc ipc;
    private MpvIpc speechIpc;
    private MediaSession session;
    private NotificationManager nm;
    private Silence silence;
    private FocusControl focusControl;
    private StatusServer status;
    private SharedPreferences prefs;
    private boolean focusActs = false;
    /** A deferred transient-loss decision; main-thread only. See onFocusChange. */
    private Runnable pendingFocus;
    /** When the speech mpv last opened or started a clip; 0 = never. */
    private volatile long speechStagedAt = 0L;
    /** When the coordinator last raised the speaking flag; 0 = never. */
    private volatile long speakingSince = 0L;
    /** Every focus callback seen, newest last — the /state readout's history. */
    private final List<String> focusHistory = new ArrayList<String>();
    /** The PlaybackState we last told the framework, and the last key we saw. */
    private volatile String lastPushedState = "none";
    private volatile String lastButton = "none";

    // ---- on-screen log (adb cannot reach this phone) ---------------------

    static void log(String line) {
        String stamp = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        synchronized (EVENTS) {
            EVENTS.add(stamp + "  " + line);
            while (EVENTS.size() > LOG_LINES) EVENTS.remove(0);
        }
    }

    static String dump() {
        synchronized (EVENTS) {
            if (EVENTS.isEmpty()) return "(no events yet)";
            StringBuilder sb = new StringBuilder();
            for (int i = EVENTS.size() - 1; i >= 0; i--) sb.append(EVENTS.get(i)).append('\n');
            return sb.toString();
        }
    }

    String status() {
        return state
                + "\nfront=" + FrontChannel.name(speechState)
                + " speech=" + (!speechState.connected ? "unreachable"
                        : speechState.playing() ? "speaking" : "quiet")
                + "\nfocus=" + (focusControl == null ? "none"
                        : FocusControl.kindName(focusControl.kind()))
                + " mode=" + (focusActs ? "acting" : "probe (logs only)")
                + (focus.owesUnduck() ? " owes:unduck" : "")
                + (speechFocus.owesResume() ? " owes:speech-resume" : "");
    }

    boolean focusActs() {
        return focusActs;
    }

    /** Flip between the probe and the acting build. Survives a restart. */
    void setFocusActs(boolean acts) {
        if (acts == focusActs) return;
        focusActs = acts;
        prefs.edit().putBoolean(KEY_FOCUS_ACTS, acts).apply();
        // Whatever the old mode owed — or was about to do — does not carry
        // across the switch.
        if (pendingFocus != null) {
            main.removeCallbacks(pendingFocus);
            pendingFocus = null;
        }
        focus.reset();
        // A speech pause is cleared before it is forgotten, not after: dropping
        // the debt would leave the broker paused with nothing left that knows to
        // undo it. The music's ducked volume is visible and pressable; this is
        // neither. DISCARD rather than RESUME, here and on the way out — leaving
        // is never a reason to start audio nobody asked for.
        if (speechFocus.owesResume()) {
            log("focus: clearing the speech pause before the mode switch");
            performSpeech(SpeechPolicy.Action.DISCARD);
            speechFocus.forget();
        }
        log("focus: mode -> " + (acts ? "acting" : "probe (logs only)"));
    }

    // ---- lifecycle -------------------------------------------------------

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        focusActs = prefs.getBoolean(KEY_FOCUS_ACTS, false);
        focusControl = new FocusControl(this, main, this::onFocusChange);

        nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(
                CHANNEL, "agent-media", NotificationManager.IMPORTANCE_LOW));

        session = new MediaSession(this, "agent-media music");
        session.setCallback(callback);
        session.setMediaButtonBroadcastReceiver(
                new ComponentName(this, MediaButtonReceiver.class));
        session.setActive(true);

        // Foreground first, before anything that can block: Android kills a
        // service that takes too long to post its notification.
        startForeground(NOTIF_ID, buildNotification(),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        pushSessionState();

        ipc = new MpvIpc(MPV_HOST, MPV_PORT, listener);
        ipc.start();

        speechIpc = new MpvIpc(MPV_HOST, MPV_SPEECH_PORT, speechListener,
                               MpvIpc.OBSERVED_SPEECH);
        speechIpc.start();

        status = new StatusServer(StatusServer.DEFAULT_PORT, statusSource,
                                  CompanionService::log);
        status.start();
        main.postDelayed(positionPoll, POSITION_POLL_MS);
        log("service started; mpv ipc -> " + MPV_HOST + ":" + MPV_PORT
                + ", speech -> " + MPV_HOST + ":" + MPV_SPEECH_PORT);
        log("focus: mode " + (focusActs ? "acting" : "probe (logs only)"));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        main.removeCallbacks(positionPoll);
        if (pendingFocus != null) main.removeCallbacks(pendingFocus);
        if (ipc != null) ipc.stop();
        if (speechIpc != null) {
            // Best-effort, and before the sender is shut down: a speech pause we
            // still owe outlives this process on the phone's mpv. The write may
            // not make it out; the coordinator clears pause at the start of the
            // next response, which is the backstop.
            if (speechFocus.owesResume()) performSpeech(SpeechPolicy.Action.DISCARD);
            speechIpc.stop();
        }
        if (status != null) status.stop();
        if (focusControl != null) focusControl.abandon();
        stopSilence();
        if (session != null) {
            session.setActive(false);
            session.release();
        }
        super.onDestroy();
    }

    // ---- mpv -> session --------------------------------------------------

    private final MpvIpc.Listener listener = new MpvIpc.Listener() {
        @Override public void onProperty(final String name, final Object value) {
            main.post(() -> {
                if (state.apply(name, value)) {
                    log("mpv " + name + " = " + value);
                    // The guard the policy needs from the outside world: a
                    // volume we did not write means something else owns it now
                    // (call_guard during a call is the live example), so the
                    // restore is dropped rather than clobbering it.
                    switch (name) {
                        case "volume":      focus.onVolumeChanged(state.volume); break;
                        case "idle-active": if (state.idleActive) focus.reset(); break;
                        default: break;
                    }
                    pushSessionState();
                }
            });
        }

        @Override public void onEvent(String event, Map<String, Object> message) {
            // Property observers carry everything the session needs; events are
            // logged only so the on-screen readout can show mpv is alive.
            if ("end-file".equals(event) || "start-file".equals(event)) log("mpv event " + event);
        }

        @Override public void onConnected() {
            main.post(() -> { state.connected = true; pushSessionState(); });
        }

        @Override public void onDisconnected(String why) {
            main.post(() -> { state.connected = false; pushSessionState(); });
        }

        @Override public void onLog(String line) {
            log(line);
        }
    };

    /**
     * The speech mpv, listened to and never driven. Only two properties are
     * observed — enough to answer "is a clip running" — so a long reply does not
     * flood the event log that focus diagnosis is read from.
     */
    private final MpvIpc.Listener speechListener = new MpvIpc.Listener() {
        @Override public void onProperty(final String name, final Object value) {
            main.post(() -> {
                if (speechState.apply(name, value)) {
                    log("speech " + name + " = " + value);
                    // When a clip was staged, so a focus loss arriving before it
                    // is audible can still be recognised as ours. A path is set
                    // on loadfile; the unpause is the same episode's other edge.
                    // Deliberately NOT the clip *ending*: the grace should
                    // expire with the reply, not be extended by it.
                    boolean staged = ("path".equals(name) && speechState.path != null)
                            || ("pause".equals(name) && !speechState.paused);
                    if (staged) speechStagedAt = System.currentTimeMillis();
                    // The guard the speech policy needs from outside: a resume
                    // from anywhere else — the popup, the CLI, or the
                    // coordinator clearing pause for the next response — means
                    // the pause we owed is no longer ours to pay.
                    if ("pause".equals(name)) speechFocus.onPauseChanged(speechState.paused);
                    if (MpvIpc.SPEAKING_PROPERTY.equals(name) && speechState.speaking) {
                        speakingSince = System.currentTimeMillis();
                    }
                    pushSessionState();
                }
            });
        }

        @Override public void onEvent(String event, Map<String, Object> message) { }

        @Override public void onConnected() {
            main.post(() -> { speechState.connected = true; pushSessionState(); });
        }

        @Override public void onDisconnected(String why) {
            // The metadata falls back to music, which is the right answer when
            // we can no longer tell whether a clip is playing.
            main.post(() -> { speechState.connected = false; pushSessionState(); });
        }

        @Override public void onLog(String line) {
            log("speech: " + line);
        }
    };

    /** Ask mpv where it is, then refresh the session's position. */
    private final Runnable positionPoll = new Runnable() {
        @Override public void run() {
            if (ipc != null && ipc.isConnected() && state.playing()) {
                ipc.getProperty(MpvIpc.POSITION_PROPERTY).whenComplete((v, e) -> {
                    if (e != null) return;
                    main.post(() -> {
                        state.apply(MpvIpc.POSITION_PROPERTY, v);
                        pushSessionState();
                    });
                });
            }
            // The only clock the speech pause has. It runs whether or not music
            // is playing, which is the case that needs it: a permanent loss
            // pauses speech and then nothing else happens at all.
            expireSpeechPause();
            main.postDelayed(this, POSITION_POLL_MS);
        }
    };

    /** Re-publish metadata, playback state and the notification from {@link #state}. */
    private void pushSessionState() {
        // The silent track follows *loaded*, not *playing*. Dropping it while
        // paused would surrender the addressed-player slot, and the press that
        // would win it back is exactly the play button we would no longer
        // receive. It stops when mpv goes idle, which is the case that would
        // otherwise cost battery all day.
        // It follows speech too, for the reason the focus claim does: while Sam
        // is the only thing audible, dropping it would drop our card out of the
        // shade's media player and hand the addressed-player slot away in the
        // middle of a reply.
        if (state.loaded() || FrontChannel.speechInFront(speechState)) startSilence();
        else stopSilence();

        // Focus follows a related predicate, for a related reason: abandoning it
        // on our own pause would forfeit the GAIN that is supposed to tell us to
        // resume. But it covers BOTH channels, which the first build did not —
        // and that omission made the speech half unreachable. Focus was
        // requested for `state.loaded()`, the music mpv alone, so a spoken reply
        // with no music behind it left the app holding nothing: David played
        // YouTube over Sam on 2026-08-15 08:10 and no callback arrived at all,
        // because you cannot be told you lost what you never took. The whole
        // point of the speech half is the interruption that lands mid-sentence,
        // which is exactly the case where music is least likely to be playing.
        //
        // The third term keeps it while a speech pause of ours is outstanding:
        // dropping focus there would forfeit the very GAIN that pays it, and the
        // pause would stand until the deadline discarded the clip.
        //
        // Music wins the tie: while a track is open the claim is the permanent
        // one, and a clip spoken over it must not downgrade the claim to a
        // transient borrow — that would hand the output back to another app the
        // moment Sam finished a sentence.
        int wantFocus = state.loaded() ? FocusControl.MUSIC
                : (FrontChannel.speechInFront(speechState) || speechFocus.owesResume())
                        ? FocusControl.SPEECH
                        : FocusControl.NONE;
        if (wantFocus != FocusControl.NONE) {
            if (focusControl.kind() != wantFocus && focusControl.request(wantFocus)) {
                log("focus: granted (" + FocusControl.kindName(wantFocus) + ")");
            }
        } else if (focusControl.held()) {
            focusControl.abandon();
            focus.reset();
            log("focus: abandoned (nothing open)");
        }

        // The metadata follows whichever channel is in front, so the car display
        // names Sam while Sam is what is audible.
        MediaMetadata.Builder md = new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE,
                           FrontChannel.title(state, speechState))
                .putString(MediaMetadata.METADATA_KEY_ARTIST,
                           FrontChannel.subtitle(state, speechState))
                .putLong(MediaMetadata.METADATA_KEY_DURATION,
                         FrontChannel.durationMs(state, speechState));
        session.setMetadata(md.build());

        // And so does the PlaybackState, since 2026-08-15. It used to describe
        // music alone, on the reasoning that resolving a PLAY_PAUSE toggle
        // against a two-second clip is the class of bug 3519172 fixed. That was
        // half right and the wrong half in practice: while Sam spoke with no
        // track open, the card said STOPPED under a title that said "Sam", its
        // button showed a play triangle, and pressing it sent pause=false to an
        // idle music mpv — which does nothing. David pressed it five times in a
        // row at 08:22 before a `previous` finally loaded a track.
        //
        // A control that describes one channel and is labelled with another is
        // worse than a stale toggle. What the card says is now what is audible,
        // and the transport below goes to the same place.
        MpvState front = FrontChannel.speechInFront(speechState) ? speechState : state;

        int playbackState;
        if (!front.connected) playbackState = PlaybackState.STATE_ERROR;
        else if (!front.loaded()) playbackState = PlaybackState.STATE_STOPPED;
        else if (front.paused) playbackState = PlaybackState.STATE_PAUSED;
        else playbackState = PlaybackState.STATE_PLAYING;

        // A spoken clip has no next, no previous and nothing to seek within, so
        // the buttons that would claim otherwise are dropped while it is in
        // front rather than pointed at the music underneath.
        long actions = PlaybackState.ACTION_PLAY
                | PlaybackState.ACTION_PAUSE
                | PlaybackState.ACTION_PLAY_PAUSE
                | PlaybackState.ACTION_STOP;
        if (front == state) {
            actions |= PlaybackState.ACTION_SKIP_TO_NEXT
                    | PlaybackState.ACTION_SKIP_TO_PREVIOUS
                    | PlaybackState.ACTION_SEEK_TO;
        }

        PlaybackState.Builder pb = new PlaybackState.Builder()
                .setActions(actions)
                .setState(playbackState, front.positionMs(),
                          front.playing() ? (float) front.speed : 0f);
        if (!front.connected) {
            // The (code, message) overload is AndroidX-only; the platform
            // Builder takes a bare CharSequence.
            pb.setErrorMessage("mpv unreachable");
        }
        session.setPlaybackState(pb.build());

        // What we report is what decides how a PLAY_PAUSE toggle is resolved,
        // so it has to be visible from /state — guessing at it was exactly what
        // made the "pause works, play does not" symptom undiagnosable.
        String named = playbackState == PlaybackState.STATE_PLAYING ? "PLAYING"
                     : playbackState == PlaybackState.STATE_PAUSED ? "PAUSED"
                     : playbackState == PlaybackState.STATE_STOPPED ? "STOPPED"
                     : playbackState == PlaybackState.STATE_ERROR ? "ERROR"
                     : String.valueOf(playbackState);
        if (!named.equals(lastPushedState)) {
            log("session: reporting " + named);
            lastPushedState = named;
        }

        nm.notify(NOTIF_ID, buildNotification());
    }

    private Notification buildNotification() {
        PendingIntent open = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        String text;
        if (!state.connected) text = "mpv unreachable on " + MPV_HOST + ":" + MPV_PORT;
        else if (!state.loaded()) text = "idle";
        else text = state.paused ? "paused" : "playing";
        if (FrontChannel.speechInFront(speechState)) text = "speaking — music " + text;

        // The small icon reports *state*, so it has to follow it: a hardcoded
        // triangle sat there through every pause and read as the app being
        // stuck, which is a poor thing for a status indicator to say when the
        // session underneath is correct. It reads the front channel for the same
        // reason the card does — while Sam speaks, "playing" is about Sam.
        boolean audible = FrontChannel.speechInFront(speechState) || state.playing();
        int icon = audible ? android.R.drawable.ic_media_play
                           : android.R.drawable.ic_media_pause;

        return new Notification.Builder(this, CHANNEL)
                .setContentTitle(state.loaded() || FrontChannel.speechInFront(speechState)
                        ? FrontChannel.title(state, speechState) : "agent-media")
                .setContentText(text)
                .setSmallIcon(icon)
                .setStyle(new Notification.MediaStyle().setMediaSession(session.getSessionToken()))
                .setContentIntent(open)
                .setOngoing(true)
                .build();
    }

    // ---- session -> mpv --------------------------------------------------

    private final MediaSession.Callback callback = new MediaSession.Callback() {
        /**
         * The raw key, before the framework decides which transport callback it
         * means. Logged because every press was arriving as onPause and there
         * was no way to tell whether the earbud sends a PLAY_PAUSE toggle that
         * we are resolving wrongly, or a dedicated PAUSE whose PLAY twin goes
         * somewhere else entirely. Those need opposite fixes.
         */
        @Override public boolean onMediaButtonEvent(Intent intent) {
            KeyEvent key = intent == null ? null
                    : (KeyEvent) intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT);
            if (key == null) return super.onMediaButtonEvent(intent);

            String name = KeyEvent.keyCodeToString(key.getKeyCode());
            boolean down = key.getAction() == KeyEvent.ACTION_DOWN;
            if (down) {
                lastButton = name;
                log("button: " + name + " (we report " + lastPushedState + ")");
            }

            ButtonPolicy.Press press = ButtonPolicy.interpret(key.getKeyCode(), frontState());
            if (press == ButtonPolicy.Press.DEFAULT) {
                return super.onMediaButtonEvent(intent);
            }

            // Consume both the down and the up, so the framework does not also
            // translate the key and undo what we just did.
            if (down) {
                log("button: " + name + " read as " + press + " — " + frontName()
                        + " is " + (frontState().paused ? "paused" : "playing"));
                if (press == ButtonPolicy.Press.PLAY) onPlay(); else onPause();
            }
            return true;
        }

        /**
         * Play and pause go to whichever channel the card is describing. They
         * used to go to music unconditionally, which is how five presses of a
         * play button under the title "Sam" set pause=false on an idle music mpv
         * five times and did nothing at all (p8a, 2026-08-15 08:22).
         *
         * Pausing Sam mid-reply is a real thing to want, and it is the action
         * the button most obviously offers while he is the one talking.
         */
        @Override public void onPlay() {
            log("transport: play -> " + frontName());
            frontIpc().setProperty("pause", Boolean.FALSE);
        }

        @Override public void onPause() {
            log("transport: pause -> " + frontName());
            frontIpc().setProperty("pause", Boolean.TRUE);
        }

        @Override public void onStop() {
            log("transport: stop -> " + frontName());
            frontIpc().command("stop");
        }

        @Override public void onSkipToNext() {
            log("transport: next");
            ipc.command("playlist-next", "weak");
        }

        @Override public void onSkipToPrevious() {
            log("transport: previous");
            ipc.command("playlist-prev", "weak");
        }

        @Override public void onSeekTo(long positionMs) {
            log("transport: seek " + positionMs);
            ipc.setProperty("time-pos", positionMs / 1000.0);
        }
    };

    // ---- the front channel, for the card and its buttons -----------------

    /** The mpv the session is describing: speech while a clip runs, else music. */
    private MpvState frontState() {
        return FrontChannel.speechInFront(speechState) ? speechState : state;
    }

    /** The connection to it. Next/previous/seek deliberately never come here. */
    private MpvIpc frontIpc() {
        return FrontChannel.speechInFront(speechState) ? speechIpc : ipc;
    }

    private String frontName() {
        return FrontChannel.name(speechState);
    }

    // ---- the outside readout ---------------------------------------------

    /**
     * What /state answers. Deliberately covers the questions that could not be
     * answered from red5 before it existed: is the app acting or only probing,
     * does it hold focus, what focus changes has it actually seen, and does it
     * still owe mpv anything.
     */
    private final StatusServer.Source statusSource = new StatusServer.Source() {
        @Override public String state() {
            Map<String, Object> m = new LinkedHashMap<String, Object>();
            m.put("connected", Boolean.valueOf(state.connected));
            m.put("idle", Boolean.valueOf(state.idleActive));
            m.put("paused", Boolean.valueOf(state.paused));
            m.put("title", state.title());
            m.put("position_s", Double.isNaN(state.position) ? null : Double.valueOf(state.position));
            m.put("duration_s", Double.isNaN(state.duration) ? null : Double.valueOf(state.duration));
            m.put("volume", Double.valueOf(state.volume));
            m.put("speed", Double.valueOf(state.speed));
            m.put("reported_state", lastPushedState);
            // Which channel the metadata is describing, and the speech mpv it
            // is read from — so "the car still says the track name" is two curl
            // lines rather than another sideload.
            m.put("front", FrontChannel.name(speechState));
            m.put("metadata_title", FrontChannel.title(state, speechState));
            Map<String, Object> sp = new LinkedHashMap<String, Object>();
            sp.put("connected", Boolean.valueOf(speechState.connected));
            sp.put("idle", Boolean.valueOf(speechState.idleActive));
            sp.put("paused", Boolean.valueOf(speechState.paused));
            long since = sinceSpeechStaged();
            long flag = sinceSpeaking();
            sp.put("staged_ms_ago", since == Long.MAX_VALUE ? null : Long.valueOf(since));
            // What the coordinator told us, and how long ago — the difference
            // between "we know" and "we guessed" when reading a duck decision.
            sp.put("speaking", Boolean.valueOf(speechState.speaking));
            sp.put("speaking_ms_ago", flag == Long.MAX_VALUE ? null : Long.valueOf(flag));
            // The answer that decides whether a transient loss ducks at all.
            sp.put("owns_the_loss", Boolean.valueOf(
                    FrontChannel.ourSpeech(speechState, since, flag)));
            // Whether the speech broker is paused *by us* — the one pause on
            // this phone that nothing else will undo. See SpeechPolicy.
            sp.put("owes_resume", Boolean.valueOf(speechFocus.owesResume()));
            m.put("speech", sp);
            m.put("last_button", lastButton);
            m.put("focus_mode", focusActs ? "acting" : "probe");
            m.put("focus_held", Boolean.valueOf(focusControl != null && focusControl.held()));
            // Which claim: the permanent one music takes, or the transient
            // borrow a spoken clip asks for. They behave differently towards
            // every other app on the phone.
            m.put("focus_kind", FocusControl.kindName(
                    focusControl == null ? FocusControl.NONE : focusControl.kind()));
            m.put("owes_unduck", Boolean.valueOf(focus.owesUnduck()));
            m.put("restore_volume", Double.valueOf(focus.volumeToRestore()));
            m.put("duck_volume", Integer.valueOf(FocusPolicy.DUCK_VOLUME));
            synchronized (focusHistory) {
                m.put("focus_events", new ArrayList<String>(focusHistory));
            }
            return Json.write(m);
        }

        @Override public String log() {
            return dump();
        }
    };

    // ---- focus -> mpv ----------------------------------------------------

    /**
     * Android has moved audio focus. Delivered on the main looper (see
     * FocusControl), so it is safe to touch state here; the IPC writes go to
     * MpvIpc's sender thread.
     */
    private void onFocusChange(int change) {
        log("focus: " + FocusControl.name(change) + " [" + state + "]");
        if (pendingFocus != null) {
            // Any newer focus change supersedes a deferred one — otherwise a
            // GAIN could be overtaken by the duck it just cancelled, and the
            // music would stay quiet with nothing owed.
            main.removeCallbacks(pendingFocus);
            pendingFocus = null;
        }
        synchronized (focusHistory) {
            focusHistory.add(new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date())
                    + " " + FocusControl.name(change)
                    + (focusActs ? "" : " (probe)"));
            while (focusHistory.size() > 40) focusHistory.remove(0);
        }
        if (!focusActs) {
            log("focus: probe mode, no action");
            return;
        }

        // A transient loss is answered a beat late, on purpose. The question it
        // now depends on — is this our own speech — is answered by the speech
        // mpv's own idle-active/pause, which travels a different socket and can
        // arrive after the focus callback for the very clip that caused it. A
        // duck is not time-critical to the millisecond; getting it wrong costs
        // the volume for the rest of the response.
        if (change == FocusPolicy.LOSS_TRANSIENT || change == FocusPolicy.LOSS_TRANSIENT_CAN_DUCK) {
            final int deferred = change;
            pendingFocus = () -> { pendingFocus = null; applyFocus(deferred); };
            main.postDelayed(pendingFocus, DUCK_DECISION_DELAY_MS);
            return;
        }
        applyFocus(change);
    }

    /** How long ago the speech mpv staged a clip, for FrontChannel.ourSpeech. */
    private long sinceSpeechStaged() {
        return since(speechStagedAt);
    }

    /** How long ago the coordinator raised the speaking flag. */
    private long sinceSpeaking() {
        return since(speakingSince);
    }

    private static long since(long stamp) {
        return stamp == 0L ? Long.MAX_VALUE : System.currentTimeMillis() - stamp;
    }

    private void applyFocus(int change) {
        boolean ourSpeech = FrontChannel.ourSpeech(
                speechState, sinceSpeechStaged(), sinceSpeaking());
        List<FocusPolicy.Action> actions = focus.onFocusChange(change, state, ourSpeech);
        List<SpeechPolicy.Action> speechActions =
                speechFocus.onFocusChange(change, speechState, ourSpeech, System.currentTimeMillis());
        if (ourSpeech && actions.isEmpty() && speechActions.isEmpty()) {
            log("focus: our own speech — the coordinator owns the volume");
            return;
        }
        for (FocusPolicy.Action action : actions) {
            perform(action);
        }
        for (SpeechPolicy.Action action : speechActions) {
            performSpeech(action);
        }
    }

    private void perform(FocusPolicy.Action action) {
        switch (action) {
            case PAUSE:
                log("focus: pause");
                ipc.setProperty("pause", Boolean.TRUE);
                break;
            case DUCK:
                log("focus: duck -> " + FocusPolicy.DUCK_VOLUME);
                ipc.setProperty("volume", Double.valueOf(FocusPolicy.DUCK_VOLUME));
                break;
            case UNDUCK:
                double restore = focus.volumeToRestore();
                log("focus: unduck -> " + (int) restore);
                ipc.setProperty("volume", Double.valueOf(restore));
                break;
            default:
                break;
        }
    }

    /**
     * The speech half. A different socket from {@link #perform}'s — the music
     * mpv is never paused for a sentence and the speech mpv is never ducked.
     */
    private void performSpeech(SpeechPolicy.Action action) {
        switch (action) {
            case PAUSE:
                log("focus: pause speech");
                speechIpc.setProperty("pause", Boolean.TRUE);
                break;
            case RESUME:
                log("focus: resume speech");
                speechIpc.setProperty("pause", Boolean.FALSE);
                break;
            case DISCARD:
                // Order matters: stop first, so clearing the pause hands back an
                // idle broker rather than starting a sentence nobody is waiting
                // for any more.
                log("focus: discard the stale clip, unpause the broker");
                speechIpc.command("stop");
                speechIpc.setProperty("pause", Boolean.FALSE);
                break;
            default:
                break;
        }
    }

    /**
     * Clear a speech pause that nothing is going to lift. mpv's pause outlives
     * the clip it was set on, so the cost of forgetting one is a broker that
     * swallows every later reply — see SpeechPolicy.RESUME_DEADLINE_MS.
     */
    private void expireSpeechPause() {
        for (SpeechPolicy.Action action : speechFocus.onTick(System.currentTimeMillis())) {
            log("focus: speech pause stood too long to be lifted by hand");
            performSpeech(action);
        }
    }

    // ---- the stream of zeros --------------------------------------------

    private void startSilence() {
        if (silence != null) return;
        silence = new Silence();
        silence.start();
        log("silent track started");
    }

    private void stopSilence() {
        if (silence == null) return;
        silence.stop();
        silence = null;
        log("silent track stopped");
    }

    /**
     * A full-volume all-zero PCM stream. Full volume on purpose: the buffer is
     * silent either way, and a zero-*volume* track risks being optimised out of
     * the mix — which would quietly cost us the controls.
     */
    private static final class Silence {
        private AudioTrack track;
        private Thread writer;
        private volatile boolean running = false;

        void start() {
            int rate = 44100;
            int min = AudioTrack.getMinBufferSize(rate,
                    AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
            final int buf = Math.max(min, 4096);

            track = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                            .build())
                    .setAudioFormat(new AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(rate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .build())
                    .setBufferSizeInBytes(buf)
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build();
            track.play();
            running = true;

            final AudioTrack t = track;
            writer = new Thread(() -> {
                short[] zeros = new short[buf / 2];
                while (running) {
                    // The blocking write paces this loop at real time.
                    if (t.write(zeros, 0, zeros.length) < 0) break;
                }
            }, "silence");
            writer.setDaemon(true);
            writer.start();
        }

        void stop() {
            running = false;
            if (writer != null) {
                try { writer.join(500); } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                writer = null;
            }
            if (track != null) {
                try { track.stop(); } catch (IllegalStateException ignored) { }
                track.release();
                track = null;
            }
        }
    }

    // ---- binding ---------------------------------------------------------

    public class LocalBinder extends android.os.Binder {
        CompanionService service() { return CompanionService.this; }
    }

    private final IBinder binder = new LocalBinder();

    @Override
    public IBinder onBind(Intent intent) {
        return binder;
    }
}
