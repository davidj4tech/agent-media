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
import android.media.AudioManager;
import android.media.AudioTrack;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.view.KeyEvent;
import android.widget.Toast;

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

    /*
     * Where the three mpv connections go is {@link Server}'s answer now, not a
     * constant here. The default is unchanged and it is the one this app was
     * built against: three socat listeners on this phone's loopback, because
     * mpv's own sockets are inside com.termux's private sandbox and no other
     * app can open them. What each connection is *for* has not moved either —
     * music is the phone's player and the focus policy is about it; speech
     * carries the front-channel metadata, the coordinator's speaking flag and
     * the pause half of David's rule; the book has a card and no part in
     * focus at all.
     */

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
    /**
     * What to do about audio focus while a two-way voice session (Claude Live,
     * a call) holds the microphone. Under experiment — see /live.
     *
     *   yield  — the old behaviour: claim focus as usual. Live pauses itself
     *            and asks for a tap before it will listen again.
     *   duck   — claim MAY_DUCK instead, so Android turns Live down rather
     *            than telling it to stop.
     *   share  — claim nothing for speech at all. Sam talks over the session,
     *            and Live's microphone hears him.
     *
     * Settable at runtime and remembered, because every alternative is a
     * sideload and a tap, and this is a question about how it *feels*.
     */
    private static final String KEY_LIVE_MODE = "live_mode";
    static final String LIVE_YIELD = "yield";
    static final String LIVE_DUCK = "duck";
    static final String LIVE_SHARE = "share";
    /**
     * hold — the default, and the tier this app is really for. While a voice
     * session runs, Sam waits: the reply is paused rather than spoken, a card
     * offers to say it now, and when the session ends it is delivered by itself.
     *
     * The reasoning, from the evening this was chosen: nothing can push into a
     * model's turn, so Cece cannot be told Sam is speaking. Yield tells her, by
     * pausing her, and costs a tap. Duck costs nothing and leaves her talking
     * over him. Waiting costs neither, because the wait ends on its own.
     */
    static final String LIVE_HOLD = "hold";

    /** The card's buttons come back here. See onStartCommand. */
    static final String ACTION_SPEAK_NOW = "net.agentmedia.companion.SPEAK_NOW";
    static final String ACTION_LATER = "net.agentmedia.companion.LATER";

    private static final String CHANNEL = "agent-media";
    /**
     * The one channel allowed to interrupt: "Sam has something to say".
     *
     * Everything else this app posts is a media card, and media cards must be
     * silent and stay in the shade — which is exactly wrong for a question. A
     * card asking whether to speak is worthless if it has to be gone looking
     * for, and during a voice session Live's own UI is the whole screen. Only
     * IMPORTANCE_HIGH gets a floating banner.
     *
     * Silent, and a short vibration instead. A chime would land in the middle
     * of David's sentence, and he is the one thing in the room we are trying
     * not to talk over.
     */
    private static final String CHANNEL_ASK = "agent-media-ask";
    /**
     * One notification per card. The shade's media player is built from
     * MediaStyle *notifications*, not from sessions alone, so three cards means
     * three ids. Only the first is the foreground-service notification.
     */
    private static final int NOTIF_ID = 1;
    private static final int NOTIF_SPEECH = 2;
    private static final int NOTIF_BOOK = 3;
    /** Not a media card: "Sam has something to say", with a button. */
    private static final int NOTIF_WAITING = 4;
    /** How often to refresh position while playing. The session extrapolates between. */
    private static final long POSITION_POLL_MS = 5000;
    /** One column per second, the rate the tmux status line has always used. */
    private static final long MARQUEE_TICK_MS = 1000;
    private static final int LOG_LINES = 200;
    /** How long a transient loss waits for the speech mpv to say whose it is. */
    private static final long DUCK_DECISION_DELAY_MS = 300;

    /**
     * How many consecutive quiet polls end an interruption that never said it
     * was over. Two, so roughly ten seconds — see pollForQuiet().
     */
    private static final int QUIET_POLLS_TO_RESUME = 2;

    private static final List<String> EVENTS = new ArrayList<String>();

    private final Handler main = new Handler(Looper.getMainLooper());
    private final MpvState state = new MpvState();
    /**
     * The two channels that get their own card in the shade's player but are not
     * the phone's player. Only music opens an AudioTrack, holds focus, or
     * answers a media button — see SideChannel.
     */
    private SideChannel speech;
    private SideChannel book;
    /**
     * speech.state() and speech.ipc(), which the focus policy reads and drives.
     * The state starts as an empty, disconnected mirror rather than null, so a
     * missing speech channel reads as "unreachable" everywhere instead of
     * needing a null check at each of a dozen call sites — and "unreachable" is
     * already a case every one of them handles.
     */
    /** Before the speech channel exists, and if it never does. */
    private static final MpvState NO_SPEECH = new MpvState();

    /**
     * Whichever player the speech channel is currently about.
     *
     * Was a field, held from startup. That stopped being safe the moment
     * speech could come from either the Termux mpv or this app's own player:
     * a reference captured at startup is a reference to one of them forever,
     * and the wrong one for half the day.
     */
    private MpvState speechState() {
        return speech != null ? speech.state() : NO_SPEECH;
    }
    private MpvIpc speechIpc;
    private final FocusPolicy focus = new FocusPolicy();
    /** The speech half of David's rule; drives speechIpc, never the music one. */
    private final SpeechPolicy speechFocus = new SpeechPolicy();
    private MpvIpc ipc;
    private MediaSession session;
    private NotificationManager nm;
    private Silence silence;
    private FocusControl focusControl;
    private StatusServer status;
    /** Speech played by this app, when a target is pointed at it. */
    private BuiltinSpeech builtinSpeech;
    /**
     * The same player, reachable without a binding.
     *
     * The home screen asks the <em>server</em> what is playing
     * ({@code Channels.fetch} -> media-share), which is the right question
     * everywhere except here: when speech plays in this app, the server's
     * speech channel is the idle mpv on 6602 and the sound is ours. David saw
     * that as a now-playing line that did not match what he could hear.
     * Static because MainActivity does not bind to this service and binding it
     * for one row would be a lifecycle for a label.
     */
    private static volatile BuiltinSpeech LIVE_SPEECH;
    private MpvServer speechServer;
    /** What the in-app player is doing, in the shape every card reads. */
    private final MpvState builtinSpeechState = new MpvState();
    /** The mic probe. See MicWatch; BargeIn decides what it means. */
    private MicWatch mic;
    /**
     * Whether an open mic is someone talking over Sam or a conversation holding
     * the microphone. Fed by both halves; see BargeIn.
     */
    private final BargeIn bargeIn = new BargeIn();
    private SharedPreferences prefs;
    /**
     * Which agent-media this is a client of, read once at startup.
     *
     * Once, deliberately: the mpv connections, the focus claim and the silent
     * track are all built from it, and a service that re-read it mid-life would
     * have to tear all three down at an arbitrary moment. The settings screen
     * restarts the service instead, which is the same work done where somebody
     * is watching it happen.
     */
    private Server server = Server.defaults();
    private boolean focusActs = false;
    private volatile String liveMode = LIVE_HOLD;
    /** We paused Sam for the voice session and owe him a delivery. */
    private boolean heldForSession = false;
    /** The other half of the same question: Sam waits while David dictates. */
    private final DictationHold dictation = new DictationHold();
    /** How often that hold engages — a rate no person produces is a fault. */
    private final HoldRate dictationRate = new HoldRate();
    /** And the third: the book stops for a conversation, since nothing else stops it. */
    private final BookHold bookHold = new BookHold();
    /** David tapped "Speak now": the hold is off for the rest of this session. */
    private boolean speakNow = false;
    /**
     * How big the pile was when we last said something about it.
     *
     * "Later" means do not ask again, and asking once per reply would be
     * ignoring that. But letting six replies stack up in silence is its own
     * failure — they all arrive at once when the session ends. So the ask comes
     * back only when the pile crosses the threshold again.
     */
    private int noticedQueue = 0;
    /** Replies waiting before the pile is worth mentioning again. David's number. */
    private static final int QUEUE_NUDGE = 3;
    /** A deferred transient-loss decision; main-thread only. See onFocusChange. */
    private Runnable pendingFocus;
    /** When the speech mpv last opened or started a clip; 0 = never. */
    private volatile long speechStagedAt = 0L;
    /** When the coordinator last raised the speaking flag; 0 = never. */
    private volatile long speakingSince = 0L;
    /** Consecutive polls that found nothing else playing. See pollForQuiet(). */
    private int quietPolls = 0;
    /**
     * Focus was taken from us permanently and we have not asked for it back.
     *
     * Two things are true at once after an AUDIOFOCUS_LOSS: our request is dead
     * and must be re-made before any callback can reach us again, and re-making
     * it immediately would snatch the output back from the app that just took
     * it. So we wait, and the thing we wait for is having something to play —
     * see pushSessionState.
     */
    private boolean focusLost = false;
    private AudioManager audio;
    /** Every focus callback seen, newest last — the /state readout's history. */
    private final List<String> focusHistory = new ArrayList<String>();
    /** The PlaybackState we last told the framework, and the last key we saw. */
    private volatile String lastPushedState = "none";
    private volatile String lastButton = "none";
    /** The music title currently crawling, when it started, and whether it is. */
    private String shownMusicTitle = "";
    private long musicTitleSince;
    private boolean marqueeRunning;
    /** Why the previous process died, newest first. Read once; see LastExit. */
    private volatile List<String> lastExits = new ArrayList<String>();

    // ---- on-screen log (adb cannot reach this phone) ---------------------

    static void log(String line) {
        String stamp = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        synchronized (EVENTS) {
            EVENTS.add(stamp + "  " + line);
            while (EVENTS.size() > LOG_LINES) EVENTS.remove(0);
        }
    }

    static String dump() {
        return dump(LOG_LINES);
    }

    /** The newest {@code limit} events, newest first. */
    static String dump(int limit) {
        synchronized (EVENTS) {
            if (EVENTS.isEmpty()) return "(no events yet)";
            StringBuilder sb = new StringBuilder();
            int stop = Math.max(0, EVENTS.size() - limit);
            for (int i = EVENTS.size() - 1; i >= stop; i--) sb.append(EVENTS.get(i)).append('\n');
            return sb.toString();
        }
    }

    String status() {
        return state
                + "\nspeech=" + (!speechState().connected ? "unreachable"
                        : speechState().playing() ? "speaking" : "quiet")
                + " book=" + (book == null || !book.state().connected ? "unreachable"
                        : book.state().playing() ? "playing" : "quiet")
                + "\nfocus=" + (focusControl == null ? "none"
                        : FocusControl.kindName(focusControl.kind()))
                + " mode=" + (focusActs ? "acting" : "probe (logs only)")
                + (focus.owesUnduck() ? " owes:unduck" : "")
                + (speechFocus.owesResume() ? " owes:speech-resume" : "");
    }

    boolean focusActs() {
        return focusActs;
    }

    /**
     * Is the mic watch alive? The home screen's most important pill.
     *
     * This app is the only mic trigger since Automate was retired, so a watch
     * that failed to start is barge-in gone — and it fails silently, which is
     * how it stayed broken for a fortnight in August 2026.
     */
    boolean micWatching() {
        return mic != null && mic.watching();
    }

    /** How many of the three channel bridges are answering right now. */
    int bridgesUp() {
        int n = state.connected ? 1 : 0;
        if (speech != null && speech.state().connected) n++;
        if (book != null && book.state().connected) n++;
        return n;
    }

    /** The exits Android has recorded, newest first. See {@link LastExit}. */
    java.util.List<String> exits() {
        return lastExits == null ? java.util.Collections.<String>emptyList() : lastExits;
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
        // Before anything that can throw. There is no adb on this phone and
        // logcat shows only Termux's uid, so an unrecorded crash is an
        // undiagnosable one — which is exactly how "agent-media keeps stopping"
        // arrived on 2026-08-15 with nothing to read.
        Crash.install(this);

        // Foreground FIRST, and with the shortest path to it there is: a
        // notification channel, a session for the MediaStyle token, and post.
        //
        // "Before anything that can block" was already the intent, but four
        // things sat in front of it — a binder query for the exit history, a
        // SharedPreferences read off disk, a second notification channel and
        // the focus controller — and on 2026-08-16 that cost three deaths in
        // an hour, all of them `Context.startForegroundService() did not then
        // call Service.startForeground()`. The window is ten seconds from the
        // caller's `startForegroundService`, and it is the *caller's* clock:
        // the activity that starts us is still laying out its own UI on this
        // same main thread, so what we have left of the ten seconds is not
        // ours to spend on diagnostics. Each death is a hole in barge-in — the
        // mic signal is this app and nothing else since Automate was retired —
        // so the audio talks over David until call_guard notices and revives.
        //
        // Everything below this call is allowed to be slow. Nothing above it
        // may be added without asking what it costs when the phone is busy,
        // which is exactly when the app is being restarted.
        nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(
                CHANNEL, "agent-media", NotificationManager.IMPORTANCE_LOW));
        session = new MediaSession(this, "agent-media music");
        // The callback and the button receiver go on BEFORE the session goes
        // active, and they stay on this side of startForeground even though it
        // is the critical path: they cost nothing measurable, and moving them
        // after cost the earbud.
        //
        // A session's flags for media buttons and transport controls are set
        // implicitly when it gets a callback, so a session that goes active
        // without one is announced as handling neither. Something else then
        // takes the addressed-player slot — on 2026-08-17 it was the speech
        // card, and `cmd media_session dispatch play` went to Sam instead of
        // the music, with our own onMediaButtonEvent never firing at all.
        // That is the whole point of the music session, so it comes first.
        session.setCallback(callback);
        // setMediaButtonBroadcastReceiver is API 31. The TV (ftv) is Android
        // 11, where calling it throws NoSuchMethodError out of onCreate and
        // the service never starts at all -- so the pre-31 path is the
        // deprecated PendingIntent form, which routes button events to the
        // same receiver. Phones (31+) keep the exact call they had.
        ComponentName button = new ComponentName(this, MediaButtonReceiver.class);
        if (android.os.Build.VERSION.SDK_INT >= 31) {
            session.setMediaButtonBroadcastReceiver(button);
        } else {
            session.setMediaButtonReceiver(PendingIntent.getBroadcast(
                    this, 0,
                    new Intent(Intent.ACTION_MEDIA_BUTTON).setComponent(button),
                    PendingIntent.FLAG_IMMUTABLE));
        }
        session.setActive(true);
        startForeground(NOTIF_ID, buildNotification(),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);

        // The deaths Crash cannot see, which is all of them bar an uncaught
        // exception. On 2026-08-15 the service stopped answering on 8770 with
        // nothing written to Downloads at all: a real answer, but only to the
        // question "was it a throw?".
        lastExits = LastExit.read(this);
        audio = getSystemService(AudioManager.class);
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        server = Settings.server(this);
        focusActs = prefs.getBoolean(KEY_FOCUS_ACTS, false);
        liveMode = prefs.getString(KEY_LIVE_MODE, LIVE_HOLD);
        focusControl = new FocusControl(this, main, this::onFocusChange);

        NotificationChannel ask = new NotificationChannel(
                CHANNEL_ASK, "agent-media asks", NotificationManager.IMPORTANCE_HIGH);
        ask.setSound(null, null);
        ask.enableVibration(true);
        ask.setDescription("Sam has something to say while you are in a "
                + "conversation — asked, not spoken");
        nm.createNotificationChannel(ask);

        // The readout comes up before the things it is there to explain. It
        // used to start last, so a crash in the channel setup below took the
        // one route to the answer down with it.
        status = new StatusServer(StatusServer.DEFAULT_PORT, statusSource,
                                  CompanionService::log);
        status.start();

        startBuiltinSpeech();

        pushSessionState();

        ipc = new MpvIpc(server.mpvHost(), server.music, listener);
        ipc.start();

        // Both side channels are optional, and the app says so by construction.
        // Music is the phone's player and the focus policy is the point of the
        // app; a second and third card are worth having and worth nothing next
        // to those. A failure here used to be fatal, which on this device means
        // a dialog, a restart loop, and no way to read why.
        speech = startChannel("speech", FrontChannel.SPEECH_TITLE, server.speech,
                              MpvIpc.OBSERVED_SPEECH, NOTIF_SPEECH, speechWatcher);
        if (speech != null) {
            speechIpc = speech.ipc();
            // Speech has two possible players now; the card follows the one
            // with a clip open. Idle in-app means the mpv bridge is the truth,
            // which is what makes switching back a setting and not a restart.
            if (builtinSpeech != null) {
                speech.mirror(builtinSpeechState, builtinSpeech::active);
            }
        }

        // The book bridge may legitimately not be running — a book broker is
        // started on the days there is a book. An unreachable channel simply
        // never shows a card; MpvIpc reconnects with backoff forever, so one
        // appears the moment the bridge does.
        book = startChannel("book", "Audiobook", server.book,
                            MpvIpc.OBSERVED, NOTIF_BOOK, bookWatcher);

        // Last, and never fatal: it is a probe, and the app worked without it
        // yesterday. It answers one question — can we see the mic at all —
        // which decides whether Automate can be retired or has to be replaced
        // by a microphone we hold ourselves.
        bargeIn.logTo(CompanionService::log);
        mic = new MicWatch(audio, main, active -> {
            bargeIn.onMic(active, mic.source(), System.currentTimeMillis());
            log("mic: " + (active ? "something is recording" : "quiet")
                    + " — " + bargeIn.why(System.currentTimeMillis()));
            // The mic opening is the whole signal for the dictation hold, and
            // it must not wait for the next position poll to be noticed: the
            // sentence being talked over is happening now. This is also what
            // stopped MicWatch being a probe — until this call nothing
            // downstream of it acted, so dictation had no effect at all.
            pushSessionState();
        });
        mic.start();

        main.postDelayed(positionPoll, POSITION_POLL_MS);
        log("service started; server " + server.describe()
                + "; music -> " + server.mpvHost() + ":" + server.music
                + ", speech -> " + server.mpvHost() + ":" + server.speech
                + ", book -> " + server.mpvHost() + ":" + server.book);
        log("focus: mode " + (focusActs ? "acting" : "probe (logs only)"));
        if (!server.ownsThePhonesAudio()) {
            // Said out loud because it silences the half of this app that has
            // the most written about it, and a reader of the log a fortnight
            // from now should not have to infer it from a missing line.
            log("focus: not claimed — sound is at " + server.host
                    + ", so there is nothing on this phone to duck");
        }
        LastExit.log(lastExits);
    }

    /**
     * Build and start one side channel, or log why there is not going to be one.
     * Never throws: see the call site.
     */
    private SideChannel startChannel(String name, String label, int port,
                                     String[] observed, int notifId,
                                     SideChannel.Watcher watcher) {
        try {
            SideChannel c = new SideChannel(this, main, name, label,
                                            server.mpvHost(), port, observed,
                                            notifId, CHANNEL, watcher);
            c.start();
            return c;
        } catch (Throwable e) {
            log(name + ": channel unavailable, carrying on without it: " + e);
            return null;
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Post FIRST, on every start, before looking at what the start was for.
        //
        // Each `startForegroundService()` carries its own obligation to call
        // `startForeground()`, and a knock on an already-running service skips
        // `onCreate` — so onCreate's call cannot satisfy a later start's
        // promise. This is what killed the app five times on 2026-08-17
        // (20:24, 20:38, 20:54, 21:09, 21:25) and ANR'd three more starts after
        // it: call_guard revives with `am start .WakeActivity`, WakeActivity
        // calls startForeground**Service**, and when the app was already up
        // nothing here posted. Ten seconds later:
        // ForegroundServiceDidNotStartInTimeException, caller
        // WakeActivity.onCreate:37 in every crash record.
        //
        // So the revive was killing the thing it exists to rescue, and each
        // death is a hole in barge-in, because the mic signal is this app.
        // Re-posting the same notification is cheap and idempotent — the shade
        // does not flicker for an identical one.
        startForeground(NOTIF_ID, buildNotification(),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);

        String action = (intent == null) ? null : intent.getAction();
        if (ACTION_SPEAK_NOW.equals(action)) {
            // For the rest of this voice session, Sam is allowed to talk. Not
            // just this clip: having said yes once, being asked again three
            // sentences later is the interruption we are trying to avoid.
            speakNow = true;
            nm.cancel(NOTIF_WAITING);
            log("live: David said speak now — releasing the hold");
            performSpeech(SpeechPolicy.Action.RESUME);
            pushSessionState();
        } else if (ACTION_LATER.equals(action)) {
            // The card goes; the hold stays. It will be delivered when the
            // session ends, which is what "later" means here.
            nm.cancel(NOTIF_WAITING);
            log("live: later — Sam keeps waiting");
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        main.removeCallbacks(positionPoll);
        main.removeCallbacks(marqueeTick);
        if (pendingFocus != null) main.removeCallbacks(pendingFocus);
        if (ipc != null) ipc.stop();
        if (speech != null) {
            // Best-effort, and before the sender is shut down: a speech pause we
            // still owe outlives this process on the phone's mpv. The write may
            // not make it out; the coordinator clears pause at the start of the
            // next response, which is the backstop.
            if (speechFocus.owesResume()) performSpeech(SpeechPolicy.Action.DISCARD);
            speech.stop();
        }
        if (book != null) book.stop();
        if (mic != null) mic.stop();
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
     * The speech channel's extra bookkeeping: everything the focus policy needs
     * that the card itself does not care about. SideChannel owns the connection
     * and the display; this owns the questions "was a clip just staged", "is the
     * coordinator speaking", and "is a pause of ours still ours".
     */
    private final SideChannel.Watcher speechWatcher = new SideChannel.Watcher() {
        @Override public void onChanged(String property, Object value, MpvState st) {
            // When a clip was staged, so a focus loss arriving before it is
            // audible can still be recognised as ours. A path is set on
            // loadfile; the unpause is the same episode's other edge.
            // Deliberately NOT the clip *ending*: the grace should expire with
            // the reply, not be extended by it.
            boolean staged = ("path".equals(property) && st.path != null)
                    || ("pause".equals(property) && !st.paused);
            if (staged) speechStagedAt = System.currentTimeMillis();
            if (MpvIpc.SPEAKING_PROPERTY.equals(property) && st.speaking) {
                speakingSince = System.currentTimeMillis();
            }
            // The guard the speech policy needs from outside: a resume from
            // anywhere else — the card, the popup, the coordinator clearing
            // pause for the next response — means the pause we owed is no
            // longer ours to pay.
            if ("pause".equals(property)) speechFocus.onPauseChanged(st.paused);
            pushSessionState();
        }
    };

    /** A book takes no part in focus; its card is the whole of its business. */
    private final SideChannel.Watcher bookWatcher = new SideChannel.Watcher() {
        @Override public void onChanged(String property, Object value, MpvState st) {
            pushSessionState();
        }
    };

    /**
     * The music title as the card should show it now — see SideChannel#cardTitle,
     * which is the same rule for the same reason. A long "Artist — Title" runs
     * off the card exactly as a book title does.
     */
    private String musicCardTitle() {
        String full = state.title();
        if (!full.equals(shownMusicTitle)) {
            shownMusicTitle = full;
            musicTitleSince = android.os.SystemClock.uptimeMillis();
        }
        if (!musicScrolling()) return full;
        return Marquee.window(full, Marquee.WIDTH,
                              android.os.SystemClock.uptimeMillis() - musicTitleSince);
    }

    private boolean musicScrolling() {
        return state.playing() && Marquee.needed(state.title(), Marquee.WIDTH);
    }

    /**
     * Advance every scrolling card by a column.
     *
     * One ticker for all three, and it only runs while something is actually
     * scrolling — which for ordinary titles is never. That matters more here
     * than it would elsewhere: notification churn is already an open worry on
     * this app, because the shade's addressed-player slot is what the earbud
     * button follows, and this is the feature most able to make it worse. So it
     * is off unless a card is both playing and too long to fit, and it stops
     * the moment either stops being true.
     */
    private final Runnable marqueeTick = new Runnable() {
        @Override public void run() {
            boolean any = false;
            if (musicScrolling()) {
                any = true;
                try {
                    pushSessionState();
                } catch (Throwable e) {
                    log("marquee: music card failed, carrying on: " + e);
                }
            }
            if (speech != null && speech.scrolling()) { any = true; speech.publish(true); }
            if (book != null && book.scrolling()) { any = true; book.publish(true); }
            marqueeRunning = any;
            if (any) main.postDelayed(this, MARQUEE_TICK_MS);
        }
    };

    /** Start the ticker if anything wants it and it is not already going. */
    private void kickMarquee() {
        if (marqueeRunning) return;
        boolean any = musicScrolling()
                || (speech != null && speech.scrolling())
                || (book != null && book.scrolling());
        if (!any) return;
        marqueeRunning = true;
        main.postDelayed(marqueeTick, MARQUEE_TICK_MS);
    }

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
            if (mic != null) mic.poll();
            pollForQuiet();
            expireSpeechPause();
            kickMarquee();
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
        // ...and it stops while we are listening for the interruption to end.
        // Our zeros are a player like any other: isMusicActive() counts them, so
        // a silent track left running would answer "someone is playing" forever
        // and pollForQuiet() would never fire. To hear whether anyone else is
        // making a noise, stop making one yourself — even a silent one. The cost
        // is the addressed-player slot for the length of the interruption, which
        // is time we are not the player anyway.
        // ...and none of it happens at all when the sound is not on this
        // phone. Every line below is about an mpv we are co-resident with:
        // holding focus on its behalf because it ignores it, and holding the
        // addressed-player slot with a stream of zeros so the earbud reaches
        // it. Point the app at red5 and it is a remote control — taking focus
        // would stop the listener's own music to drive a stereo in another
        // room, and the zeros would win a slot for a player that is not here.
        if (!server.ownsThePhonesAudio()) {
            stopSilence();
            if (focusControl.held()) focusControl.abandon();
        } else {
            boolean listening = speechFocus.owesResume() && !state.loaded();
            if ((state.loaded() || speechFront()) && !listening) startSilence();
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
            // speakingNow() is what holds the claim across the gaps *inside* one
            // reply. The coordinator pauses the broker before each response and
            // sink-speech goes briefly idle between clips, and without this term the
            // app abandoned and re-took focus on every one of those — 08:55:14
            // granted, 08:55:16 abandoned, mid-reply. Each cycle tells whatever we
            // interrupted to resume and then to stop again.
            // Asking for focus back is what we are actually playing, not what we
            // are owed. After a permanent loss the paused clip and its outstanding
            // resume are reasons to KEEP focus, never to take it: re-requesting
            // there would stop the video David just started, one poll after we got
            // out of its way.
            if (state.playing() || speechState().playing()) focusLost = false;

            int wantFocus = state.loaded() ? FocusControl.MUSIC
                    : (speechFront() || speakingNow() || speechFocus.owesResume())
                            ? FocusControl.SPEECH
                            : FocusControl.NONE;
            // A voice session is the one case where taking the output is not
            // obviously the right thing: Live pauses itself and wants a tap back.
            // Music is left alone here — it is the phone's player, and a call or a
            // Live session ducking it is exactly what the focus policy is for.
            if (wantFocus == FocusControl.SPEECH && bargeIn.voiceSession()) {
                if (LIVE_SHARE.equals(liveMode)) wantFocus = FocusControl.NONE;
                else if (LIVE_DUCK.equals(liveMode)) wantFocus = FocusControl.SPEECH_DUCK;
            }
            if (wantFocus != FocusControl.NONE && !focusLost) {
                if (focusControl.kind() != wantFocus && focusControl.request(wantFocus)) {
                    log("focus: granted (" + FocusControl.kindName(wantFocus) + ")");
                }
            } else if (focusControl.held()) {
                focusControl.abandon();
                focus.reset();
                log("focus: abandoned (nothing open)");
            }
        }

        // A music card describing music, which it had not been since the
        // metadata started following the front channel. Speech and book have
        // cards of their own now, so nothing here has to stand in for them —
        // and the listener gets a music pause button that works *while* Sam
        // talks, which one shared card could never offer.
        MediaMetadata.Builder md = new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, musicCardTitle())
                .putString(MediaMetadata.METADATA_KEY_ARTIST, musicSubtitle())
                .putLong(MediaMetadata.METADATA_KEY_DURATION, state.durationMs())
                .putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, Artwork.art("music"));
        session.setMetadata(md.build());

        MpvState front = state;

        int playbackState;
        if (!front.connected) playbackState = PlaybackState.STATE_ERROR;
        else if (!front.loaded()) playbackState = PlaybackState.STATE_STOPPED;
        else if (front.paused) playbackState = PlaybackState.STATE_PAUSED;
        else playbackState = PlaybackState.STATE_PLAYING;

        long actions = PlaybackState.ACTION_PLAY
                | PlaybackState.ACTION_PAUSE
                | PlaybackState.ACTION_PLAY_PAUSE
                | PlaybackState.ACTION_STOP
                | PlaybackState.ACTION_SKIP_TO_NEXT
                | PlaybackState.ACTION_SKIP_TO_PREVIOUS
                | PlaybackState.ACTION_SEEK_TO;

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
        kickMarquee();

        // The other two cards. Both now stay once they have something to say:
        // a book because it is a thing you come back to tomorrow, and speech
        // because the clip you just heard is the one you are most likely to
        // want again. That reverses the earlier rule ("Sam sitting in the shade
        // all evening"), which was right while the card forgot its clip on the
        // way out and so had nothing to offer but a stale play button.
        //
        // The session stays active with it. The spike's finding holds — the
        // addressed-player slot follows the silent AudioTrack, and only the
        // music session opens one — so this competes for the shade, not for the
        // earbud. Worth re-checking on the phone all the same.
        applyLiveHold();
        applyDictationHold();
        applyBookHold();

        // The quiet half of the cue: the count sits on the speech card, which
        // is already in the shade and costs nothing to glance at. The banner is
        // for crossing a threshold; this is for wondering.
        if (speech != null) {
            speech.note(heldForSession
                    ? (speechState().queued > 1
                            ? speechState().queued + " waiting"
                            : "waiting for the conversation to finish")
                    : null);
        }

        if (speech != null) speech.publish(speechFront() || speech.remembers());
        if (book != null) book.publish(book.state().loaded());
    }

    /**
     * The hold tier: while a two-way voice session holds the microphone, Sam
     * waits rather than talking over it, and says so with a card.
     *
     * Called from pushSessionState, so it sees every change either side brings.
     * The card is posted on the transition only — a notify with the same id
     * would re-raise one David had swiped away, and this one is swipeable on
     * purpose.
     */
    private void applyLiveHold() {
        boolean session = bargeIn.voiceSession();
        if (!session) {
            if (heldForSession) {
                heldForSession = false;
                speakNow = false;
                noticedQueue = 0;
                nm.cancel(NOTIF_WAITING);
                log("live: voice session over — Sam carries on");
                performSpeech(SpeechPolicy.Action.RESUME);
            }
            return;
        }
        if (!LIVE_HOLD.equals(liveMode) || speakNow) return;
        // Urgency picks the tier. The enum is the coordinator's own
        // (agent_media_core.types.Priority) and arrives on the broker with the
        // speaking flag, so this is decided before the first word is audible.
        String prio = speechState().priority;
        if ("urgent".equals(prio)) {
            // Take the room. Nothing is held, and pushSessionState's ordinary
            // claim applies — which for a voice session means Live pauses and
            // David gets told, the price of something that could not wait.
            if (heldForSession) {
                heldForSession = false;
                nm.cancel(NOTIF_WAITING);
                performSpeech(SpeechPolicy.Action.RESUME);
            }
            log("live: urgent — taking the room");
            speakNow = true;
            noticedQueue = 0;
            return;
        }
        // Something to hold: a clip playing, or a reply the coordinator has
        // announced but not yet staged.
        if (!(speechState().playing() || speakingNow() || speechFront())) return;
        performSpeech(SpeechPolicy.Action.PAUSE);
        // The pile grew past another threshold while David was not asking to be
        // told. Say so once, not once per reply.
        if (heldForSession && !"low".equals(prio)
                && speechState().queued >= noticedQueue + QUEUE_NUDGE) {
            noticedQueue = speechState().queued;
            log("live: " + speechState().queued + " replies waiting — asking again");
            nm.notify(NOTIF_WAITING, waitingCard());
            toast("Sam has " + speechState().queued
                    + " replies waiting — pull down to answer");
        }
        if (!heldForSession) {
            heldForSession = true;
            log("live: holding Sam while the voice session runs (" + prio + ")");
            // Low is the ambient tier: it waits, and it does not ask. Anything
            // else is worth a card, because a reply to something David said is
            // worth telling him about even when it can wait.
            noticedQueue = speechState().queued;
            if (!"low".equals(prio)) {
                nm.notify(NOTIF_WAITING, waitingCard());
                // And a toast beside it, because the banner did not arrive.
                //
                // A heads-up notification is at the mercy of three things we do
                // not control: the channel's importance as the system has it
                // (not as we asked for it), Do Not Disturb, and whatever
                // Bedtime mode is doing at 00:41. A toast is none of those — it
                // is not a notification at all, so nothing suppresses it. It
                // cannot carry the two buttons, so it says where they are.
                toast("Sam has something to say — pull down to answer");
            }
        }
    }

    /**
     * The quiet half: Sam waits while the mic is open for dictation.
     *
     * Silent on purpose — no card, no toast. A dictation lasts seconds, and a
     * notification every time David talks to his keyboard would be worse than
     * the problem. The log line is there for {@code /log}, which is how this
     * gets diagnosed from red5.
     *
     * Called from pushSessionState like {@link #applyLiveHold}, so it sees both
     * the mic changing and speech starting — the second one matters, because a
     * reply staged mid-dictation lands on a broker whose pause the coordinator
     * has just cleared.
     */
    private void applyDictationHold() {
        boolean micOpen = mic != null && mic.active();
        boolean audible = speechState().playing();
        boolean wasHolding = dictation.holding();
        boolean wasExpired = dictation.expired();
        DictationHold.Action action = dictation.onState(
                micOpen, bargeIn.voiceSession(), audible, System.currentTimeMillis());

        if (dictation.holding() && !wasHolding) {
            dictationRate.engaged(System.currentTimeMillis());
            log("dictation: mic open — Sam waits");
        } else if (!dictation.holding() && wasHolding) {
            log("dictation: mic shut — Sam carries on");
        } else if (dictation.expired() && !wasExpired) {
            log("dictation: mic still open after "
                    + (DictationHold.MAX_HOLD_MS / 1000)
                    + "s — that is not dictation, letting Sam speak");
        }
        // PAUSE arrives on every push while speech is audible and the mic is
        // open, which is the re-assert; only the first is worth a line.
        if (action == DictationHold.Action.PAUSE && !wasHolding) {
            log("dictation: pausing the clip in flight");
        }
        if (action == DictationHold.Action.RESUME && heldForSession) {
            // A dictation that turns into a Live session hands its pause back
            // — but applyLiveHold has just run, and it wants Sam held for the
            // conversation. Un-pausing here would have Sam start talking at the
            // exact moment the session began, which is the failure both holds
            // exist to prevent. Let the bookkeeping clear and leave the broker
            // where the other hold put it; it owns the resume from now on.
            log("dictation: the conversation takes over the hold");
            return;
        }
        if (action != DictationHold.Action.NONE) {
            performSpeech(action == DictationHold.Action.PAUSE
                    ? SpeechPolicy.Action.PAUSE
                    : SpeechPolicy.Action.RESUME);
        }
    }

    /**
     * The third hold: the book stops for a conversation.
     *
     * The one channel with no handler on this route until 2026-08-16 — see
     * {@link BookHold} for why a conversation reaches neither {@code call_guard}
     * nor the focus policy, and why the book is held coarsely rather than
     * ducked and handed back per utterance.
     *
     * Called from pushSessionState with the other two, so it sees the session
     * starting and the book's own state changing. Silent apart from the log:
     * the book's card already shows the pause, which is a better place to
     * notice it than a second notification.
     */
    private void applyBookHold() {
        if (book == null) return;
        boolean audible = book.state().playing();
        boolean wasHolding = bookHold.holding();
        boolean wasSurrendered = bookHold.surrendered();
        // A call records as VOICE_COMMUNICATION exactly like Live does, so this
        // is the only thing that tells them apart — and it decides the resume,
        // not the pause. MODE_IN_CALL is telephony's own; an app cannot put the
        // phone into it.
        boolean inCall = audio != null
                && audio.getMode() == AudioManager.MODE_IN_CALL;
        BookHold.Action action = bookHold.onState(
                bargeIn.voiceSession(), audible, inCall, System.currentTimeMillis());

        if (bookHold.holding() && !wasHolding) {
            log("book: conversation started — " + bookHold.why());
        } else if (!bookHold.holding() && wasHolding) {
            log("book: conversation over — " + (action == BookHold.Action.RESUME
                    ? "picking the book back up"
                    : "leaving the book where it is"));
        } else if (bookHold.surrendered() && !wasSurrendered) {
            log("book: playing again with the conversation still going — "
                    + "that was David, staying out of it");
        }

        if (action == BookHold.Action.PAUSE) {
            log("book: pausing for the conversation");
            book.ipc().setProperty("pause", Boolean.TRUE);
        } else if (action == BookHold.Action.RESUME) {
            book.ipc().setProperty("pause", Boolean.FALSE);
        }
    }

    /**
     * The music card's second line: who it is by, or where it is in the queue.
     *
     * The artist comes from mpv's own tag metadata, so it is there for a local
     * file and absent for a stream — and absent is fine. See {@link CardText}.
     */
    private String musicSubtitle() {
        return CardText.music(state.artist, state.playlistPos, state.queued);
    }

    /** Best-effort; a missed toast is never worth the process. */
    private void toast(String text) {
        try {
            Toast.makeText(this, text, Toast.LENGTH_LONG).show();
        } catch (Throwable e) {
            log("toast failed, carrying on: " + e);
        }
    }

    /** "Sam has something to say", with the two answers to it. */
    private Notification waitingCard() {
        PendingIntent speak = PendingIntent.getService(this, 1,
                new Intent(this, CompanionService.class).setAction(ACTION_SPEAK_NOW),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent later = PendingIntent.getService(this, 2,
                new Intent(this, CompanionService.class).setAction(ACTION_LATER),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        String what = speech == null ? null : speech.state().lastTitle();
        int waiting = speechState().queued;
        return new Notification.Builder(this, CHANNEL_ASK)
                .setContentTitle(waiting > 1 ? "Sam has " + waiting
                                               + " replies waiting"
                                             : "Sam has something to say")
                .setContentText(what != null ? what
                        : "Held while the voice session is running")
                .setSmallIcon(android.R.drawable.ic_media_pause)
                .addAction(new Notification.Action.Builder(
                        null, "Speak now", speak).build())
                .addAction(new Notification.Action.Builder(
                        null, "Later", later).build())
                // A question, so it floats and it times out on its own: left
                // alone, the hold delivers when the session ends anyway, and a
                // banner still sitting there tomorrow would be a lie.
                .setCategory(Notification.CATEGORY_MESSAGE)
                .setTimeoutAfter(5 * 60 * 1000L)
                .setAutoCancel(true)
                .setOngoing(false)
                .build();
    }

    private Notification buildNotification() {
        PendingIntent open = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        // Only the thing nothing else can say. "playing" / "paused" next to a
        // play/pause button was the card saying it twice; who it is by, or
        // where it sits in the queue, is what the line is worth spending on.
        // An unreachable mpv is the exception: that one is not visible anywhere
        // else on this surface.
        String text = state.connected ? musicSubtitle()
                : "mpv unreachable on " + server.mpvHost() + ":" + server.music;

        return new Notification.Builder(this, CHANNEL)
                // Not gated on loaded() any more: MpvState#title falls back to
                // the last track played, which is a better thing for an idle
                // music card to say than the app's own name.
                .setContentTitle(musicCardTitle())
                .setContentText(text)
                // The channel's mark rather than a transport glyph. The old
                // icon reported play/pause, which the card's own button already
                // does; what the status bar cannot otherwise say is *which* of
                // the three channels is up there.
                .setSmallIcon(Artwork.icon("music"))
                .setLargeIcon(Artwork.art("music"))
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

            ButtonPolicy.Press press = ButtonPolicy.interpret(key.getKeyCode(), state);
            if (press == ButtonPolicy.Press.DEFAULT) {
                return super.onMediaButtonEvent(intent);
            }

            // Consume both the down and the up, so the framework does not also
            // translate the key and undo what we just did.
            if (down) {
                log("button: " + name + " read as " + press + " — music is "
                        + (state.paused ? "paused" : "playing"));
                if (press == ButtonPolicy.Press.PLAY) onPlay(); else onPause();
            }
            return true;
        }

        /**
         * This card drives music, full stop. It spent one morning routing to
         * whichever channel was in front — a necessary trick while one card had
         * to describe two things, and one that could never let the listener
         * pause the music *while* Sam talked. Sam has his own card now.
         */
        @Override public void onPlay() {
            log("transport: play -> music");
            ipc.setProperty("pause", Boolean.FALSE);
        }

        @Override public void onPause() {
            log("transport: pause -> music");
            ipc.setProperty("pause", Boolean.TRUE);
        }

        @Override public void onStop() {
            log("transport: stop -> music");
            ipc.command("stop");
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

    /**
     * Which build is actually running — the commit build.sh stamped into
     * versionName. Sideloading is a file copy and a tap through a chooser, and
     * an install that silently does not take looks exactly like a fix that does
     * not work. On 2026-08-15 it cost a round trip of arguing with code the
     * phone had never run.
     */
    private String buildStamp() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {   // NameNotFound is impossible for ourselves
            return "unknown";
        }
    }

    // ---- the front channel, for the card and its buttons -----------------

    /**
     * A clip is paused by someone who means to resume it: the speech card's own
     * pause button, or a focus pause of ours. Either way its card stays, so the
     * button that paused it can start it again.
     */
    private boolean speechHeldNow() {
        return (speech != null && speech.heldByUser()) || speechFocus.owesResume();
    }

    /**
     * Speech is audible or held. It decides the speech card's visibility, the
     * silent track and the focus claim — display questions no longer among them,
     * now that each channel has a card of its own.
     */
    private boolean speechFront() {
        return FrontChannel.speechInFront(speechState(), speechHeldNow());
    }

    /** The coordinator says a response is in flight, and said so recently. */
    private boolean speakingNow() {
        return speechState().speaking && sinceSpeaking() < FrontChannel.SPEAKING_FLAG_MAX_MS;
    }

    // ---- the outside readout ---------------------------------------------

    /**
     * What /state answers. Deliberately covers the questions that could not be
     * answered from red5 before it existed: is the app acting or only probing,
     * does it hold focus, what focus changes has it actually seen, and does it
     * still owe mpv anything.
     */
    /**
     * Bring up the in-app speech player on a port of its own.
     *
     * <b>Beside mpv, not instead of it.</b> The phone's socat bridges keep
     * 6602 and the Termux mpv keeps answering there; this listens on
     * {@link Server#BUILTIN_SPEECH_PORT}, so a target is moved into the app by
     * pointing one environment variable at the new port —
     * {@code MEDIA_SPEECH_SOCKET_<TARGET>=tcp://<phone>:6612} — and moved back
     * by unsetting it. Nothing switches implicitly, and the old path is never
     * more than an env var away, which is the whole reason to run both.
     *
     * It binds the tailnet address where red5 will look for it, and loopback
     * for anything on the phone, exactly as the socat bridges do.
     */
    private void startBuiltinSpeech() {
        try {
            builtinSpeech = new BuiltinSpeech(this, CompanionService::log);
            LIVE_SPEECH = builtinSpeech;
            // The card, the focus policy and the hold tiers read one state per
            // channel. Give them this player's, and let SideChannel choose
            // between it and the mpv bridge by which one is speaking.
            builtinSpeech.mirrorInto(builtinSpeechState,
                                     () -> main.post(this::pushSessionState));
            speechServer = new MpvServer(Server.tailnetAddress(),
                                         Server.BUILTIN_SPEECH_PORT,
                                         builtinSpeech,
                                         CompanionService::log);
            builtinSpeech.attach(speechServer);
            speechServer.start();
        } catch (Throwable t) {
            // Optional by construction, like the side channels: a phone that
            // cannot bind the port keeps every other thing this service does.
            log("builtin speech unavailable: " + t);
        }
    }

    /**
     * What the in-app player is playing, as a channel row, or null.
     *
     * Null means "not the one making the noise" — either there is no in-app
     * player or it has nothing open — and the caller should believe the server
     * as it always has.
     */
    static Channels.Channel builtinSpeechNow() {
        BuiltinSpeech player = LIVE_SPEECH;
        if (player == null || !player.active()) return null;
        return player.asChannel();
    }

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
            m.put("metadata_title", state.title());
            // Which cards are in the shade's player right now. One session per
            // channel since 2026-08-15; only music holds an AudioTrack.
            m.put("cards", (speech != null && speech.visible() ? "music,speech" : "music")
                    + (book != null && book.visible() ? ",book" : ""));
            Map<String, Object> sp = new LinkedHashMap<String, Object>();
            sp.put("connected", Boolean.valueOf(speechState().connected));
            sp.put("idle", Boolean.valueOf(speechState().idleActive));
            sp.put("paused", Boolean.valueOf(speechState().paused));
            long since = sinceSpeechStaged();
            long flag = sinceSpeaking();
            sp.put("staged_ms_ago", since == Long.MAX_VALUE ? null : Long.valueOf(since));
            // What the coordinator told us, and how long ago — the difference
            // between "we know" and "we guessed" when reading a duck decision.
            sp.put("speaking", Boolean.valueOf(speechState().speaking));
            sp.put("speaking_ms_ago", flag == Long.MAX_VALUE ? null : Long.valueOf(flag));
            // The answer that decides whether a transient loss ducks at all.
            sp.put("owns_the_loss", Boolean.valueOf(
                    FrontChannel.ourSpeech(speechState(), since, flag)));
            // Whether the speech broker is paused *by us* — the one pause on
            // this phone that nothing else will undo. See SpeechPolicy.
            sp.put("owes_resume", Boolean.valueOf(speechFocus.owesResume()));
            sp.put("card", Boolean.valueOf(speech != null && speech.visible()));
            sp.put("card_failed", Boolean.valueOf(speech != null && speech.failed()));
            m.put("speech", sp);
            Map<String, Object> bk = new LinkedHashMap<String, Object>();
            MpvState bs = book == null ? new MpvState() : book.state();
            bk.put("connected", Boolean.valueOf(bs.connected));
            bk.put("idle", Boolean.valueOf(bs.idleActive));
            bk.put("paused", Boolean.valueOf(bs.paused));
            bk.put("title", bs.title());
            bk.put("card", Boolean.valueOf(book != null && book.visible()));
            bk.put("card_failed", Boolean.valueOf(book != null && book.failed()));
            m.put("book", bk);
            m.put("last_button", lastButton);
            // Which agent-media this phone is a client of, and where its sound
        // comes out — the first two questions to ask of a readout that no
        // longer describes one fixed arrangement. Never the token.
        Map<String, Object> srv = new LinkedHashMap<String, Object>();
        srv.put("host", server.host);
        srv.put("control_port", server.control);
        srv.put("playback", server.playback);
        srv.put("mpv_host", server.mpvHost());
        srv.put("local", server.local());
        srv.put("token", !server.token.isEmpty());
        m.put("server", srv);
        m.put("focus_mode", focusActs ? "acting" : "probe");
            m.put("focus_held", Boolean.valueOf(focusControl != null && focusControl.held()));
            // True while another app owns the output and we are staying out of
            // its way. Nothing can reach us through the focus listener here.
            m.put("focus_lost", Boolean.valueOf(focusLost));
            m.put("build", buildStamp());
            // Why the previous process went away — the question `/crash` cannot
            // answer, because only a throw ever reaches the crash recorder.
            m.put("last_exit", new ArrayList<String>(lastExits));
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
            // The mic probe. `mic_seen` is the finding: what a non-privileged
            // app is actually shown about a recording it does not own.
            m.put("mic_active", Boolean.valueOf(mic != null && mic.active()));
            m.put("mic_count", Integer.valueOf(mic == null ? 0 : mic.count()));
            m.put("mic_seen", mic == null ? "(no probe)" : mic.detail());
            m.put("mic_verdict", bargeIn.why(System.currentTimeMillis()));
            // The rate, always, and the complaint only when there is one: a
            // health line that is present on a healthy phone gets skimmed past.
            long nowMs = System.currentTimeMillis();
            m.put("dictation_holds_1h",
                  Integer.valueOf(dictationRate.recent(nowMs)));
            String rateProblem = dictationRate.problem(nowMs);
            if (!rateProblem.isEmpty()) m.put("dictation_rate", rateProblem);
            m.put("live_mode", liveMode);
            m.put("speech_priority", speechState().priority);
            m.put("speech_waiting", Integer.valueOf(speechState().queued));
            m.put("voice_session", Boolean.valueOf(bargeIn.voiceSession()));
            // The dictation half of the same question. `dictation_owes_resume`
            // is the one to check when Sam has gone quiet and nobody knows why.
            m.put("dictation", dictation.why());
            m.put("dictation_holding", Boolean.valueOf(dictation.holding()));
            m.put("dictation_owes_resume", Boolean.valueOf(dictation.owesResume()));
            // And the book's, which answers the other "why has this gone quiet"
            // — or, before the fix, "why has this NOT gone quiet".
            m.put("book_hold", bookHold.why());
            m.put("book_hold_owes_resume", Boolean.valueOf(bookHold.owesResume()));
            m.put("mic_events", mic == null
                    ? new ArrayList<String>() : mic.history());
            return Json.write(m);
        }

        @Override public String log() {
            return dump();
        }

        @Override public String mic() {
            if (mic == null) return "0 (no probe)";
            // The answer is "should this hold the audio down", not "is the mic
            // on" — a voice session holds the mic for its whole length and is
            // nobody talking over Sam. See BargeIn.
            long now = System.currentTimeMillis();
            boolean hold = mic.active() && bargeIn.holding(now);
            return (hold ? "1" : "0") + " n=" + mic.count()
                    + " " + bargeIn.why(now) + " " + mic.detail();
        }

        @Override public String live(String set) {
            if (set != null && !set.isEmpty()) {
                if (LIVE_YIELD.equals(set) || LIVE_DUCK.equals(set)
                        || LIVE_SHARE.equals(set) || LIVE_HOLD.equals(set)) {
                    liveMode = set;
                    prefs.edit().putString(KEY_LIVE_MODE, set).apply();
                    CompanionService.log("live mode: " + set);
                    // Re-decide now rather than at the next mpv property: the
                    // point of setting this is to hear the difference.
                    main.post(() -> pushSessionState());
                } else {
                    return "unknown mode: " + set + " (hold|yield|duck|share)\n";
                }
            }
            return liveMode + " (voice session: "
                    + (bargeIn.voiceSession() ? "yes" : "no") + ", focus: "
                    + FocusControl.kindName(focusControl == null
                            ? FocusControl.NONE : focusControl.kind())
                    + ")\n";
        }

        @Override public String crash() {
            return Crash.read(getFilesDir());
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
        // Told in every mode, like the history above: whether this recording is
        // a conversation is a fact about the phone, not something we do to it,
        // and a probe-mode build should answer it as well as an acting one.
        bargeIn.onFocus(change, System.currentTimeMillis());

        // Whatever the mode, the bookkeeping follows the framework: a permanent
        // loss kills the request we registered, and believing otherwise is how
        // the app stopped hearing anything at all after 09:09:01.
        if (change == FocusPolicy.LOSS) {
            focusControl.lost();
            focusLost = true;
            log("focus: lost for good — will re-request when we next play");
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
                speechState(), sinceSpeechStaged(), sinceSpeaking());
        List<FocusPolicy.Action> actions = focus.onFocusChange(change, state, ourSpeech);
        List<SpeechPolicy.Action> speechActions =
                speechFocus.onFocusChange(change, speechState(), ourSpeech, System.currentTimeMillis());
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
        // While the two players coexist, "pause speech" must mean whichever
        // one is speaking. These policies were all written when speech could
        // only be the Termux mpv, and the first reply played by this app went
        // straight through a dictation hold that paused the wrong player.
        BuiltinSpeech builtin = builtinSpeech;
        if (builtin != null && builtin.active()) {
            switch (action) {
                case PAUSE:
                    log("focus: pause speech (in-app player)");
                    builtin.pause(true);
                    return;
                case RESUME:
                    log("focus: resume speech (in-app player)");
                    builtin.pause(false);
                    return;
                case DISCARD:
                    log("focus: discard the stale clip (in-app player)");
                    builtin.stop();
                    return;
                default:
                    return;
            }
        }
        if (speechIpc == null) return;
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
     * Notice that an interruption is over when nothing said so.
     *
     * Android sends a GAIN after a transient loss and <em>nothing at all</em>
     * after a permanent one — and a permanent loss is what a real media app
     * takes. The YouTube app claimed the output with AUDIOFOCUS_GAIN on p8a at
     * 08:58:19, so the two-minute resume window could never fire for it: the
     * signal that starts it does not exist.
     *
     * So we listen instead. {@code isMusicActive()} is true while any app is
     * playing on the music stream; when it goes false, whatever took the output
     * has stopped using it. That is a heuristic where the focus callback is a
     * fact, which is why it is bounded three ways: only while a pause of ours is
     * outstanding, only when our own music mpv is idle (otherwise we would be
     * hearing ourselves), and only after two consecutive quiet polls, so an app
     * that takes focus and pauses for breath does not get talked over.
     *
     * It reuses the GAIN branch of the policy, so the two-minute window and the
     * manual-resume rule apply exactly as they do to a real one.
     *
     * The catch worth knowing: while our own music is loaded — even paused — we
     * do not listen at all, because the silent track and mpv make the answer
     * about us. That case falls through to the five-minute deadline.
     */
    private void pollForQuiet() {
        if (!speechFocus.owesResume() || state.loaded() || audio == null) {
            quietPolls = 0;
            return;
        }
        if (audio.isMusicActive()) {
            quietPolls = 0;
            return;
        }
        if (++quietPolls < QUIET_POLLS_TO_RESUME) return;
        quietPolls = 0;
        List<SpeechPolicy.Action> actions = speechFocus.onFocusChange(
                FocusPolicy.GAIN, speechState(), false, System.currentTimeMillis());
        if (actions.isEmpty()) return;   // outside the window: David's to lift
        log("focus: nothing else is playing any more");
        for (SpeechPolicy.Action action : actions) performSpeech(action);
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
