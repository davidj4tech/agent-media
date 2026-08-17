package net.agentmedia.companion;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.Map;

/**
 * Which agent-media this phone is a client of, and where its sound comes out.
 *
 * The two questions {@link Server} keeps apart, kept apart on screen too: the
 * address is one block, the playback location is another, and the second is
 * <em>not</em> implied by the first. Pointing the app at red5 does not move the
 * sound to this phone — it moves it to red5's speakers — and that surprise is
 * the whole reason the choice is drawn as its own list rather than inferred.
 *
 * There is a Test button because every alternative on this device is a sideload
 * and a squint at the phone screen: a wrong port, a listener bound to loopback
 * on the far side, or a token that does not match all fail the same silent way
 * from the home screen. Here they each say which one it was, before the
 * configuration is saved over a working one.
 */
public class SettingsActivity extends Activity {

    private final Handler main = new Handler(Looper.getMainLooper());

    private EditText host;
    private EditText control;
    private EditText music;
    private EditText speech;
    private EditText book;
    private EditText token;
    private TextView verdict;

    private String playback = Server.PHONE;
    private LinearLayout playbackList;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);

        Server current = Settings.server(this);
        playback = current.playback;

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Style.GROUND);
        int pad = dp(Style.gap(4));
        root.setPadding(pad, pad, pad, dp(Style.gap(6)));

        TextView heading = new TextView(this);
        heading.setText("Server");
        heading.setTextSize(Style.TITLE);
        heading.setTextColor(Style.INK);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(heading);

        root.addView(note("The machine running `media`. On this phone that is "
                + "Termux, and the address is " + Server.LOOPBACK + "."));

        root.addView(label("Address"));
        host = field(current.host, InputType.TYPE_TEXT_VARIATION_URI);
        root.addView(host);

        root.addView(label("Control port (media-share)"));
        control = field(String.valueOf(current.control), InputType.TYPE_CLASS_NUMBER);
        root.addView(control);

        root.addView(label("Token"));
        token = field(current.token, InputType.TYPE_TEXT_VARIATION_PASSWORD);
        token.setTypeface(Typeface.MONOSPACE);
        root.addView(token);
        root.addView(note("Required for any server that is not this phone: the "
                + "control endpoint can start playback, so anything that can "
                + "reach it can drive the speakers. Set MEDIA_SHARE_TOKEN in "
                + "~/.config/agent-media.env on that machine to the same value."));

        root.addView(label("Where the sound comes out"));
        playbackList = new LinearLayout(this);
        playbackList.setOrientation(LinearLayout.VERTICAL);
        root.addView(playbackList);
        buildPlaybackList();

        root.addView(label("mpv bridges"));
        root.addView(note("The media cards in the shade are built from these, "
                + "one socket per channel. They follow the sound rather than "
                + "the server — local while playback is on this phone — so "
                + "only the ports are yours to set. A bridge that is not "
                + "running costs exactly its own card and nothing else."));
        music = portField("Music", current.music, root);
        speech = portField("Speech", current.speech, root);
        book = portField("Book", current.book, root);

        verdict = new TextView(this);
        verdict.setTextSize(Style.BODY);
        verdict.setTextColor(Style.MUTED);
        verdict.setPadding(0, dp(Style.gap(4)), 0, 0);
        root.addView(verdict);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setPadding(0, dp(Style.gap(3)), 0, 0);
        buttons.addView(button("Test", false, new View.OnClickListener() {
            @Override public void onClick(View v) { test(); }
        }), weight());
        buttons.addView(button("Save", true, new View.OnClickListener() {
            @Override public void onClick(View v) { save(); }
        }), weight());
        root.addView(buttons);

        ScrollView scroller = new ScrollView(this);
        scroller.addView(root, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        scroller.setBackgroundColor(Style.GROUND);
        setContentView(scroller);
        Edges.fit(root);
    }

    // ---- the form ----------------------------------------------------------

    /** What the fields say right now, valid or not. */
    private Server typed() {
        return new Server(
                host.getText().toString(),
                Server.port(control.getText().toString(), 0),
                Server.port(music.getText().toString(), 0),
                Server.port(speech.getText().toString(), 0),
                Server.port(book.getText().toString(), 0),
                token.getText().toString(),
                playback);
    }

    private void buildPlaybackList() {
        playbackList.removeAllViews();
        String[] all = {Server.PHONE, Server.SERVER, Server.BUILTIN};
        for (final String option : all) {
            playbackList.addView(playbackRow(option));
        }
    }

    private View playbackRow(final String option) {
        boolean available = Server.available(option);
        boolean chosen = option.equals(playback);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.VERTICAL);
        int p = dp(Style.gap(3));
        row.setPadding(p, p, p, p);
        row.setMinimumHeight(dp(Style.TOUCH));

        GradientDrawable d = new GradientDrawable();
        d.setColor(Style.SURFACE);
        d.setCornerRadius(dp(8));
        d.setStroke(dp(chosen ? 2 : 1), chosen ? Style.SPEECH : Style.RULE);
        row.setBackground(d);

        TextView name = new TextView(this);
        name.setText((chosen ? "● " : "○ ") + Server.label(option));
        name.setTextSize(Style.HEAD);
        name.setTextColor(available ? Style.INK : Style.FAINT);
        row.addView(name);

        TextView why = new TextView(this);
        why.setText(explain(option));
        why.setTextSize(Style.BODY);
        why.setTextColor(available ? Style.MUTED : Style.FAINT);
        row.addView(why);

        if (available) {
            row.setClickable(true);
            row.setFocusable(true);
            row.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) {
                    playback = option;
                    buildPlaybackList();
                }
            });
        } else {
            row.setAlpha(0.5f);
        }

        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(Style.gap(2));
        row.setLayoutParams(lp);
        return row;
    }

    private static String explain(String option) {
        if (Server.SERVER.equals(option)) {
            return "mpv plays at the server. This app is a remote control, "
                 + "holds no audio focus, and needs no Termux here.";
        }
        if (Server.BUILTIN.equals(option)) {
            return "The server fetches and renders, the app plays it. Not "
                 + "built yet — this is the one that needs no mpv on the "
                 + "phone at all.";
        }
        return "mpv in Termux on this phone. The app holds audio focus on its "
             + "behalf, because mpv ignores it.";
    }

    // ---- the two buttons ---------------------------------------------------

    private void test() {
        final Server candidate = typed();
        String problem = candidate.problem();
        if (problem != null) {
            say(problem, Style.WARN);
            return;
        }
        say("asking " + candidate.authority() + "…", Style.MUTED);
        new Thread(new Runnable() {
            @Override public void run() {
                final Loopback.Reply r = Loopback.get(candidate, "/channels");
                final Map<String, Channels.Channel> channels =
                        r.ok() ? Channels.parse(r.body) : null;
                main.post(new Runnable() {
                    @Override public void run() {
                        if (!r.reached()) { say(r.failure, Style.WARN); return; }
                        if (r.refused()) { say(Loopback.REFUSED, Style.WARN); return; }
                        if (!r.ok()) {
                            say("the server answered " + r.status, Style.WARN);
                            return;
                        }
                        say("reached " + candidate.authority() + " — "
                                + (channels == null ? 0 : channels.size())
                                + " channels", Style.OK);
                    }
                });
            }
        }, "settings-test").start();
    }

    private void save() {
        Server candidate = typed();
        String problem = Settings.save(this, candidate);
        if (problem != null) {
            say(problem, Style.WARN);
            return;
        }
        // The service reads its configuration once, at startup, and builds
        // three mpv connections, the focus claim and the silent track out of
        // it. Restarting is how those are rebuilt — the same work a
        // reconfigure would do, done where somebody is watching it happen and
        // can read the log line it leaves.
        Intent svc = new Intent(this, CompanionService.class);
        stopService(svc);
        startForegroundService(svc);
        Toast.makeText(this, "server: " + candidate.describe(),
                       Toast.LENGTH_LONG).show();
        finish();
    }

    private void say(String line, int colour) {
        verdict.setText(line);
        verdict.setTextColor(colour);
    }

    // ---- the pieces --------------------------------------------------------

    private EditText portField(String name, int value, LinearLayout root) {
        root.addView(label(name + " port"));
        EditText f = field(String.valueOf(value), InputType.TYPE_CLASS_NUMBER);
        root.addView(f);
        return f;
    }

    private EditText field(String value, int inputType) {
        EditText f = new EditText(this);
        f.setText(value);
        f.setSingleLine(true);
        f.setInputType(InputType.TYPE_CLASS_TEXT | inputType);
        f.setTextSize(Style.HEAD);
        f.setTextColor(Style.INK);
        f.setHintTextColor(Style.FAINT);
        f.setMinimumHeight(dp(Style.TOUCH));
        int p = dp(Style.gap(3));
        f.setPadding(p, p, p, p);

        GradientDrawable d = new GradientDrawable();
        d.setColor(Style.SURFACE);
        d.setCornerRadius(dp(8));
        d.setStroke(dp(1), Style.RULE);
        f.setBackground(d);
        return f;
    }

    private TextView button(String text, boolean primary, View.OnClickListener onClick) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(Style.HEAD);
        t.setTextColor(primary ? Style.GROUND : Style.INK);
        t.setGravity(Gravity.CENTER);
        t.setMinimumHeight(dp(Style.TOUCH));
        t.setClickable(true);
        t.setFocusable(true);
        t.setOnClickListener(onClick);

        GradientDrawable d = new GradientDrawable();
        d.setColor(primary ? Style.SPEECH : Style.SURFACE);
        d.setCornerRadius(dp(8));
        d.setStroke(dp(1), primary ? Style.SPEECH : Style.RULE);
        t.setBackground(d);

        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        lp.leftMargin = dp(Style.gap(1));
        lp.rightMargin = dp(Style.gap(1));
        t.setLayoutParams(lp);
        return t;
    }

    private TextView label(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(Style.LABEL);
        t.setTextColor(Style.FAINT);
        t.setTypeface(Typeface.MONOSPACE);
        t.setAllCaps(true);
        t.setLetterSpacing(0.08f);
        t.setPadding(0, dp(Style.gap(4)), 0, dp(Style.gap(1)));
        return t;
    }

    private TextView note(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(Style.BODY);
        t.setTextColor(Style.MUTED);
        t.setPadding(0, dp(Style.gap(2)), 0, 0);
        return t;
    }

    private LinearLayout.LayoutParams weight() {
        return new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
