package net.agentmedia.speedspike;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URI;

/**
 * The readout answers, and answers on loopback only.
 *
 * Worth a test rather than a squint because its failure mode is silence: a
 * bind that loses to a port already in use would leave `curl` refusing and the
 * spike looking dead from red5, which is exactly the diagnosis loop this whole
 * thing exists to shorten.
 */
public class ReadoutTest {

    public static void main(String[] args) throws Exception {
        Readout r = new Readout(() -> "trial 1  measured 1.6");
        r.start();
        Thread.sleep(300);

        int failures = 0;
        failures += check("bound to " + Readout.PORT, r.boundPort() == Readout.PORT);
        failures += check("serves the report",
                get("http://127.0.0.1:" + Readout.PORT + "/")
                        .contains("measured 1.6"));
        failures += check("any path answers the same thing",
                get("http://127.0.0.1:" + Readout.PORT + "/anything")
                        .contains("measured 1.6"));
        r.stop();
        Thread.sleep(200);
        failures += check("stops", r.boundPort() == -1);

        System.out.println(failures == 0 ? "ReadoutTest ok" : failures + " failed");
        if (failures != 0) System.exit(1);
    }

    private static String get(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) URI.create(url).toURL().openConnection();
        try (BufferedReader in = new BufferedReader(
                new InputStreamReader(c.getInputStream()))) {
            StringBuilder b = new StringBuilder();
            String line;
            while ((line = in.readLine()) != null) b.append(line).append('\n');
            return b.toString();
        } finally {
            c.disconnect();
        }
    }

    private static int check(String what, boolean ok) {
        System.out.println((ok ? "  ok   " : "  FAIL ") + what);
        return ok ? 0 : 1;
    }
}
