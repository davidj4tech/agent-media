package net.agentmedia.companion;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The client/server configuration: what it defaults to, what it refuses, and
 * what it means for the audio on this phone.
 *
 * The defaults matter more than anything else here. An install that never opens
 * the settings screen must behave exactly as the app did before there was one —
 * Termux on this phone, the same four ports, no token — because that is every
 * install that exists today, and a configuration layer that quietly changed
 * their behaviour would be the worst possible way to find out it was added.
 */
public class ServerTest {

    private static int passed = 0;
    private static final java.util.List<String> failures =
            new java.util.ArrayList<String>();

    public static void main(String[] args) {
        testDefaultsAreTodaysPhone();
        testRoundTripsThroughStorage();
        testStorageGapsFallBackFieldByField();
        testARemoteServerNeedsAToken();
        testBadPortsAreRefused();
        testAUrlIsNotAHostName();
        testOnlyThePhoneOwnsThePhonesAudio();
        testTheBridgesLiveWhereTheSoundIs();
        testBuiltinIsNamedButNotOffered();
        testAnUnusableConfigurationReadsBackAsTheDefaults();
        testDescribeNeverLeaksTheToken();

        testThePlayerSocketOnlyGoesOnTheTailnet();

        testTheCanvasDefaultsToTheServersOwnHost();
        testTheCanvasCanLiveSomewhereElse();
        testACanvasOnLoopbackIsRefusedWithAReason();
        testABadCanvasDoesNotCostTheRestOfTheConfiguration();
        testTheCanvasRoundTripsThroughStorage();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    /**
     * Where the in-app speech player is allowed to listen.
     *
     * The mistake this guards against is binding 0.0.0.0 for convenience: mpv's
     * IPC has no authentication, never had any, and the socat bridges have
     * always bound one address on purpose. A control socket that follows the
     * phone onto café Wi-Fi is a different security posture arrived at by
     * accident.
     */
    // ---- the canvas -------------------------------------------------------

    /**
     * Empty canvas address means "wherever the server is".
     *
     * The setting exists because the two can differ, not because they usually
     * do: point the app at red5 for everything and the canvas must follow
     * without a second address to keep in step. A default that had to be typed
     * would be a second thing to get wrong for no gain.
     */
    private static void testTheCanvasDefaultsToTheServersOwnHost() {
        Server s = new Server("red5", 8771, 6601, 6602, 6603, "sekrit",
                              Server.SERVER);
        check("canvas port defaults to 8781", s.canvas == Server.CANVAS_PORT);
        check("canvas host follows the server", "red5".equals(s.canvasAddress()));
        check("and the URL is built from it",
              "http://red5:8781/".equals(s.canvasUrl()));
        check("a configured canvas has no problem", s.canvasProblem() == null);
    }

    /**
     * The arrangement this fleet actually runs: media-share in Termux on this
     * phone, the canvas on the machine producing the speech. Neither can be
     * derived from the other.
     */
    private static void testTheCanvasCanLiveSomewhereElse() {
        Server s = new Server(Server.LOOPBACK, 8771, 6601, 6602, 6603, "",
                              Server.PHONE, "red5", 8781);
        check("the server is still this phone", s.local());
        check("the canvas is not", "red5".equals(s.canvasAddress()));
        check("no problem with the split", s.canvasProblem() == null);
        // Pairing is the one path that needs more than "/": the token lands in
        // localStorage and the page redirects to itself.
        check("a path can be asked for",
              "http://red5:8781/pair?c=abc".equals(s.canvasUrl("/pair?c=abc")));
        check("with or without the leading slash",
              "http://red5:8781/pair?c=abc".equals(s.canvasUrl("pair?c=abc")));
    }

    /**
     * The failure phase 0 papered over with a hardcoded hostname.
     *
     * An unconfigured install points at loopback, where no canvas listens —
     * and a canvas that never connected is indistinguishable from a canvas
     * with nothing on it. Both are black. So say which one it is.
     */
    private static void testACanvasOnLoopbackIsRefusedWithAReason() {
        Server s = Server.defaults();
        String why = s.canvasProblem();
        check("loopback canvas is refused", why != null);
        check("and the reason names the fix",
              why != null && why.contains("Settings"));

        Server url = new Server("red5", 8771, 6601, 6602, 6603, "x",
                                Server.SERVER, "http://red5:8781", 8781);
        check("a URL in the address field is caught",
              url.canvasProblem() != null);

        Server badPort = new Server("red5", 8771, 6601, 6602, 6603, "x",
                                    Server.SERVER, "", 0);
        check("a bad canvas port is caught", badPort.canvasProblem() != null);
    }

    /**
     * A mistyped canvas port must not take the music with it.
     *
     * `orDefaults` throws the whole configuration away when `problem` bites,
     * which is right for a token that would otherwise be sent to a remote
     * host — and would be badly wrong for a canvas, whose only consequence is
     * one screen. They are kept as two questions on purpose.
     */
    private static void testABadCanvasDoesNotCostTheRestOfTheConfiguration() {
        Server s = new Server("red5", 8771, 6601, 6602, 6603, "sekrit",
                              Server.SERVER, "", 0);
        check("the canvas is broken", s.canvasProblem() != null);
        check("the configuration is not", s.problem() == null);
        check("so it is kept", s.equals(s.orDefaults()));
    }

    private static void testTheCanvasRoundTripsThroughStorage() {
        Server s = new Server("red5", 8771, 6601, 6602, 6603, "sekrit",
                              Server.SERVER, "pn", 9781);
        Server back = Server.from(s.toMap());
        check("canvas host survives storage", "pn".equals(back.canvasHost));
        check("canvas port survives storage", back.canvas == 9781);
        check("and the whole thing is equal", s.equals(back));

        Map<String, String> old = s.toMap();
        old.remove(Server.KEY_CANVAS_HOST);
        old.remove(Server.KEY_CANVAS_PORT);
        Server upgraded = Server.from(old);
        check("prefs written before the canvas existed still read",
              upgraded.canvasHost.isEmpty()
                      && upgraded.canvas == Server.CANVAS_PORT);
    }

    private static void testThePlayerSocketOnlyGoesOnTheTailnet() {
        check("a tailscale address is recognised", Server.isTailnet("100.94.14.59"));
        check("the bottom of the range is in", Server.isTailnet("100.64.0.1"));
        check("the top of the range is in", Server.isTailnet("100.127.255.254"));
        check("100.128.x is public space, not tailnet",
                !Server.isTailnet("100.128.0.1"));
        check("and nor is 100.63.x", !Server.isTailnet("100.63.0.1"));
        check("a LAN address is never offered the socket",
                !Server.isTailnet("192.168.1.10"));
        check("nor is loopback", !Server.isTailnet("127.0.0.1"));
        check("nor an IPv6 link-local", !Server.isTailnet("fe80::1%wlan0"));
        check("nor nonsense", !Server.isTailnet("100.64") && !Server.isTailnet(null));
        String chosen = Server.tailnetAddress();
        check("what it picks here is the tailnet or loopback, never a LAN",
                Server.LOOPBACK.equals(chosen) || Server.isTailnet(chosen));
        check("the builtin speech port is not one of mpv's",
                Server.BUILTIN_SPEECH_PORT != Server.SPEECH_PORT
                        && Server.BUILTIN_SPEECH_PORT != Server.MUSIC_PORT
                        && Server.BUILTIN_SPEECH_PORT != Server.BOOK_PORT);
    }

    private static void testDefaultsAreTodaysPhone() {
        Server d = Server.defaults();
        check("default host is loopback", "127.0.0.1".equals(d.host));
        check("default control port is media-share", d.control == 8771);
        check("default music port is the music bridge", d.music == 6601);
        check("default speech port is the speech bridge", d.speech == 6602);
        check("default book port is the book bridge", d.book == 6603);
        check("no token by default", d.token.isEmpty());
        check("sound is on this phone by default", d.ownsThePhonesAudio());
        check("the defaults are usable", d.problem() == null);
        check("the defaults are local", d.local() && !d.remote());
    }

    private static void testRoundTripsThroughStorage() {
        Server s = new Server("red5", 8771, 6601, 6602, 6603, "abc", Server.SERVER);
        Server back = Server.from(s.toMap());
        check("a configuration survives storage", s.equals(back));
        check("and so does the playback location",
                Server.SERVER.equals(back.playback));
    }

    private static void testStorageGapsFallBackFieldByField() {
        // A half-written prefs file should cost the field it broke, not the app.
        Map<String, String> m = new LinkedHashMap<String, String>();
        m.put(Server.KEY_HOST, "red5");
        Server s = Server.from(m);
        check("the stored field is kept", "red5".equals(s.host));
        check("a missing port falls back", s.control == 8771);
        check("a missing playback location falls back",
                Server.PHONE.equals(s.playback));
        check("an unknown playback location falls back",
                Server.PHONE.equals(Server.from(one(Server.KEY_PLAYBACK, "wat"))
                        .playback));
        check("null is the defaults", Server.from(null).equals(Server.defaults()));
    }

    private static void testARemoteServerNeedsAToken() {
        // The rule the whole thing turns on: the control endpoint starts
        // playback, so off loopback the secret is required rather than offered.
        Server bare = new Server("red5", 8771, 6601, 6602, 6603, "", Server.SERVER);
        check("a remote server with no token is refused", bare.problem() != null);
        check("and says why", bare.problem().contains("token"));

        Server withToken = new Server("red5", 8771, 6601, 6602, 6603, "s3cret",
                                      Server.SERVER);
        check("a remote server with a token is fine", withToken.problem() == null);

        Server loopbackBare = Server.defaults();
        check("loopback needs no token", loopbackBare.problem() == null);
        check("localhost counts as loopback",
                new Server("localhost", 8771, 6601, 6602, 6603, "", Server.PHONE)
                        .local());
    }

    private static void testBadPortsAreRefused() {
        check("port 0 is refused",
                new Server("red5", 0, 6601, 6602, 6603, "t", Server.SERVER)
                        .problem() != null);
        check("a port past 65535 is refused",
                new Server("red5", 70000, 6601, 6602, 6603, "t", Server.SERVER)
                        .problem() != null);
        check("an unparsable port is 0, not an exception",
                Server.port("eight thousand", 8771) == 0);
        check("empty typed text keeps the fallback", Server.port("", 8771) == 8771);
        check("a bridge port is checked too",
                new Server("red5", 8771, 6601, -1, 6603, "t", Server.SERVER)
                        .problem().contains("Speech"));
    }

    private static void testAUrlIsNotAHostName() {
        // The mistake a person makes once: pasting the address with a scheme.
        Server s = new Server("http://red5:8771", 8771, 6601, 6602, 6603, "t",
                              Server.SERVER);
        check("a URL is refused", s.problem() != null);
        check("and is named as such", s.problem().contains("host name"));
        check("an empty address is refused",
                new Server("", 8771, 6601, 6602, 6603, "", Server.PHONE)
                        .problem() != null);
    }

    private static void testOnlyThePhoneOwnsThePhonesAudio() {
        // Everything about audio focus and the silent track hangs off this one
        // predicate: it is true only when the mpv making the noise is here.
        check("phone playback owns the audio",
                Server.defaults().ownsThePhonesAudio());
        check("server playback does not",
                !new Server("red5", 8771, 6601, 6602, 6603, "t", Server.SERVER)
                        .ownsThePhonesAudio());
        check("a loopback server can still be a remote control",
                !new Server("127.0.0.1", 8771, 6601, 6602, 6603, "", Server.SERVER)
                        .ownsThePhonesAudio());
    }

    private static void testTheBridgesLiveWhereTheSoundIs() {
        // The arrangement this fleet actually runs: `media` originates on red5,
        // the sound comes out of the phone's own mpv. Control is then remote
        // and the mpv sockets are local — an app that sent both to the same
        // address would ask red5 for the state of a player on this phone.
        Server split = new Server("red5", 8771, 6601, 6602, 6603, "t", Server.PHONE);
        check("control goes to the server", "red5".equals(split.host));
        check("the bridges stay here", "127.0.0.1".equals(split.mpvHost()));

        Server remote = new Server("red5", 8771, 6601, 6602, 6603, "t", Server.SERVER);
        check("sound at the server takes the bridges with it",
                "red5".equals(remote.mpvHost()));
        check("and the default keeps both local",
                "127.0.0.1".equals(Server.defaults().mpvHost()));
    }

    private static void testBuiltinIsNamedButNotOffered() {
        check("phone playback is available", Server.available(Server.PHONE));
        check("server playback is available", Server.available(Server.SERVER));
        check("in-app playback is not", !Server.available(Server.BUILTIN));
        Server s = new Server("127.0.0.1", 8771, 6601, 6602, 6603, "",
                              Server.BUILTIN);
        check("and a stored one is refused", s.problem() != null);
    }

    private static void testAnUnusableConfigurationReadsBackAsTheDefaults() {
        // A remote server whose token has been cleared: failing locally and
        // visibly beats sending the secret-less request to the address anyway.
        Server s = new Server("red5", 8771, 6601, 6602, 6603, "", Server.SERVER);
        check("an unusable configuration falls back",
                s.orDefaults().equals(Server.defaults()));
        Server good = new Server("red5", 8771, 6601, 6602, 6603, "t", Server.SERVER);
        check("a usable one is kept", good.orDefaults().equals(good));
    }

    private static void testDescribeNeverLeaksTheToken() {
        // describe() goes into the event log and /state, both of which are read
        // out loud and pasted into transcripts.
        Server s = new Server("red5", 8771, 6601, 6602, 6603, "s3cret",
                              Server.SERVER);
        check("describe says there is a token", s.describe().contains("token"));
        check("describe does not say what it is", !s.describe().contains("s3cret"));
        check("nor does toString", !s.toString().contains("s3cret"));
        check("describe names the server", s.describe().startsWith("red5:8771"));
    }

    private static Map<String, String> one(String key, String value) {
        Map<String, String> m = new LinkedHashMap<String, String>();
        m.put(key, value);
        return m;
    }

    private static void check(String what, boolean ok) {
        if (ok) {
            passed++;
            System.out.print(".");
        } else {
            failures.add(what);
            System.out.print("F");
        }
    }
}
