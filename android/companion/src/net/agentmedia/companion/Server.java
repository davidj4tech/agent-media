package net.agentmedia.companion;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Which agent-media this app is a client of, and where its sound comes out.
 *
 * <h4>Why there is a configuration at all</h4>
 *
 * Every address in this app used to be a constant, and every one of them said
 * 127.0.0.1: the app assumed `media`, mpv and yt-dlp were in com.termux on the
 * same phone, because on p8a they are. That assumption is the only thing
 * standing between this and an APK somebody else can install — and it is also
 * what puts a GPL mpv and a Python yt-dlp on the device, which is a licence
 * question the moment the app is distributed. Both go away if the server is
 * allowed to be somewhere else.
 *
 * <h4>Two settings, not one</h4>
 *
 * They are independent, and conflating them is the mistake this class exists to
 * prevent:
 *
 * <ul>
 *   <li>{@link #host} — <em>where the server is</em>. The machine running
 *       `media`: this phone's Termux, or red5 across the tailnet.</li>
 *   <li>{@link #playback} — <em>where the sound comes out</em>. Pointing the
 *       app at red5 does not move the audio to the phone; it moves it to
 *       red5's speakers and makes this app a remote control. That is a real
 *       mode and it is the one that needs no Termux at all, but it is not
 *       "music on my phone".</li>
 * </ul>
 *
 * {@link #BUILTIN} is the third playback location and the one that finally
 * closes the loop — the server fetches and renders, the app plays the bytes
 * with Android's own player, and nothing GPL ships in the APK. It is named
 * here and not implemented; see {@link #available}.
 *
 * <h4>What each mode needs on the far side</h4>
 *
 * The control endpoint ({@link #control}, media-share) is the whole API for the
 * home screen, the recent list and the share sheet — it is `media` over HTTP
 * and it works against any host that runs it. The three mpv ports are a
 * <em>second</em>, lower path: raw JSON IPC, one socket per channel, and what
 * the shade's media cards are built from. A server without those bridges costs
 * exactly the cards, which is the same bargain a missing bridge has always
 * been.
 *
 * <h4>The token</h4>
 *
 * On loopback there is none, and that is deliberate — the boundary is the
 * phone's own UID sandbox. Off loopback the control endpoint can start
 * playback from anything that can route to it, so a shared secret is required
 * rather than offered: see {@link #problem}. Tailscale ACLs are a perimeter,
 * not a credential, and the app is meant to outlive this tailnet.
 *
 * {@code android.*}-free, so {@code test/run.sh} covers it.
 */
final class Server {

    /** This phone. The default, and the only host that needs no token. */
    static final String LOOPBACK = "127.0.0.1";

    /** media-share: /channels, /control, /recent, /play, /chapters, /share. */
    static final int CONTROL_PORT = 8771;

    /** The three mpv IPC bridges, in channel order. */
    static final int MUSIC_PORT = 6601;
    static final int SPEECH_PORT = 6602;
    static final int BOOK_PORT = 6603;

    /** Sound comes out of the mpv in Termux on this phone. Today's behaviour. */
    static final String PHONE = "phone";
    /** Sound comes out at the server; this app is a remote control. */
    static final String SERVER = "server";
    /**
     * Sound comes out of a player inside this app, fed by the server over HTTP.
     * Reserved: the settings screen shows it and will not let it be chosen.
     */
    static final String BUILTIN = "builtin";

    /** How the shared secret travels. Read by media-share; see its --bind. */
    static final String TOKEN_HEADER = "X-Agent-Media-Token";

    final String host;
    final int control;
    final int music;
    final int speech;
    final int book;
    final String token;
    final String playback;

    Server(String host, int control, int music, int speech, int book,
           String token, String playback) {
        this.host = trim(host);
        this.control = control;
        this.music = music;
        this.speech = speech;
        this.book = book;
        this.token = trim(token);
        this.playback = normalise(playback);
    }

    /** Termux on this phone, which is what every install starts as. */
    static Server defaults() {
        return new Server(LOOPBACK, CONTROL_PORT, MUSIC_PORT, SPEECH_PORT,
                          BOOK_PORT, "", PHONE);
    }

    /** A loopback server on one control port. For tests and fake listeners. */
    static Server loopback(int controlPort) {
        return new Server(LOOPBACK, controlPort, MUSIC_PORT, SPEECH_PORT,
                          BOOK_PORT, "", PHONE);
    }

    // ---- what the rest of the app asks it ---------------------------------

    /** Is the server this same phone? Then the sandbox is the boundary. */
    boolean local() {
        return LOOPBACK.equals(host) || "localhost".equals(host)
                || "::1".equals(host);
    }

    /** Anything else, and the token rule applies. */
    boolean remote() {
        return !local();
    }

    /**
     * Should the app drive an mpv it is co-resident with — and therefore hold
     * audio focus, open the silent track, and act on a focus loss?
     *
     * All of that machinery exists for one reason: the mpv making the noise is
     * on this phone and ignores audio focus, so somebody has to hold it on its
     * behalf. When the noise is coming out of red5 there is nothing on this
     * phone to duck, and claiming focus would stop the user's own music to
     * control a stereo in another room.
     */
    boolean ownsThePhonesAudio() {
        return PHONE.equals(playback);
    }

    /**
     * Where the three mpv IPC bridges are — which is not always {@link #host}.
     *
     * <b>The bridges live where the sound is, not where the server is.</b> The
     * combination that proves it is the one this fleet actually runs: `media`
     * originates on red5 and the audio comes out of the phone's own mpv. The
     * control endpoint is then remote and the mpv sockets are local, and an app
     * that sent both to the same address would ask red5 for the state of a
     * player on this phone.
     *
     * So it is derived rather than configured — a fourth address on the screen
     * would be a fourth thing to get wrong, and there is no arrangement where
     * the cards describe a player somewhere other than the one making the
     * noise. In-app playback has no mpv at all; loopback is the harmless
     * answer, and nothing will be connecting.
     */
    String mpvHost() {
        return SERVER.equals(playback) ? host : LOOPBACK;
    }

    /** {@code host:control} — how the control endpoint is named in a log. */
    String authority() {
        return host + ":" + control;
    }

    /** One line for the log and the readout. Never the token. */
    String describe() {
        return authority() + " (" + playback + " playback"
                + (token.isEmpty() ? "" : ", token") + ")";
    }

    /**
     * Why this configuration cannot be used, or null.
     *
     * Written for a settings screen to show verbatim, so each one names the
     * field and what to do about it.
     */
    String problem() {
        if (host.isEmpty()) return "Server address is empty.";
        if (host.indexOf('/') >= 0 || host.indexOf(' ') >= 0) {
            return "Server address is a host name, not a URL: " + host;
        }
        String p = portProblem("Control port", control);
        if (p == null) p = portProblem("Music port", music);
        if (p == null) p = portProblem("Speech port", speech);
        if (p == null) p = portProblem("Book port", book);
        if (p != null) return p;
        if (remote() && token.isEmpty()) {
            return "A server that is not this phone needs a token: anything "
                    + "that can reach it can start playback.";
        }
        if (BUILTIN.equals(playback)) {
            return "Playing in the app is not built yet.";
        }
        return null;
    }

    private static String portProblem(String label, int port) {
        return port > 0 && port < 65536 ? null
                : label + " must be between 1 and 65535.";
    }

    /** Is this playback location one the app can actually deliver today? */
    static boolean available(String playback) {
        return PHONE.equals(playback) || SERVER.equals(playback);
    }

    /** What to call a playback location on screen. */
    static String label(String playback) {
        if (SERVER.equals(playback)) return "The server";
        if (BUILTIN.equals(playback)) return "This phone (in the app)";
        return "This phone (Termux)";
    }

    // ---- storage ----------------------------------------------------------
    //
    // A Map rather than SharedPreferences directly, so this class stays
    // android.*-free and the round trip is testable. Settings does the
    // platform half.

    static final String KEY_HOST = "server_host";
    static final String KEY_CONTROL = "server_control_port";
    static final String KEY_MUSIC = "server_music_port";
    static final String KEY_SPEECH = "server_speech_port";
    static final String KEY_BOOK = "server_book_port";
    static final String KEY_TOKEN = "server_token";
    static final String KEY_PLAYBACK = "server_playback";

    Map<String, String> toMap() {
        Map<String, String> m = new LinkedHashMap<String, String>();
        m.put(KEY_HOST, host);
        m.put(KEY_CONTROL, String.valueOf(control));
        m.put(KEY_MUSIC, String.valueOf(music));
        m.put(KEY_SPEECH, String.valueOf(speech));
        m.put(KEY_BOOK, String.valueOf(book));
        m.put(KEY_TOKEN, token);
        m.put(KEY_PLAYBACK, playback);
        return m;
    }

    /**
     * Read a stored configuration, falling back field by field.
     *
     * Never throws and never returns null: a half-written or hand-edited prefs
     * file should cost the field it broke, not the app. A whole configuration
     * that cannot be used is a separate question — see {@link #problem}, and
     * {@link #orDefaults}.
     */
    static Server from(Map<String, String> m) {
        if (m == null) return defaults();
        return new Server(
                text(m, KEY_HOST, LOOPBACK),
                port(m.get(KEY_CONTROL), CONTROL_PORT),
                port(m.get(KEY_MUSIC), MUSIC_PORT),
                port(m.get(KEY_SPEECH), SPEECH_PORT),
                port(m.get(KEY_BOOK), BOOK_PORT),
                text(m, KEY_TOKEN, ""),
                text(m, KEY_PLAYBACK, PHONE));
    }

    /**
     * This configuration if it can be used, otherwise the defaults.
     *
     * The one that matters is a saved remote server whose token has been
     * cleared: falling back to loopback fails visibly and locally, where
     * carrying on would send the secret-less request to the address anyway.
     */
    Server orDefaults() {
        return problem() == null ? this : defaults();
    }

    /** Parse a port from stored or typed text; anything unusable is 0. */
    static int port(String raw, int fallback) {
        String s = trim(raw);
        if (s.isEmpty()) return fallback;
        try {
            return Integer.parseInt(s);
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private static String text(Map<String, String> m, String key, String fallback) {
        String v = trim(m.get(key));
        return v.isEmpty() ? fallback : v;
    }

    private static String normalise(String playback) {
        String p = trim(playback);
        if (SERVER.equals(p) || BUILTIN.equals(p)) return p;
        return PHONE;
    }

    private static String trim(String s) {
        return s == null ? "" : s.trim();
    }

    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Server)) return false;
        Server s = (Server) o;
        return host.equals(s.host) && control == s.control && music == s.music
                && speech == s.speech && book == s.book
                && token.equals(s.token) && playback.equals(s.playback);
    }

    @Override
    public int hashCode() {
        return toMap().hashCode();
    }

    @Override
    public String toString() {
        return "Server[" + describe() + "]";
    }
}
