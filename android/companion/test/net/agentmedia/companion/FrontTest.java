package net.agentmedia.companion;

import java.util.ArrayList;
import java.util.List;

/**
 * Host-side tests for whose focus loss a spoken clip is.
 *
 * This file used to test which channel one shared MediaSession named. That
 * mechanism was retired on 2026-08-15 when each channel got a card of its own,
 * and the tests for it went with it — what is left is the part that was always
 * the hard bit: telling our own speech's focus loss from somebody else's, when
 * the loss can arrive 37 seconds before the clip it belongs to is audible.
 */
public final class FrontTest {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<String>();

    public static void main(String[] args) {
        testSpeechTakesTheFront();
        testFinishedClipHandsTheFrontBack();
        testPausedSpeechIsNotInFront();
        testAHeldClipKeepsTheFront();
        testUnreachableSpeechFallsBackToMusic();
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


    private static void testSpeechTakesTheFront() {
        MpvState speech = playing("remote-20260814T190922-18480.mp3", 9.0);
        yes(FrontChannel.speechInFront(speech), "a running clip is in front");
    }


    private static void testFinishedClipHandsTheFrontBack() {
        MpvState speech = playing("clip.mp3", 4.0);
        speech.idleActive = true;              // end-file: sink-speech goes idle
        no(FrontChannel.speechInFront(speech), "a finished clip is not in front");
    }

    private static void testPausedSpeechIsNotInFront() {
        // The popup's Space pauses the speech broker. Nobody is holding it for
        // a resume, so it is not the audible channel.
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
        MpvState speech = playing("clip.mp3", 4.0);
        speech.paused = true;

        no(FrontChannel.speechInFront(speech, false),
           "a clip nobody is holding is still not in front");
        yes(FrontChannel.speechInFront(speech, true),
            "one that was paused on purpose is — its card has to outlive the "
            + "pause, or there is no button left to undo it with");

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
