package net.agentmedia.companion;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.media.PlaybackParams;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.util.ArrayList;
import java.util.List;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Speech played by this app, behind the socket the server already talks to.
 *
 * <h4>Why this exists</h4>
 *
 * Every focus mechanism on this phone — the silent {@code AudioTrack}, the
 * external-hold flag, {@code FocusPolicy}, the duck/restore bookkeeping — is
 * there because mpv ignores Android audio focus and somebody has to hold it on
 * mpv's behalf. A player inside the app needs none of it: Android ducks our
 * stream because it is our stream.
 *
 * <h4>Fetch, then play</h4>
 *
 * The spike measured {@code MediaPlayer.prepare()} at 8.5–9.8 seconds against
 * red5's clip server and 44–78 ms from a local file, with rebuffers mid-clip.
 * A reply is many short clips, so streaming each one would put eight seconds in
 * front of every sentence. Clips are downloaded to the cache and played from
 * disk, and the next one is fetched while the current one plays — which is also
 * what keeps the gap between sentences closed, the job {@code gapless-audio}
 * used to do.
 *
 * <h4>What it owes the protocol</h4>
 *
 * {@link MpvServer.Player}, and one obligation beyond answering questions: when
 * playback moves on its own — a clip ends and the next begins — it must
 * <em>volunteer</em> {@code playlist-pos}, because that is how the coordinator
 * follows a reply sentence by sentence. mpv does this; an app that only
 * answered when asked would break highlighting while looking correct.
 *
 * @see MpvServer
 */
final class BuiltinSpeech implements MpvServer.Player {

    /** All player calls happen here: MediaPlayer is not thread-safe. */
    private final ExecutorService worker =
            Executors.newSingleThreadExecutor(r -> {
                Thread t = new Thread(r, "builtin-speech");
                t.setDaemon(true);
                return t;
            });

    /**
     * Clip fetches in flight, so a clip is not downloaded twice.
     *
     * Two things race for the same file the moment a reply is queued: the
     * whole-reply prefetch below and {@link #prepareNext()}. Both are
     * idempotent, but on a link this slow the duplicate is a second copy of
     * the same bytes competing with the one that is about to be played.
     */
    private final Set<String> fetching =
            Collections.synchronizedSet(new HashSet<String>());

    /** Two at a time: enough to stay ahead, few enough to not fight the clip
     *  that is playing for a share of a slow tailnet link. */
    private final ExecutorService fetcher = Executors.newFixedThreadPool(2, r -> {
        Thread t = new Thread(r, "speech-fetch");
        t.setDaemon(true);
        return t;
    });

    private final Context context;
    private final Log log;

    private final List<String> playlist = new ArrayList<String>();
    private volatile int pos = -1;
    private volatile MediaPlayer player;
    /** The next clip, prepared and handed to Android for a seamless join. */
    private volatile MediaPlayer next;
    private volatile int nextIndex = -1;

    private volatile boolean paused;
    private volatile boolean muted;
    private volatile double volume = 100;
    private volatile double speed = 1.0;

    /** Set once the server is listening, so changes can be volunteered. */
    private volatile MpvServer server;

    interface Log {
        void line(String text);
    }

    BuiltinSpeech(Context context, Log log) {
        this.context = context.getApplicationContext();
        this.log = log;
    }

    void attach(MpvServer server) {
        this.server = server;
        // Every change, from either side of the socket: playback moving, and
        // the server naming the reply. Both belong on the card.
        server.onAnyChange(this::mirror);
    }

    /**
     * Publish this player's state where the card and the policies read it.
     *
     * The same {@link MpvState} shape the mpv bridge fills, because everything
     * downstream — the shade card, the focus policy, the hold tiers, the
     * marquee — was written against that and none of it should have to learn
     * that speech has two players. What decides which one they see is
     * {@link SideChannel#mirror}, not anything here.
     */
    void mirrorInto(MpvState out, Runnable onChange) {
        this.mirror = out;
        this.onMirrorChange = onChange;
        mirror();
    }

    private volatile MpvState mirror;
    private volatile Runnable onMirrorChange;

    private void mirror() {
        MpvState out = mirror;
        if (out == null) return;
        MpvServer s = server;
        // connected means "this player can be asked", which is true whenever
        // it exists — there is no socket to lose.
        out.connected = true;
        out.idleActive = idle();
        out.paused = paused;
        out.speed = speed;
        out.volume = volume;
        out.path = path();
        out.position = timePos();
        out.duration = duration();
        if (s != null) {
            String title = s.storedText("force-media-title");
            out.mediaTitle = title.isEmpty() ? null : title;
            out.speaking = s.storedFlag("user-data/agent-media/speaking");
            String priority = s.storedText("user-data/agent-media/priority");
            if (!priority.isEmpty()) out.priority = priority;
            String text = s.storedText("user-data/agent-media/text");
            out.replyText = text.isEmpty() ? null : text;
        }
        Runnable r = onMirrorChange;
        if (r != null) r.run();
    }

    /**
     * Is this player the one making noise right now?
     *
     * Asked by the app's own policies — barge-in, the dictation hold, the
     * focus rules — which were all written when the only speech on this phone
     * came out of the Termux mpv and therefore all drive that socket. While
     * both players exist, "pause speech" has to mean the one that is actually
     * speaking, and the honest test is whether this one has a clip open.
     */
    boolean active() {
        // NOT "a MediaPlayer exists". Between two sentences there is none for a
        // moment — the last is released and the next has not been adopted — and
        // a parked reply has none at all. Reading those as "not ours" handed
        // the channel back to the idle mpv on 6602, so the card, the shade, the
        // car display and /state showed a reply from hours ago while this
        // player held the current one. Sampled on p8a: five of six probes
        // during one reply said idle=true, paused=true.
        //
        // What it means instead is "this player is responsible for the speech
        // channel": something is open, or a reply is loaded and recent.
        if (player != null) return true;
        if (pos < 0 || playlist.isEmpty()) return false;
        // A parked reply is only interesting while it is recent. Without this
        // bound, moving the target back to mpv would leave this player
        // claiming a channel nobody will send it another command for.
        return System.currentTimeMillis() - lastCommandAt < PARKED_MS;
    }

    /** How long a finished reply still counts as this player's. */
    private static final long PARKED_MS = 10 * 60 * 1000;

    private volatile long lastCommandAt = 0;

    /**
     * This player as a row the home screen understands.
     *
     * The same shape media-share returns for a channel, so the screen needs no
     * special case beyond preferring this one — see
     * {@link CompanionService#builtinSpeechNow()}. Title and conversation come
     * from what the server set over the socket, because a clip's filename is
     * {@code remote-20260814T190922-18480.mp3} and that is worse than nothing.
     */
    Channels.Channel asChannel() {
        MpvServer s = server;
        String title = s == null ? "" : s.storedText("force-media-title");
        String text = s == null ? "" : s.storedText("user-data/agent-media/text");
        double pos = timePos();
        double dur = duration();
        return new Channels.Channel(
                "speech",
                idle(),
                !paused && !idle(),
                paused,
                title.isEmpty() ? null : title,
                null,
                pos < 0 ? null : Long.valueOf((long) (pos * 1000)),
                dur < 0 ? null : Long.valueOf((long) (dur * 1000)),
                Double.valueOf(speed),
                Integer.valueOf((int) Math.round(volume)),
                muted,
                0,
                java.util.Collections.<String>emptySet(),
                text.isEmpty() ? null : text);
    }

    private void volunteer(String property) {
        MpvServer s = server;
        if (s != null) {
            // changed() calls back into mirror() through onAnyChange, so the
            // card and the socket learn the same thing at the same moment.
            s.changed(property);
        } else {
            mirror();
        }
    }

    // ---- the playlist -----------------------------------------------------

    @Override
    public void load(String uri, String mode) {
        lastCommandAt = System.currentTimeMillis();
        worker.execute(() -> {
            if ("replace".equals(mode)) {
                playlist.clear();
                playlist.add(uri);
                pos = 0;
                startCurrent();
            } else {
                // append does NOT auto-play, and the sink depends on that: it
                // builds a whole reply into an idle player before jumping to
                // index 0, because a first clip that started early could end
                // before the rest were queued.
                playlist.add(uri);
                // That same batch is the earliest this device can know the
                // whole reply, so start fetching all of it now rather than one
                // clip ahead. The old path pushed the clips to the phone while
                // the sentences were still rendering; this is the nearest thing
                // available from behind the socket, and it turns every sentence
                // after the first into a local file.
                warm(uri);
                volunteer("playlist-count");
            }
        });
    }

    @Override
    public void playlistClear() {
        worker.execute(() -> {
            String current = current();
            playlist.clear();
            if (current != null) {
                playlist.add(current);
                pos = 0;
            } else {
                pos = -1;
            }
            volunteer("playlist-count");
        });
    }

    @Override
    public void stop() {
        worker.execute(() -> {
            release();
            playlist.clear();
            pos = -1;
            volunteer("playlist-pos");
            volunteer("idle-active");
        });
    }

    @Override
    public void playlistPos(int index) {
        lastCommandAt = System.currentTimeMillis();
        worker.execute(() -> {
            pos = index;
            if (index < 0 || index >= playlist.size()) {
                release();
                } else {
                startCurrent();
            }
            volunteer("playlist-pos");
        });
    }

    @Override
    public void playlistNext() {
        worker.execute(() -> advance(true));
    }

    @Override
    public void playlistPrev() {
        worker.execute(() -> {
            if (pos > 0) {
                pos--;
                startCurrent();
                volunteer("playlist-pos");
            }
        });
    }

    /** The end of a clip, or an explicit next. */
    private void advance(boolean announce) {
        if (pos + 1 < playlist.size()) {
            pos++;
            startCurrent();
        } else {
            release();
            pos = playlist.isEmpty() ? -1 : playlist.size() - 1;
            volunteer("idle-active");
        }
        if (announce) volunteer("playlist-pos");
    }

    // ---- playback ---------------------------------------------------------

    private void startCurrent() {
        String uri = current();
        if (uri == null) return;
        release();
        try {
            File file = local(uri);
            MediaPlayer mp = new MediaPlayer();
            mp.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            mp.setDataSource(file.getAbsolutePath());
            mp.prepare();
            mp.setOnCompletionListener(m -> worker.execute(this::onClipEnded));
            mp.setOnErrorListener((m, what, extra) -> {
                // One clip failing must not strand the rest of the reply:
                // the sentence is lost, the paragraph continues.
                log.line("builtin-speech: clip failed (" + what + "/" + extra
                        + "), skipping");
                worker.execute(() -> advance(true));
                return true;
            });
            applyGain(mp);
            player = mp;
            if (!paused) {
                applySpeed(mp);
                mp.start();
            }
            volunteer("playlist-pos");
            prepareNext();
        } catch (Exception e) {
            log.line("builtin-speech: " + uri + " failed: " + e);
            worker.execute(() -> advance(true));
        }
    }

    /**
     * Speed, with pitch held and no silent fallback.
     *
     * The spike measured this exact call at 1.602 for a requested 1.6 — and
     * measured it rather than believing the player, because mpv's pinned
     * scaletempo2 reported 1.6 while playing 1.18. {@code AUDIO_FALLBACK_MODE_FAIL}
     * is what keeps an unsupportable speed from becoming a quiet resample.
     */
    private void applySpeed(MediaPlayer mp) {
        PlaybackParams p = new PlaybackParams();
        p.setAudioFallbackMode(PlaybackParams.AUDIO_FALLBACK_MODE_FAIL);
        p.setPitch(1.0f);
        p.setSpeed((float) speed);
        mp.setPlaybackParams(p);
    }

    private void applyGain(MediaPlayer mp) {
        float g = muted ? 0f : (float) Math.max(0, Math.min(100, volume)) / 100f;
        mp.setVolume(g, g);
    }

    @Override
    public void pause(boolean wanted) {
        lastCommandAt = System.currentTimeMillis();
        worker.execute(() -> {
            paused = wanted;
            MediaPlayer mp = player;
            if (mp == null) return;
            try {
                if (wanted) {
                    if (mp.isPlaying()) mp.pause();
                } else if (!mp.isPlaying()) {
                    applySpeed(mp);
                    mp.start();
                }
            } catch (IllegalStateException ignored) {
                // A player torn down underneath us; the next clip rebuilds it.
            }
            volunteer("pause");
        });
    }

    @Override
    public void mute(boolean wanted) {
        worker.execute(() -> {
            muted = wanted;
            MediaPlayer mp = player;
            if (mp != null) applyGain(mp);
            volunteer("mute");
        });
    }

    @Override
    public void volume(double wanted) {
        worker.execute(() -> {
            volume = wanted;
            MediaPlayer mp = player;
            if (mp != null) applyGain(mp);
            volunteer("volume");
        });
    }

    @Override
    public void speed(double wanted) {
        worker.execute(() -> {
            speed = wanted <= 0 ? 1.0 : wanted;
            MediaPlayer mp = player;
            try {
                if (mp != null && mp.isPlaying()) applySpeed(mp);
            } catch (IllegalStateException ignored) {
            }
            volunteer("speed");
        });
    }

    // ---- what the protocol asks -------------------------------------------

    @Override public boolean paused() { return paused; }
    @Override public boolean muted() { return muted; }
    @Override public double volume() { return volume; }
    @Override public double speed() { return speed; }
    @Override public int playlistPos() { return pos; }
    @Override public int playlistCount() { return playlist.size(); }
    @Override public String path() { return current(); }

    @Override
    public double timePos() {
        MediaPlayer mp = player;
        try {
            return mp == null ? -1 : mp.getCurrentPosition() / 1000.0;
        } catch (IllegalStateException e) {
            return -1;
        }
    }

    @Override
    public double duration() {
        MediaPlayer mp = player;
        try {
            return mp == null ? -1 : mp.getDuration() / 1000.0;
        } catch (IllegalStateException e) {
            return -1;
        }
    }

    @Override
    public boolean idle() {
        return player == null && pos < 0;
    }

    private String current() {
        int p = pos;
        return p >= 0 && p < playlist.size() ? playlist.get(p) : null;
    }

    // ---- fetching ---------------------------------------------------------

    /**
     * The clip as a file on this device, fetching it if it is a URL.
     *
     * A {@code file://} or bare path is used where it lies — the server may
     * have pre-staged the whole reply, and copying it again would be work for
     * nothing.
     */
    private File local(String uri) throws Exception {
        if (uri.startsWith("http://") || uri.startsWith("https://")) {
            return fetch(uri);
        }
        return new File(uri.startsWith("file://") ? uri.substring(7) : uri);
    }

    /**
     * Fetch and prepare the next clip, and hand it to Android to start itself.
     *
     * {@code setNextMediaPlayer} is what closes the gap between sentences —
     * the framework starts the prepared player the instant this one ends,
     * with no fetch, no prepare and no round trip in between. The first
     * end-to-end reply measured about 0.7s of silence per sentence without it,
     * against mpv's gapless playlist, which is a regression a listener would
     * notice in every reply.
     *
     * Fetch and prepare happen off the worker so a slow clip cannot stall
     * commands arriving on the socket; only the attach hops back.
     */
    private void prepareNext() {
        final int index = pos + 1;
        if (index >= playlist.size()) return;
        final String uri = playlist.get(index);
        new Thread(() -> {
            MediaPlayer prepared = null;
            try {
                File file = local(uri);
                prepared = new MediaPlayer();
                prepared.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build());
                prepared.setDataSource(file.getAbsolutePath());
                prepared.prepare();
                applyGain(prepared);
                // Deliberately NOT applySpeed here. setPlaybackParams with a
                // non-zero speed on a player that is not Started *starts it* —
                // documented, and the reason the first real reply logged
                // "gapless handoff refused": the next clip had begun playing
                // under the current one, so setNextMediaPlayer refused a
                // player that was no longer merely prepared. Speed is applied
                // on adoption instead, costing a few milliseconds at 1.0.
            } catch (Exception e) {
                log.line("builtin-speech: preparing " + uri + " failed: " + e);
                if (prepared != null) {
                    try { prepared.release(); } catch (Throwable ignored) { }
                }
                return;
            }
            final MediaPlayer ready = prepared;
            worker.execute(() -> {
                // The playlist may have moved on while this was preparing --
                // a new reply replaces everything -- in which case this clip
                // is already history and must not be attached to anything.
                if (index != pos + 1 || player == null) {
                    try { ready.release(); } catch (Throwable ignored) { }
                    return;
                }
                ready.setOnCompletionListener(m -> worker.execute(this::onClipEnded));
                next = ready;
                nextIndex = index;
                try {
                    player.setNextMediaPlayer(ready);
                } catch (Throwable t) {
                    // Then it plays with a gap, which is worse and not broken.
                    log.line("builtin-speech: gapless handoff refused: " + t);
                }
            });
        }, "speech-prepare-next").start();
    }

    /**
     * A clip ended. If Android has already started the one we handed it, adopt
     * it rather than building a player for a clip that is already playing.
     */
    private void onClipEnded() {
        MediaPlayer started = next;
        if (started != null && nextIndex == pos + 1) {
            MediaPlayer old = player;
            player = started;
            next = null;
            pos = nextIndex;
            nextIndex = -1;
            if (old != null) {
                try { old.release(); } catch (Throwable ignored) { }
            }
            try {
                applySpeed(player);
            } catch (Throwable ignored) {
                // Started by the framework at 1.0; not worth dropping a clip.
            }
            volunteer("playlist-pos");
            prepareNext();
            return;
        }
        advance(true);
    }

    /** Fetch in the background, once, and never complain: this is a warmup. */
    private void warm(String uri) {
        if (!uri.startsWith("http")) return;
        if (!fetching.add(uri)) return;
        fetcher.execute(() -> {
            try {
                fetch(uri);
            } catch (Exception e) {
                // Its turn will come, and the failure will be reported there,
                // where there is a sentence waiting on it.
            } finally {
                fetching.remove(uri);
            }
        });
    }

    private File fetch(String url) throws Exception {
        File dir = new File(context.getCacheDir(), "speech");
        dir.mkdirs();
        File out = new File(dir, Integer.toHexString(url.hashCode()) + ".clip");
        if (out.length() > 0) return out;
        File part = new File(out.getPath() + ".part");
        HttpURLConnection c = (HttpURLConnection) URI.create(url).toURL()
                .openConnection();
        c.setConnectTimeout(5000);
        c.setReadTimeout(20000);
        try (InputStream in = c.getInputStream();
             OutputStream os = new FileOutputStream(part)) {
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
        } finally {
            c.disconnect();
        }
        // Rename last: a half-written file that is never renamed is retried,
        // where a half-written file at the real name would be played.
        if (!part.renameTo(out)) return part;
        return out;
    }

    // ---- focus: deliberately none, see above -------------------------------

    /**
     * Focus is NOT requested by this player, and that is the whole lesson of
     * the first evening it was used in anger.
     *
     * The reasoning that put a request here was sound in isolation: the sound
     * is ours, so the request would be honest — unlike the silent AudioTrack,
     * which exists to make Android believe an app is playing when the noise is
     * really mpv's. What it missed is that this app <em>already has</em> a
     * focus owner: {@link FocusControl}, driven by the channel state this
     * player now feeds. Requesting focus from inside the same app made it
     * compete with itself:
     *
     * <pre>
     *   focus: granted (speech) / silent track started
     *   focus: abandoned (nothing open) / silent track stopped
     *   focus: granted (speech)      ... about five times a second
     * </pre>
     *
     * Each grant re-evaluated the state, each abandon reached this class as a
     * change, and playback paused and resumed between them. David heard it as
     * "very jittery", which is precisely what it was.
     *
     * One focus owner per app. What this player owes it is accurate state,
     * which {@link #mirrorInto} provides — and the honesty argument survives:
     * the focus FocusControl holds while this player plays is held for a stream
     * the app really is producing.
     */
    private void release() {
        MediaPlayer n = next;
        next = null;
        nextIndex = -1;
        if (n != null) {
            try { n.release(); } catch (Throwable ignored) { }
        }
        MediaPlayer mp = player;
        player = null;
        if (mp == null) return;
        try { mp.stop(); } catch (Throwable ignored) { }
        try { mp.release(); } catch (Throwable ignored) { }
    }
}
