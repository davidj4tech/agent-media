# Frequently asked questions

Short answers to the things people run into with agent-media and the
Sasonica app. Each one says what is going on, gives the command to check or
fix it, and points at the reference doc or `server-contract.md` section
that has the full story.

Device names, hosts and paths in commands are examples; use your own.

**In the app**

- Why aren't replies playing automatically?
- Speech keeps pausing by itself, or voice typing says another app is using the mic.
- Why does the voice sometimes change to a different one?
- Why is text bigger in the app than in the browser?
- How do I link to a thread?

**On the server**

- Can I delete `~/.cache/agent-media` to free disk?
- I changed `agent-media.env` and nothing changed.
- YouTube won't download on my VPS.
- A Codex session is stuck on "Hooks need review".
- Mopidy or Iris won't start after upgrading to Mopidy 4.
- Keys on the canvas do strange things (open a search, scroll).

**On a phone running Termux**

- Speech is silent on the phone, but everything looks healthy.
- `cryptography` fails with "cannot locate symbol PyLong_Type".
- YouTube music fails with HTTP 403.
- How do I update a Termux install?

---

## In the app

### Why aren't replies playing automatically?

Your phone is probably on silent or Do Not Disturb. With **Settings → When
you're busy → Hold replies while on silent** on (the default), a quiet phone
holds every normal reply, even the open thread's. The thread shows an "On
silent: replies wait" (or "Do Not Disturb: replies wait") chip with
**Speak anyway**. Held replies keep a Play button, and **Catch me up** reads
out what waited. Turn the setting off if you want silent to hold only alerts.

See also: `server-contract.md` §6.22 "Can David be spoken to now"
(`held_why`, `quiet_kind`, `quiet_replies`).

### Speech keeps pausing by itself, or voice typing says another app is using the mic.

On many Android phones (Pixels especially), Android System Intelligence
opens the microphone over and over, all day. The app reads each one as you
starting to talk, so it pauses the reply. Blocking that app's mic stops it.
Check who is recording, then block it:

```sh
adb shell dumpsys audio | sed -n '/Recording Activity/,/^$/p'
adb shell appops set --uid com.google.android.as RECORD_AUDIO ignore
```

Android undoes the block on its own, sometimes within minutes. To keep it
in place, run the `mic-block` service on the phone with
`MEDIA_MIC_BLOCK_PACKAGES=com.google.android.as` set (it needs adb on the
phone). `media doctor` reports a block that has come loose. To undo it, run
the same `appops` command with `allow`. This costs you "Now Playing" and
"Hey Google".

See also: `packages/core/src/agent_media_core/mic_block.py`.

### Why does the voice sometimes change to a different one?

In phone mode the app makes the speech itself. It asks Microsoft's online
service for the voice you chose, and if a sentence doesn't arrive in time it
uses a Google voice on the phone for that sentence. Mobile data makes this
more likely: some carriers get "too many requests" answers, which the app
retries before it gives up. If it keeps happening, look for the
`edge-voice-phone` alert under **Home → Alerts**. You can also switch the
Voice setting to server mode, so the server makes the speech.

See also: `server-contract.md` §6.4 "`GET` / `POST /speech/voice`", §6.17
"Alerts".

### Why is text bigger in the app than in the browser?

The app follows Android's own font size setting (usually under Display),
and the browser does not. At Android's 1.5× scale, "Default" text in
the app is 1.5 times the size of the same setting in a browser. Make
Android's font smaller if you want them to match.

### How do I link to a thread?

There are two forms. Either one opens the thread in place when you tap it
in the app:

```
sasonica://t/<session>
https://<server>/app/t/<session>
```

The https form opens the web app's thread page in a browser. On a phone with
the app it is an Android App Link, so it opens the app instead. Agents are
told their own thread's link at the start of a session; they use
`MEDIA_PUBLIC_URL` when it is set and the `sasonica://` form when it isn't.
Set `MEDIA_THREAD_LINK=0` to turn that off.

See also: `server-contract.md` §19, "Thread links".

## On the server

### Can I delete `~/.cache/agent-media` to free disk?

Yes. Since 18 Oct 2026 it holds only copies of things that live elsewhere or
are made again on demand: fetched books and music, rendered documents and web
clips, downloaded Matrix voice notes (a held one that has not been played yet
loses its Play, but the note is still on the Matrix server). The spoken clips
that replay needs live in the state dir, `~/.local/state/agent-media/audio`
(`$XDG_STATE_HOME`, or `MEDIA_AUDIO_DIR` to put them on a bigger disk). Don't
delete that one unless you are willing to lose replay: history points at those
files, so transcripts and search keep working but old replies can no longer be
heard.

An older install kept the clips in `~/.cache/agent-media/audio`. The first
speech after upgrading moves them across, rewrites the history rows to the new
path and leaves `~/.cache/agent-media/audio` as a link to the new folder (the
clip server for the phone serves it from there). Deleting the link is
harmless: it is made again on the next reply.

### I changed `agent-media.env` and nothing changed.

The env file only fills in variables that are **not already set**. Anything
already exported wins. A long-running tmux server is the usual culprit,
because it hands the environment it started with to every new pane. Compare
it with your file and clear the stale ones; you don't need a restart:

```sh
tmux show-environment -g | grep -E '^(MEDIA|TTS)_'
tmux set-environment -gr MEDIA_SOME_VAR
```

Check your shell's own exports too (`env | grep MEDIA_`).

### YouTube won't download on my VPS.

YouTube blocks downloads from data-centre IP addresses. agent-media works
around that by downloading on a machine with a home connection (usually your
phone) and playing the file from there. Point it at that machine with
`MEDIA_MUSIC_LOCAL_SSH`; for the rooms lane only, use
`MEDIA_MUSIC_ROOMS_FETCH_SSH` (an always-on box at home, for example).
Downloading on the server and copying the file over is not an option.

See also: `packages/core/src/agent_media_core/sinks/music_fetch.py`.

### A Codex session is stuck on "Hooks need review".

Codex stops and asks you to approve its hooks whenever `~/.codex/hooks.json`
changes. That includes the first time `media-setup profile` adds
agent-media's hook. Until someone answers, the session takes no input. The
app shows the prompt as an approval, so you can answer it there or at the
terminal. You only need to answer it once per change.

See also: `server-contract.md` §6.2, approvals and `POST /session/answer`.

### Mopidy or Iris won't start after upgrading to Mopidy 4.

There are three usual causes:

- New setuptools releases dropped `pkg_resources`, which Iris and
  Mopidy-YouTube still import. Pin `setuptools<81` in Mopidy's environment.
- Iris imports `mopidy.models.serialize`, which Mopidy 4 removed. Iris
  needs a patch or a release that supports Mopidy 4.
- Watch out for `pipx inject mopidy <pkg>`: it can quietly upgrade Mopidy
  itself, and the break only shows at the next restart. Compare `pip
  freeze` before and after.

See also: `docs/reference/music-local-and-snapcast.md`.

### Keys on the canvas do strange things (open a search, scroll).

A keyboard browser extension, such as Vimium, is catching the keys before
the canvas gets them. The canvas uses `c` (captions) and `f` (sound
effects). Add an exclusion rule in the extension for the canvas page (for
example `http://<host>:8781/*`).

## On a phone running Termux

### Speech is silent on the phone, but everything looks healthy.

Check Android's **media** volume. Speech plays into the music stream, so at
0 nothing can be heard, even though every component reports healthy.
`media doctor` flags "media volume is 0" but never changes the volume for
you.

```sh
termux-volume
```

### `cryptography` fails with "cannot locate symbol PyLong_Type".

A pip-built `cryptography` builds fine on Termux, but then Android can't
load it. Install Termux's own build and link it into the agent-media venv,
using your Python version in the paths:

```sh
pkg install python-cryptography
V=~/projects/agent-media/.venv/lib/python3.14/site-packages
SYS=$PREFIX/lib/python3.14/site-packages
rm -rf $V/cryptography $V/cryptography-*.dist-info
ln -s $SYS/cryptography $V/
ln -s $SYS/cryptography-*.dist-info $V/
```

Other native packages work the same way: get them from `pkg`, not pip.

### YouTube music fails with HTTP 403.

If the track is found but the download returns 403, the stable yt-dlp is
behind a YouTube change. Run bare `yt-dlp` on the phone to confirm. Then
install the nightly; if `~/.local/bin` comes first on your PATH, it takes
over from Termux's package:

```sh
python3 -m pip install --user --pre --upgrade yt-dlp
```

### How do I update a Termux install?

Pull the checkout, then restart the services:

```sh
git -C ~/projects/agent-media pull --ff-only
media restart-services
```

If the pull added a package or changed a dependency pin, install it too:
`uv pip install --no-deps --python .venv/bin/python -e packages/<pkg>` (the
venv has no pip). After Termux upgrades Python, run
`packages/core/scripts/termux-python-heal.sh` (`--install-hook` makes it
run on its own). After moving the checkout, run `media-setup profile`. From
the server, `media doctor --fix` pulls and restarts any host that is only
behind.

See also: `docs/reference/version-skew-monitoring.md`.
