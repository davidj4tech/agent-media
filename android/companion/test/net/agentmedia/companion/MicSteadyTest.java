package net.agentmedia.companion;

/**
 * The mic has to be held, not sampled, before anything acts on it.
 *
 * Every case here is a trace from p8a rather than an invented one, because the
 * first version of this test invented a plausible case — dictation stopping and
 * restarting its recording between two words — that the platform does not
 * produce. Voice typing holds one recording open across the gaps in speech;
 * what stops and restarts, endlessly, is the phone's own recogniser.
 */
public class MicSteadyTest {

    public static void main(String[] args) {
        int failures = 0;

        // The baseline on p8a: com.google.android.as holds the microphone for
        // ~650ms and releases it for ~350ms, around the clock, whether or not
        // anything is playing. It is not a person and must never look like one.
        MicSteady m = new MicSteady();
        long t = 1000;
        m.update(0, t);
        boolean engaged = false;
        for (int i = 0; i < 60; i++) {
            engaged |= m.update(1, t);
            t += 650;
            engaged |= m.update(1, t);
            engaged |= m.update(0, t);
            t += 350;
            engaged |= m.update(0, t);
        }
        failures += check("a minute of the recogniser's cycling never engages",
                !engaged);

        // The same recogniser while our own audio plays: it holds the mic for
        // ~2s a time, which is exactly as long as a person would. Duration
        // cannot tell them apart, so once the cycling has been seen, only
        // company counts.
        m = new MicSteady();
        t = 1000;
        m.update(0, t);
        engaged = false;
        for (int i = 0; i < 10; i++) {
            engaged |= m.update(1, t);
            t += 2000;
            engaged |= m.update(1, t);
            engaged |= m.update(0, t);
            t += 900;
            engaged |= m.update(0, t);
        }
        failures += check("two-second holds from a cycling recogniser are still not a person",
                !engaged);
        failures += check("but a second recording on top of it still is",
                m.update(2, t + 100));

        // A restart must not trust duration before it knows the phone: the
        // history is empty then, and the cycling it is waiting to detect would
        // engage the hold first. Observed on p8a — two pauses in the seconds
        // after the app came up, and none afterwards.
        m = new MicSteady();
        m.update(0, 0);
        failures += check("duration is not trusted in the first minute",
                !m.update(1, 3000) && !m.update(1, 20000));
        failures += check("but company still is",
                m.update(2, 21000));

        // Dictation on a phone whose microphone nothing samples: there is no
        // sampler's hold to out-wait, so the floor is the short one. 1.5s of
        // Sam talking into David's dictation was the cost of the old floor
        // here — "a bit slow to stop and got some of your texts in my
        // dictation".
        m = new MicSteady();
        m.update(0, 0);
        m.update(0, 61000);
        m.update(1, 62000);
        failures += check("a flicker of mic is not yet a person",
                !m.update(1, 62200));
        failures += check("but 400ms on a quiet phone is",
                m.update(1, 62500));
        failures += check("and it stays while the recording is held",
                m.update(1, 70000));

        // It ends, and the baseline is all that is left.
        failures += check("a stop has not released 400ms later",
                m.update(0, 70400));
        failures += check("but has by 900ms",
                !m.update(0, 70900));
        failures += check("and the recogniser underneath does not re-engage it",
                !m.update(1, 71200) && !m.update(0, 71800));

        // A second recording on top of the baseline is somebody arriving:
        // a call, a voice session, dictation starting while it cycles. No
        // waiting to do — the count is the evidence, and barge-in needs speed.
        m = new MicSteady();
        m.update(1, 1000);
        failures += check("one recording alone is the baseline",
                !m.update(1, 1400));
        failures += check("a second is believed at once",
                m.update(2, 1450));
        failures += check("and holds while the crowd persists",
                m.update(2, 3000));
        // Back to the baseline's cycling, which is how a crowd really ends.
        m.update(1, 3300);
        m.update(0, 3500);
        // The tail is now the crowd grace plus the release window — a couple
        // of seconds before Sam picks up again. Deliberate: resuming into the
        // gap between two things somebody is saying is worse than waiting.
        failures += check("the baseline alone still holds it briefly",
                m.update(0, 4400));
        failures += check("but lets go once grace and release have passed",
                !m.update(0, 5600));

        // Dictation with the recogniser cycling underneath it: the count
        // alternates 2, 1, 2, 1 about once a second, and the microphone never
        // closes, because dictation holds it. The hold must not follow the
        // count down — that is the "cutting in and cutting out" David heard.
        m = new MicSteady();
        long d = 1000;
        m.update(1, d);                       // baseline alone
        m.update(2, d + 500);                 // dictation starts
        boolean dropped = false;
        for (int i = 0; i < 12; i++) {
            d += 1000;
            dropped |= !m.update(1, d);       // recogniser lets go, mic still open
            d += 400;
            dropped |= !m.update(2, d);       // and takes it again
        }
        failures += check("a hold does not flicker while the baseline cycles under it",
                !dropped);

        // Dictation stops: now the microphone really does close, in the gaps
        // of the baseline's own cycling.
        d += 1000;
        m.update(1, d);
        m.update(0, d + 200);                 // the mic closes -- nobody is holding it
        failures += check("and it is not released the instant that happens",
                m.update(0, d + 400));
        failures += check("but is once the grace and the release window pass",
                !m.update(0, d + 2600));

        // The watch is event-driven and events stop; a pending change has to
        // be able to say when to look again or it never lands.
        m = new MicSteady();
        m.update(0, 0);
        m.update(1, 100);
        failures += check("a pending engage says when to look again",
                m.pendingInMs(200) == MicSteady.QUIET_ENGAGE_MS - 100);
        failures += check("a fresh watch has nothing pending",
                new MicSteady().pendingInMs(0) == -1);

        // An app that starts while the mic is already open must not assume the
        // worst: it has no idea how long it has been open.
        MicSteady fresh = new MicSteady();
        failures += check("a mic already open at startup is not yet a person",
                !fresh.update(1, 5000));
        failures += check("and needs company to become one inside the first minute",
                fresh.update(2, 6600));

        // Three dictations in a minute is not a sampler, it is David: dictate,
        // read the reply, dictate again. While "three runs inside a minute"
        // was the rule, his own third press taught this class that the phone
        // cycles, the duration rule switched off, and with the sampler blocked
        // no company was ever coming — "pressing the mic paused your speech
        // the first few times but failed to work after that."
        m = new MicSteady();
        m.update(0, 0);
        m.update(0, 61000);
        long p = 62000;
        boolean everMissed = false;
        for (int i = 0; i < 6; i++) {
            // Press, speak for a second, stop; press again four seconds later.
            // Faster than a conversation, because this is David testing the
            // button — which is how the fifth press found the last version.
            m.update(1, p);
            everMissed |= !m.update(1, p + 1000);
            m.update(0, p + 1200);
            m.update(0, p + 2000);
            p += 5000;
        }
        failures += check("six dictations in a row are all still a person",
                !everMissed);

        // The sampler itself: same three runs, seconds apart rather than tens.
        m = new MicSteady();
        m.update(0, 0);
        m.update(0, 61000);
        p = 62000;
        for (int i = 0; i < 4; i++) {
            m.update(1, p);
            m.update(0, p + 700);
            p += 1100;
        }
        failures += check("but a burst of them does",
                !m.update(1, p) && !m.update(1, p + 2000));
        failures += check("and company is still believed at once",
                m.update(2, p + 2100));

        // What the phone is willing to say about each recording, and what that
        // is worth. The failure this pins: Android silences whichever recorder
        // loses priority, so a Gboard dictation over a cycling baseline shows
        // as one heard recording and one silenced — and while only the heard
        // ones were counted, the crowd never arrived, the hold never engaged,
        // and Sam talked straight through David pressing the mic button.
        failures += check("a silenced recording alone is nobody",
                MicSteady.counting(0, 1) == 0);
        failures += check("and neither are two of them",
                MicSteady.counting(0, 2) == 0);
        failures += check("dictation on its own is one",
                MicSteady.counting(1, 0) == 1);
        failures += check("dictation that silenced the baseline is company",
                MicSteady.counting(1, 1) == 2);
        failures += check("and so it engages at once, cycling or not",
                new MicSteady().update(MicSteady.counting(1, 1), 9000));
        failures += check("an idle phone with a blocked sampler stays shut",
                !new MicSteady().update(MicSteady.counting(0, 1), 9000));

        System.out.println(failures == 0 ? "MicSteadyTest ok" : failures + " failed");
        if (failures != 0) System.exit(1);
    }

    private static int check(String what, boolean ok) {
        System.out.println((ok ? "  ok   " : "  FAIL ") + what);
        return ok ? 0 : 1;
    }
}
