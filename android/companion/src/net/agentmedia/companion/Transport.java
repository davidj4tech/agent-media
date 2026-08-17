package net.agentmedia.companion;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The verbs, for whichever channel is being driven.
 *
 * Lifted out of ControlsActivity, which no longer exists: the transport now
 * sits under the channel card on the home screen rather than behind a second
 * screen with its own tab bar restating the three channels the home screen was
 * already showing. What is here is what the popup has and a media card does
 * not — seek by an amount, speed, volume, mute, chapters, seek-to.
 *
 * The buttons are TextViews, not Buttons. A platform Button arrives with the
 * default theme's grey capsule, its own padding and its own ripple, and there
 * is no styles.xml here to tell it otherwise — so the row read as Android's
 * furniture sitting inside somebody's app. These are drawn from
 * {@link Style} like everything else.
 */
final class Transport {

    /** Who to send to, and what to do afterwards. */
    interface Host {
        /** The channel currently being driven. */
        String channel();
        /** Something was pressed: re-poll rather than guess at the new state. */
        void refreshed();
    }

    private final Activity ctx;
    private final Host host;
    private final Handler main = new Handler(Looper.getMainLooper());
    private TextView playButton;
    private TextView chaptersButton;
    private TextView openButton;
    /**
     * Every button, against the verb it sends.
     *
     * Kept so {@link #apply} can take away the ones this channel does not have.
     * The book has no volume and no mute — a book is a thing you pause, not one
     * you silence — and drawing those anyway made two buttons whose only
     * possible answer was a toast saying no.
     */
    private final Map<TextView, String> needs = new LinkedHashMap<TextView, String>();

    Transport(Activity ctx, Host host) {
        this.ctx = ctx;
        this.host = host;
    }

    /**
     * The three rows: transport, levels, then the two that open a dialog.
     *
     * Order inside the first row is the popup's, and it is deliberate: the
     * play/pause sits in the middle, where a thumb lands without aiming.
     */
    LinearLayout build() {
        LinearLayout root = new LinearLayout(ctx);
        root.setOrientation(LinearLayout.VERTICAL);

        LinearLayout transport = row();
        transport.addView(verb("⏮", "prev", ""), weight());
        transport.addView(verb("−30", "seek", "-30"), weight());
        transport.addView(verb("−5", "seek", "-5"), weight());
        playButton = verb("⏸", "toggle", "");
        transport.addView(playButton, weight());
        transport.addView(verb("+5", "seek", "+5"), weight());
        transport.addView(verb("+30", "seek", "+30"), weight());
        transport.addView(verb("⏭", "next", ""), weight());
        root.addView(transport);

        LinearLayout levels = row();
        levels.addView(verb("vol −", "volume", "-5"), weight());
        levels.addView(verb("vol +", "volume", "+5"), weight());
        levels.addView(verb("spd −", "speed", "down"), weight());
        levels.addView(verb("spd +", "speed", "up"), weight());
        levels.addView(verb("1×", "speed", "reset"), weight());
        root.addView(levels);

        LinearLayout extras = row();
        TextView seekTo = button("seek to…");
        needs.put(seekTo, "seek");
        seekTo.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { askSeek(); }
        });
        extras.addView(seekTo, weight());
        chaptersButton = button("chapters");
        needs.put(chaptersButton, "chapter");
        chaptersButton.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { showChapters(); }
        });
        extras.addView(chaptersButton, weight());
        openButton = button("open…");
        openButton.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { askOpen(); }
        });
        extras.addView(openButton, weight());
        extras.addView(verb("mute", "mute", ""), weight());
        // Speech only: say the last clip again. The transport row above steps
        // within the clip being read, and neither of its arrows can go back
        // past the start of it — which is where "sorry, what?" usually lands.
        extras.addView(verb("replay", "replay", "1"), weight());
        root.addView(extras);

        return root;
    }

    /**
     * Follow the channel: state on the play button, and only the verbs it has.
     *
     * Gone, not greyed, for a verb the channel does not take at all — greyed
     * says "not now", and this is "not ever". Chapters is the one exception:
     * music and book both take it, and whether there are any comes and goes
     * with the track, so it stays put and dims, which is "not now" and true.
     */
    void apply(Channels.Channel c) {
        if (playButton != null) {
            playButton.setText(c != null && c.advancing() ? "⏸" : "▶");
        }
        for (Map.Entry<TextView, String> e : needs.entrySet()) {
            TextView b = e.getKey();
            if (b == chaptersButton) continue;
            boolean has = c == null || c.takes(e.getValue());
            b.setVisibility(has ? View.VISIBLE : View.GONE);
        }
        if (openButton != null) {
            // Music and book only, exactly as the popup's `o` is: speech has
            // no playlist to put a link into, and there it points at Tab.
            String name = host.channel();
            openButton.setVisibility("music".equals(name) || "book".equals(name)
                                     ? View.VISIBLE : View.GONE);
        }
        if (chaptersButton != null) {
            // Named for what the list is on this channel: speech's rows are
            // spoken turns, and "chapters" would promise the wrong thing.
            chaptersButton.setText(speech() ? "clips" : "chapters");
            boolean channelHas = c == null || c.takes("chapter");
            chaptersButton.setVisibility(channelHas ? View.VISIBLE : View.GONE);
            boolean may = c != null && c.mayHaveChapters();
            chaptersButton.setEnabled(may);
            chaptersButton.setAlpha(may ? 1f : 0.4f);
        }
    }

    /** Tint the pressable row to the channel it is driving. */
    void accent(String channel) {
        if (playButton != null) {
            playButton.setTextColor(Style.accent(channel));
            playButton.setBackground(pill(Style.accent(channel)));
        }
    }

    // ---- sending -----------------------------------------------------------

    private void send(final String action, final String arg) {
        final String target = host.channel();
        new Thread(new Runnable() {
            @Override public void run() {
                final String problem =
                        Channels.control(Settings.server(ctx), target, action, arg);
                main.post(new Runnable() {
                    @Override public void run() {
                        // Only failures are worth saying: a press that worked
                        // shows up in the next poll, a second later.
                        if (!problem.isEmpty()) toast(problem);
                        host.refreshed();
                    }
                });
            }
        }, "control-" + action).start();
    }

    private void askSeek() {
        final EditText input = new EditText(ctx);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        input.setHint("1:23:45, 12:30, or +90 / -5:00");
        // The field is built from the activity, not from the dialog's theme,
        // so left alone it arrives light inside a dark sheet.
        input.setTextColor(Style.INK);
        input.setHintTextColor(Style.FAINT);
        show(dialog()
                .setTitle("seek — " + host.channel())
                .setView(input)
                .setPositiveButton("go", (d, w) -> {
                    String t = input.getText().toString().trim();
                    if (!t.isEmpty()) send("seek", t);
                })
                .setNegativeButton("cancel", null));
    }

    /**
     * A builder that is dark, because the app is and the platform is not.
     *
     * Every view in this app is hand-built and hand-coloured from {@link Style},
     * and a dialog is the one surface that is not: {@code AlertDialog} draws
     * itself from the activity's theme, no theme is declared here, and the
     * platform default landed light. So the clip picker opened as a white sheet
     * over a dark app — the only light thing in it.
     *
     * The theme is named rather than the manifest changed. An application-wide
     * theme would repaint every window this app has, most of which cover
     * themselves anyway, to fix the two that ask the platform to draw.
     */
    private AlertDialog.Builder dialog() {
        return new AlertDialog.Builder(
                ctx, android.R.style.Theme_Material_Dialog_Alert);
    }

    /** Show it, on the app's own ground rather than Material's grey. */
    private void show(AlertDialog.Builder b) {
        AlertDialog d = b.create();
        Window w = d.getWindow();
        if (w != null) {
            GradientDrawable bg = new GradientDrawable();
            bg.setColor(Style.SURFACE);
            bg.setCornerRadius(ChannelCard.dp(ctx, 12));
            w.setBackgroundDrawable(bg);
        }
        d.show();
    }

    private void showChapters() {
        new Thread(new Runnable() {
            @Override public void run() {
                Loopback.Reply r = Loopback.get(
                        Settings.server(ctx),
                        "/chapters?channel=" + host.channel());
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
        boolean clips = speech();
        if (rows.isEmpty()) {
            toast(clips ? "nothing said yet" : "no chapters in this track");
            return;
        }
        final String[] labels = new String[rows.size()];
        for (int i = 0; i < rows.size(); i++) labels[i] = rows.get(i).label();
        show(dialog()
                .setTitle(clips ? "recent clips" : "chapters")
                // By ref, not by row: on speech the rows are history ids, and
                // a clip landing while this is open would renumber them.
                .setItems(labels, (d, which) ->
                        send("chapter", rows.get(which).ref())));
    }

    private boolean speech() {
        return "speech".equals(host.channel());
    }

    /**
     * Put a link on this channel — the popup's `o`, on a phone.
     *
     * Clipboard-first, because that is how a link arrives here: you copy it in
     * a browser and come back, and typing a YouTube URL on a soft keyboard is
     * not a thing anybody does twice. A buffered link is filled in and can be
     * played with one more tap; typing over it overrides it.
     *
     * It goes to `/share`, not to a play verb, and that is the point: sharing
     * a link and opening one are the same act from two directions, and the
     * far side already knows how to fetch, classify and route. The only thing
     * this end adds is the channel, because a sharer looking at the book
     * channel has already answered the question yt-dlp would be asked.
     */
    private void askOpen() {
        final String channel = host.channel();
        final EditText input = new EditText(ctx);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        input.setHint("paste or type a link");
        input.setTextColor(Style.INK);
        input.setHintTextColor(Style.FAINT);
        String buffered = clipboardLink();
        if (!buffered.isEmpty()) input.setText(buffered);
        show(dialog()
                .setTitle("open on " + channel)
                .setView(input)
                .setPositiveButton("play", (d, w) -> {
                    String t = input.getText().toString().trim();
                    if (!t.isEmpty()) open(t, channel);
                })
                .setNegativeButton("cancel", null));
    }

    /** The clipboard, when it holds something that looks like a link. */
    private String clipboardLink() {
        try {
            android.content.ClipboardManager cm =
                    (android.content.ClipboardManager)
                            ctx.getSystemService(Activity.CLIPBOARD_SERVICE);
            if (cm == null || !cm.hasPrimaryClip()) return "";
            android.content.ClipData clip = cm.getPrimaryClip();
            if (clip == null || clip.getItemCount() == 0) return "";
            CharSequence text = clip.getItemAt(0).coerceToText(ctx);
            String s = text == null ? "" : text.toString().trim();
            return s.startsWith("http://") || s.startsWith("https://") ? s : "";
        } catch (RuntimeException e) {
            // A clipboard this app is not allowed to read is an empty field,
            // not a crash on the way to a text box.
            return "";
        }
    }

    private void open(final String link, final String channel) {
        toast("opening on " + channel + "…");
        new Thread(new Runnable() {
            @Override public void run() {
                final ShareRequest.Result r = ShareRequest.send(
                        Settings.server(ctx), link, channel);
                main.post(new Runnable() {
                    @Override public void run() {
                        toast(r.message);
                        host.refreshed();
                    }
                });
            }
        }, "open-link").start();
    }

    // ---- the furniture -----------------------------------------------------

    private LinearLayout row() {
        LinearLayout r = new LinearLayout(ctx);
        r.setOrientation(LinearLayout.HORIZONTAL);
        r.setPadding(0, ChannelCard.dp(ctx, Style.gap(2)), 0, 0);
        return r;
    }

    private TextView verb(String label, final String action, final String arg) {
        TextView b = button(label);
        needs.put(b, action);
        b.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { send(action, arg); }
        });
        return b;
    }

    private TextView button(String label) {
        TextView b = new TextView(ctx);
        b.setText(label);
        b.setTextSize(Style.BODY);
        b.setTextColor(Style.INK);
        b.setGravity(Gravity.CENTER);
        b.setMinimumHeight(ChannelCard.dp(ctx, Style.TOUCH));
        b.setBackground(pill(Style.RULE));
        b.setClickable(true);
        b.setFocusable(true);
        return b;
    }

    private GradientDrawable pill(int edge) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(Style.SURFACE);
        d.setCornerRadius(ChannelCard.dp(ctx, 8));
        d.setStroke(ChannelCard.dp(ctx, 1), edge);
        return d;
    }

    private LinearLayout.LayoutParams weight() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        lp.leftMargin = ChannelCard.dp(ctx, Style.gap(1));
        lp.rightMargin = ChannelCard.dp(ctx, Style.gap(1));
        return lp;
    }

    private void toast(String message) {
        Toast t = Toast.makeText(ctx.getApplicationContext(), message,
                                 Toast.LENGTH_LONG);
        t.setGravity(Gravity.BOTTOM, 0, ChannelCard.dp(ctx, 48));
        t.show();
    }
}
