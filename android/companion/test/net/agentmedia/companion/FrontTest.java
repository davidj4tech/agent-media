package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for which channel the one MediaSession describes.
 *
 * The rule under test is narrow on purpose: the metadata follows the front
 * channel, and nothing else does. Most of what is checked here is therefore
 * what must *not* move — a spoken clip must not change what the transport
 * describes, because the framework resolves a PLAY_PAUSE toggle from it and
 * answering that question about a two-second clip is the bug 3519172 fixed.
 */
public final class FrontTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) {
        testMusicAloneIsInFront();
        testSpeechTakesTheFront();
        testSpeechOverPausedMusic();
        testSpeechNamesTheMusicUnderneath();
        testFinishedClipHandsTheFrontBack();
        testPausedSpeechIsNotInFront();
        testAHeldClipKeepsTheFront();
        testUnreachableSpeechFallsBackToMusic();
        testSpeechHidesTheDuration();
        testIdleEverywhere();
        testAStagedClipOwnsTheLossBeforeItIsAudible();
        testTheGraceExpires();
        testAnUnreachableSpeechMpvOwnsNothing();
        testTheCoordinatorsFlagBeatsEveryHeuristic();
        testAStuckFlagStopsBeingBelieved();

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ok — " + passed + " checks passed");
            return;
        }
        System.out.println(failures.size() + " FAILED of " + (passed + failures.size()));
        for (String f : failures) System.out.println("  " + f);
        System.exit(1);
    }

    private static void testMusicAloneIsInFront() {
        MpvState music = playing("Rite of Spring", 400.0);
        MpvState speech = idle();
        is("music", FrontChannel.name(speech), "nothing speaking: music is in front");
        is("Rite of Spring", FrontChannel.title(music, speech), "the track's own title");
        is(FrontChannel.DEFAULT_SUBTITLE, FrontChannel.subtitle(music, speech),
           "the artist line is unchanged from before the speech channel existed");
        is(Long.valueOf(400000L), Long.valueOf(FrontChannel.durationMs(music, speech)),
           "and the track's duration");
    }

    private static void testSpeechTakesTheFront() {
        MpvState music = playing("Rite of Spring", 400.0);
        MpvState speech = playing("remote-20260814T190922-18480.mp3", 9.0);
        yes(FrontChannel.speechInFront(speech), "a running clip is in front");
        is(FrontChannel.SPEECH_TITLE, FrontChannel.title(music, speech),
           "and is named Sam, not by the clip file mpv is playing");
    }

    private static void testSpeechOverPausedMusic() {
        MpvState music = playing("Rite of Spring", 400.0);
        music.paused = true;
        MpvState speech = playing("clip.mp3", 4.0);
        is(FrontChannel.SPEECH_TITLE, FrontChannel.title(music, speech),
           "speech is in front whether or not music is running under it");
    }

    private static void testSpeechNamesTheMusicUnderneath() {
        MpvState music = playing("Rite of Spring", 400.0);
        MpvState speech = playing("clip.mp3", 4.0);
        is("Rite of Spring", FrontChannel.subtitle(music, speech),
           "the second line names whose progress bar is on screen");

        is(FrontChannel.DEFAULT_SUBTITLE, FrontChannel.subtitle(idle(), speech),
           "with nothing playing underneath there is nothing to name");
    }

    private static void testFinishedClipHandsTheFrontBack() {
        MpvState music = playing("Rite of Spring", 400.0);
        MpvState speech = playing("clip.mp3", 4.0);
        speech.idleActive = true;              // end-file: sink-speech goes idle
        no(FrontChannel.speechInFront(speech), "a finished clip is not in front");
        is("Rite of Spring", FrontChannel.title(music, speech), "the track comes back");
    }

    private static void testPausedSpeechIsNotInFront() {
        // The popup's Space pauses the speech broker. A held clip is not
        // something to name on the car display.
        MpvState speech = playing("clip.mp3", 4.0);
        speech.paused = true;
        no(FrontChannel.speechInFront(speech), "a paused clip is not in front");
    }

    /**
     * The card that paused Sam must still be able to start him again. On p8a on
     * 2026-08-15 it could not: `transport: pause -> speech` at 08:54:17, then
     * `transport: play -> music` at 08:54:20, because the front channel dropped
     * with the pause and took the play button to an idle music mpv with it.
     */
    private static void testAHeldClipKeepsTheFront() {
        MpvState music = playing("Rite of Spring", 900.0);
        MpvState speech = playing("clip.mp3", 4.0);
        speech.paused = true;

        no(FrontChannel.speechInFront(speech, false),
           "a clip nobody is holding is still not in front");
        yes(FrontChannel.speechInFront(speech, true),
            "one that was paused on purpose is");
        is(FrontChannel.SPEECH_TITLE, FrontChannel.title(music, speech, true),
           "so the card goes on naming Sam while he is paused");
        is("speech", FrontChannel.name(speech, true), "and the readout says so");

        // The hold cannot outlive the clip: sink-speech going idle leaves
        // nothing to resume, and holding the front there would strand the card
        // on a channel with nothing in it.
        speech.idleActive = true;
        no(FrontChannel.speechInFront(speech, true),
           "a hold on a clip that is gone is not a front");
    }

    private static void testUnreachableSpeechFallsBackToMusic() {
        // The bridge is down, or sink-speech is not running. We cannot tell
        // whether a clip is playing, and music is the safe thing to describe.
        MpvState speech = playing("clip.mp3", 4.0);
        speech.connected = false;
        no(FrontChannel.speechInFront(speech), "an unreachable speech mpv is never in front");
    }

    private static void testSpeechHidesTheDuration() {
        MpvState music = playing("Rite of Spring", 400.0);
        MpvState speech = playing("clip.mp3", 9.0);
        is(Long.valueOf(-1L), Long.valueOf(FrontChannel.durationMs(music, speech)),
           "no duration while speech is in front: the position we publish is the music's, "
           + "and pairing the two would draw a bar wrong in both directions");
    }

    private static void testIdleEverywhere() {
        MpvState music = idle();
        MpvState speech = idle();
        is("agent-media", FrontChannel.title(music, speech), "MpvState's own fallback title");
        is(Long.valueOf(-1L), Long.valueOf(FrontChannel.durationMs(music, speech)),
           "and no duration");
    }

    /**
     * The p8a trace of 2026-08-14: the focus loss for a spoken reply arrived at
     * 20:16:29 and the clip's first audio at 20:16:40. Asking "is speech playing"
     * at loss time answers no for a loss that is entirely ours — which is how a
     * rule meant to stop the app ducking over the coordinator would have ducked
     * anyway.
     */
    private static void testAStagedClipOwnsTheLossBeforeItIsAudible() {
        MpvState speech = playing("clip.mp3", 9.0);
        speech.paused = true;                       // staged, not yet started
        no(FrontChannel.speechInFront(speech), "not audible yet");
        yes(FrontChannel.ourSpeech(speech, 11000),
            "but the loss eleven seconds after staging is still ours");
        yes(FrontChannel.ourSpeech(playing("clip.mp3", 9.0), Long.MAX_VALUE),
            "and an audible clip needs no staging time at all");
    }

    private static void testTheGraceExpires() {
        MpvState speech = playing("clip.mp3", 9.0);
        speech.paused = true;
        no(FrontChannel.ourSpeech(speech, FrontChannel.STAGING_GRACE_MS + 1),
           "past the grace, a loss belongs to whatever else is playing");
        no(FrontChannel.ourSpeech(idle(), Long.MAX_VALUE),
           "and a speech mpv that has never staged anything owns nothing");

        // The parked case this bound exists for: sink-speech keeps the last clip
        // open indefinitely, so "a file is loaded" alone would suppress every
        // duck for the rest of the day.
        MpvState parked = playing("clip.mp3", 9.0);
        parked.paused = true;
        no(FrontChannel.ourSpeech(parked, 3600000L), "a parked broker owns nothing");
    }

    private static void testAnUnreachableSpeechMpvOwnsNothing() {
        MpvState speech = playing("clip.mp3", 9.0);
        speech.connected = false;
        no(FrontChannel.ourSpeech(speech, 1000),
           "a loss we cannot attribute ducks, because ducking is the point");
    }

    /**
     * The case no window can catch: the coordinator renders and relays a
     * response before any of it is audible, and mpv takes the output when it
     * opens the clip — 37 s before staging on p8a, 2026-08-14. The flag is set
     * at the top of that work, so it is true when nothing else is.
     */
    private static void testTheCoordinatorsFlagBeatsEveryHeuristic() {
        MpvState speech = idle();              // nothing open, nothing staged
        speech.speaking = true;
        yes(FrontChannel.ourSpeech(speech, Long.MAX_VALUE, 1000),
            "the flag answers where playback cannot");

        // And it is not a licence to suppress forever: cleared, the fallbacks
        // decide again.
        speech.speaking = false;
        no(FrontChannel.ourSpeech(speech, Long.MAX_VALUE, 1000),
           "a lowered flag ducks again immediately");
    }

    private static void testAStuckFlagStopsBeingBelieved() {
        // after_speech lowers it, so a process killed mid-response leaves it
        // raised — and a raised flag means never ducking for anything.
        MpvState speech = idle();
        speech.speaking = true;
        no(FrontChannel.ourSpeech(speech, Long.MAX_VALUE,
                                  FrontChannel.SPEAKING_FLAG_MAX_MS + 1),
           "past its lifetime the flag is a leftover, not a fact");
    }

    // ---- fixtures --------------------------------------------------------

    private static MpvState idle() {
        MpvState s = new MpvState();
        s.connected = true;
        s.idleActive = true;
        return s;
    }

    private static MpvState playing(String title, double duration) {
        MpvState s = new MpvState();
        s.connected = true;
        s.idleActive = false;
        s.paused = false;
        s.mediaTitle = title;
        s.duration = duration;
        return s;
    }

    // ---- assertions ------------------------------------------------------

    private static void is(Object want, Object got, String what) {
        if (want == null ? got == null : want.equals(got)) {
            passed++;
        } else {
            failures.add(what + ": wanted " + want + ", got " + got);
        }
    }

    private static void yes(boolean got, String what) {
        is(Boolean.TRUE, Boolean.valueOf(got), what);
    }

    private static void no(boolean got, String what) {
        is(Boolean.FALSE, Boolean.valueOf(got), what);
    }
}
