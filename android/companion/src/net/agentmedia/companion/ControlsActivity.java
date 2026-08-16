package net.agentmedia.companion;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.util.List;
import java.util.Map;

/**
 * The popup's transport, on the phone.
 *
 * The shade already gives every channel a card, and a card is play/pause/next.
 * This is the rest of what {@code media-popup} does: which channel, how far in,
 * seek by an amount, speed, volume, mute, chapters. The half of the popup that
 * is about tmux — replay the clip at the copy-mode cursor, page the pane
 * behind, jump to the pane that said it — is not here and cannot be; there is
 * no pane to go to.
 *
 * Polls {@code /channels} while it is on screen and stops when it is not, which
 * is the same bargain the popup makes: it is a window onto state, and it costs
 * nothing when nobody is looking.
 */
public class ControlsActivity extends Activity {

    private static final long POLL_MS = 1000;

    private final Handler main = new Handler(Looper.getMainLooper());
    private String channel = "music";
    private Map<String, Channels.Channel> state;

    private LinearLayout tabs;
    private TextView heading;
    private TextView detail;
    private TextView clock;
    private ProgressBar bar;
    private Button playButton;
    private Button chaptersButton;
    private boolean polling;

    private final Runnable tick = new Runnable() {
        @Override public void run() {
            if (!polling) return;
            refresh();
            main.postDelayed(this, POLL_MS);
        }
    };

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);
        int p = dp(14);
        root.setPadding(p, p, p, p);

        tabs = new LinearLayout(this);
        tabs.setOrientation(LinearLayout.HORIZONTAL);
        for (final String name : Channels.ORDER) {
            Button b = new Button(this);
            b.setAllCaps(false);
            b.setText(name);
            b.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) {
                    channel = name;
                    render();
                }
            });
            tabs.addView(b, new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        }
        root.addView(tabs);

        heading = text(22, Color.WHITE);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        heading.setPadding(0, dp(14), 0, 0);
        root.addView(heading);

        detail = text(14, Color.parseColor("#999999"));
        root.addView(detail);

        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(1000);
        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(8)));

        clock = text(15, Color.parseColor("#cccccc"));
        clock.setPadding(0, dp(4), 0, dp(10));
        root.addView(clock);

        // Transport, in the order the popup lists it: prev, big jumps, the
        // play/pause in the middle where a thumb lands, then forward.
        LinearLayout transport = new LinearLayout(this);
        transport.setOrientation(LinearLayout.HORIZONTAL);
        transport.addView(verb("⏮", "prev", ""), weight());
        transport.addView(verb("−30", "seek", "-30"), weight());
        transport.addView(verb("−5", "seek", "-5"), weight());
        playButton = verb("⏸", "toggle", "");
        transport.addView(playButton, weight());
        transport.addView(verb("+5", "seek", "+5"), weight());
        transport.addView(verb("+30", "seek", "+30"), weight());
        transport.addView(verb("⏭", "next", ""), weight());
        root.addView(transport);

        LinearLayout levels = new LinearLayout(this);
        levels.setOrientation(LinearLayout.HORIZONTAL);
        levels.addView(verb("vol −", "volume", "-5"), weight());
        levels.addView(verb("vol +", "volume", "+5"), weight());
        levels.addView(verb("spd −", "speed", "down"), weight());
        levels.addView(verb("spd +", "speed", "up"), weight());
        levels.addView(verb("1×", "speed", "reset"), weight());
        root.addView(levels);

        LinearLayout extras = new LinearLayout(this);
        extras.setOrientation(LinearLayout.HORIZONTAL);
        Button seekTo = button("seek to…");
        seekTo.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { askSeek(); }
        });
        extras.addView(seekTo, weight());
        chaptersButton = button("chapters");
        chaptersButton.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { showChapters(); }
        });
        extras.addView(chaptersButton, weight());
        extras.addView(verb("mute", "mute", ""), weight());
        root.addView(extras);

        setContentView(root);
    }

    @Override
    protected void onResume() {
        super.onResume();
        polling = true;
        main.post(tick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        // A window onto state costs nothing when nobody is looking at it.
        polling = false;
        main.removeCallbacks(tick);
    }

    // ---- state ------------------------------------------------------------

    private void refresh() {
        new Thread(new Runnable() {
            @Override public void run() {
                final Map<String, Channels.Channel> got =
                        Channels.fetch(Loopback.PORT);
                main.post(new Runnable() {
                    @Override public void run() {
                        state = got;
                        render();
                    }
                });
            }
        }, "channels-poll").start();
    }

    private void render() {
        for (int i = 0; i < tabs.getChildCount(); i++) {
            Button b = (Button) tabs.getChildAt(i);
            boolean on = Channels.ORDER[i].equals(channel);
            b.setTypeface(on ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
            b.setTextColor(on ? Color.WHITE : Color.parseColor("#777777"));
        }
        Channels.Channel c = state == null ? null : state.get(channel);
        if (c == null) {
            heading.setText("…");
            detail.setText("");
            clock.setText("");
            bar.setProgress(0);
            return;
        }
        heading.setText(c.heading());
        detail.setText(c.detail());
        clock.setText(c.clock());
        float f = c.progress();
        bar.setVisibility(f < 0 ? View.INVISIBLE : View.VISIBLE);
        if (f >= 0) bar.setProgress((int) (f * 1000));
        playButton.setText(c.playing ? "⏸" : "▶");
        chaptersButton.setEnabled(c.mayHaveChapters());
    }

    // ---- verbs ------------------------------------------------------------

    private Button verb(String label, final String action, final String arg) {
        Button b = button(label);
        b.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { send(action, arg); }
        });
        return b;
    }

    private void send(final String action, final String arg) {
        final String target = channel;
        new Thread(new Runnable() {
            @Override public void run() {
                final String problem =
                        Channels.control(Loopback.PORT, target, action, arg);
                main.post(new Runnable() {
                    @Override public void run() {
                        // Only failures are worth saying: a press that worked
                        // shows up in the next poll, a second later.
                        if (!problem.isEmpty()) toast(problem);
                        refresh();
                    }
                });
            }
        }, "control-" + action).start();
    }

    private void askSeek() {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        input.setHint("1:23:45, 12:30, or +90 / -5:00");
        new AlertDialog.Builder(this)
                .setTitle("seek — " + channel)
                .setView(input)
                .setPositiveButton("go", (d, w) -> {
                    String t = input.getText().toString().trim();
                    if (!t.isEmpty()) send("seek", t);
                })
                .setNegativeButton("cancel", null)
                .show();
    }

    private void showChapters() {
        new Thread(new Runnable() {
            @Override public void run() {
                Loopback.Reply r = Loopback.get(Loopback.PORT, "/chapters");
                final List<Chapters.Chapter> rows =
                        r.ok() ? Chapters.parse(r.body)
                               : java.util.Collections.<Chapters.Chapter>emptyList();
                main.post(new Runnable() {
                    @Override public void run() { chapterDialog(rows); }
                });
            }
        }, "chapters-fetch").start();
    }

    private void chapterDialog(final List<Chapters.Chapter> rows) {
        if (rows.isEmpty()) {
            toast("no chapters in this track");
            return;
        }
        final String[] labels = new String[rows.size()];
        for (int i = 0; i < rows.size(); i++) labels[i] = rows.get(i).label();
        new AlertDialog.Builder(this)
                .setTitle("chapters")
                .setItems(labels, (d, which) ->
                        send("chapter", Integer.toString(rows.get(which).number)))
                .show();
    }

    // ---- small view helpers -----------------------------------------------

    private Button button(String label) {
        Button b = new Button(this);
        b.setAllCaps(false);
        b.setText(label);
        return b;
    }

    private TextView text(int size, int colour) {
        TextView t = new TextView(this);
        t.setTextSize(size);
        t.setTextColor(colour);
        t.setSingleLine(true);
        t.setEllipsize(android.text.TextUtils.TruncateAt.END);
        return t;
    }

    private LinearLayout.LayoutParams weight() {
        return new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
    }

    private void toast(String message) {
        Toast t = Toast.makeText(getApplicationContext(), message,
                                 Toast.LENGTH_LONG);
        t.setGravity(Gravity.BOTTOM, 0, dp(48));
        t.show();
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
