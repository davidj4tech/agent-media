package net.agentmedia.speedspike;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * The spike's screen: a clip URL, one button that runs the trials, and a few
 * that play at a speed so the result can be judged by ear.
 *
 * The numbers are also served on {@code 127.0.0.1:8772} — see {@link Readout} —
 * because "screenshot the activity" was the previous spike's readout and it
 * made every result a retyping job.
 *
 * Nothing here is meant to survive into the app. If the verdict is that
 * MediaPlayer holds its speed, what ships is a player class the companion talks
 * to, not this.
 */
public class MainActivity extends Activity {

    /**
     * A real rendered reply from red5's clip cache: 72 seconds, mono, 24 kHz,
     * straight out of the TTS that feeds the speech channel. Testing against a
     * music file would answer a question nobody asked — speech is the only
     * channel this player is proposed for, and its output is unusually narrow.
     */
    private static final String DEFAULT_URL =
            "http://100.103.43.93:8780/audio/"
            + "20260803T215131-3758518-b95094--claude-code--000.mp3";

    private final Handler handler = new Handler(Looper.getMainLooper());
    private SpeedTrials trials;
    private EditText urlField;
    private TextView logView;
    private Button run;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        trials = Spike.trials(this);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(24, 24, 24, 24);
        root.setBackgroundColor(Color.BLACK);

        root.addView(text("MediaPlayer speed spike — does 1.6x hold?\n"
                + "Trials measure position advance against wall clock; the "
                + "listen row is for your ear.\n"
                + "Results also at 127.0.0.1:" + Readout.PORT, 13f, Color.WHITE));

        urlField = new EditText(this);
        urlField.setText(DEFAULT_URL);
        urlField.setTextSize(11f);
        urlField.setTextColor(Color.WHITE);
        urlField.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        root.addView(urlField);

        run = new Button(this);
        run.setText("Run the five trials (~1 min, out loud)");
        run.setOnClickListener(v -> start(false));
        root.addView(run);

        LinearLayout listen = new LinearLayout(this);
        listen.setOrientation(LinearLayout.HORIZONTAL);
        listen.addView(speedButton("1.0x", 1.0f, false));
        listen.addView(speedButton("1.6x", 1.6f, false));
        listen.addView(speedButton("->1.6x", 1.6f, true));
        Button stop = new Button(this);
        stop.setText("stop");
        stop.setOnClickListener(v -> trials.stopListening());
        listen.addView(stop);
        root.addView(listen);

        logView = new TextView(this);
        logView.setTypeface(Typeface.MONOSPACE);
        logView.setTextSize(10f);
        logView.setTextColor(Color.WHITE);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(logView);
        root.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);
        redraw();
        autorun(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        autorun(intent);
    }

    /**
     * Start the trials from an intent, so a run costs no tap.
     *
     * {@code am start} is one of the few system commands still open to a
     * non-shell uid from Termux, and this phone has no adb — so this extra is
     * the difference between "David, open the app and press the button" and
     *
     * <pre>
     *   ssh p8a am start -n net.agentmedia.speedspike/.MainActivity --ez run true
     * </pre>
     *
     * {@code --ez mute true} runs it at zero gain, for the repeat runs that
     * only want the numbers back.</pre>
     *
     * which, with the results posting themselves to red5 when the run ends,
     * closes the loop entirely. Installing a new build still needs a human:
     * {@code pm install} from Termux dies on "Reverse mode only supported from
     * shell", and shell uid means adb.
     */
    private void autorun(Intent intent) {
        if (intent == null || !intent.getBooleanExtra("run", false)) return;
        String url = intent.getStringExtra("url");
        if (url != null && !url.trim().isEmpty()) urlField.setText(url.trim());
        start(intent.getBooleanExtra("mute", false));
    }

    /** A listen button: play at this speed, or nudge what is already playing. */
    private Button speedButton(String label, float speed, boolean nudge) {
        Button b = new Button(this);
        b.setText(label);
        b.setOnClickListener(v -> {
            if (nudge) {
                trials.nudge(speed);
            } else {
                new Thread(() -> trials.listen(url(), speed)).start();
            }
        });
        return b;
    }

    /**
     * Hand the run to the foreground service rather than running it here.
     *
     * The activity is the wrong owner: background it and the process is frozen
     * a minute later, mid-trial. See {@link Spike}.
     */
    private void start(boolean muted) {
        run.setText("running… (see the notification)");
        Intent i = new Intent(this, SpikeService.class)
                .putExtra("run", true)
                .putExtra("mute", muted)
                .putExtra("url", url());
        startForegroundService(i);
        handler.postDelayed(() -> run.setText("Run the five trials again"), 3000);
    }

    private String url() {
        String s = urlField.getText().toString().trim();
        return s.isEmpty() ? DEFAULT_URL : s;
    }

    /** The log lives in {@link Spike} now, so poll it rather than be told. */
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            logView.setText(Spike.report());
            handler.postDelayed(this, 700);
        }
    };

    private void redraw() {
        logView.setText(Spike.report());
    }

    private TextView text(String s, float size, int colour) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(size);
        t.setTextColor(colour);
        return t;
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(tick);
    }

    @Override
    protected void onPause() {
        handler.removeCallbacks(tick);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        // Deliberately not stopping playback on rotate/background: the ear test
        // wants to keep playing while the screen is off, which is also a small
        // free answer about what a real in-app player would need.
        super.onDestroy();
    }
}
