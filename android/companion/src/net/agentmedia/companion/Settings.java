package net.agentmedia.companion;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Where the {@link Server} configuration is kept, and the only class that knows
 * it is SharedPreferences.
 *
 * Every screen needs the answer — the share sheet fires with no service bound,
 * the recent list runs before the service is up — so this is a static read
 * against a Context rather than something handed down from the service. It is
 * a few string lookups; the alternative was binding a service to find out where
 * to send a POST.
 *
 * Same prefs file the focus switch already uses, because it is the same app's
 * one file and a second would be a second thing to lose on a reinstall.
 */
final class Settings {

    /** Shared with CompanionService. One file, one name. */
    static final String PREFS = "companion";

    private Settings() { }

    /** The configured server, or the defaults if what is stored cannot be used. */
    static Server server(Context context) {
        SharedPreferences p = prefs(context);
        Map<String, String> m = new LinkedHashMap<String, String>();
        for (String key : Server.defaults().toMap().keySet()) {
            m.put(key, p.getString(key, null));
        }
        return Server.from(m).orDefaults();
    }

    /**
     * Store a configuration. Returns why it was refused, or null.
     *
     * Validation is here rather than only on the screen because this is the
     * last place before the file: a configuration that {@link Server#problem}
     * rejects would be read back as the defaults on the next launch, which
     * looks exactly like the save not happening.
     */
    static String save(Context context, Server server) {
        String problem = server.problem();
        if (problem != null) return problem;
        SharedPreferences.Editor e = prefs(context).edit();
        for (Map.Entry<String, String> kv : server.toMap().entrySet()) {
            e.putString(kv.getKey(), kv.getValue());
        }
        e.apply();
        return null;
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
