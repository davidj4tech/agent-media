# agent-media-core

Core library for agent-media. Event-driven pipeline:

```
intake/  ─►  route/  ─►  render/  ─►  sinks/
                  ▲                       │
                  └─────  state/  ◄───────┘
```

## Modules

- `intake/` — event sources: Claude Code hook, Codex hook, HA-SSE, Matrix, CLI.
- `route/` — coordinator: content-type-aware interruption (duck vs pause-and-resume), MPRIS browser pause, remote host MPRIS via SSH.
- `render/` — text → audio. Engines: edge (Edge TTS), openai, qwen, realtime.
- `sinks/` — `sink-speech` (mpv broker), `sink-music` (Mopidy/MPD).
- `state/` — SQLite-backed now-playing, history, errors.

---

## Pipeline

1. An intake source (e.g. Claude Code Stop hook) calls `submit_event(event)`.
2. `submit_event` resolves render engine/voice, starts remote MPRIS pause in a background thread, then calls `render_text` to produce an audio clip.
3. Once the clip is ready, `coordinator.before_speech()` waits for the remote pause to finish, then handles local MPRIS + Mopidy interruption.
4. The clip plays via `sink-speech` (mpv IPC broker).
5. `coordinator.after_speech()` resumes everything that was paused/ducked.

---

## Configuration

All settings are read from environment variables. Put them in
`~/.config/agent-audio-relay.env` (sourced by every hook and the `media`
CLI on startup).

### TTS / render

| Variable | Default | Description |
|---|---|---|
| `MEDIA_RENDER_ENGINE` | `edge` | TTS engine: `edge`, `openai`, `qwen`, `realtime` |
| `MEDIA_RENDER_VOICE` | *(engine default)* | Voice name for the engine |
| `OPENAI_API_KEY` | — | Required for `openai` / `realtime` engines |
| `CLAUDE_TTS_ENABLED` | `1` | Set to `0` to silence all TTS |
| `MEDIA_HOOK_ENABLED` | `1` | Set to `0` to disable the Claude Code hook |

### Speech routing (sink-speech)

| Variable | Default | Description |
|---|---|---|
| `MEDIA_SPEECH_DEFAULT_TARGET` | `local` | Where clips play: `local` or `rooms` |
| `MEDIA_SPEECH_LOCAL_DEVICE` | *(broker default)* | mpv `audio-device` for the `local` target (e.g. `pulse/am`) |
| `MEDIA_ROOMS_SINK` | `am` | PulseAudio/PipeWire sink name for the `rooms` target (fed into Snapcast) |
| `MEDIA_SPEECH_SOCKET_<TARGET>` | *(XDG state dir)* | Override IPC socket path per target |

### Music interruption (coordinator)

When TTS fires, the coordinator checks what Mopidy is playing and applies
a strategy based on content type:

- **Podcast / audiobook / speech** → pause-and-resume (with a configurable
  lead-in rewind so the listener doesn't miss a word)
- **Music / stream / unknown** → duck to a lower volume, then restore

| Variable | Default | Description |
|---|---|---|
| `MEDIA_DUCK_VOLUME` | *(per-policy)* | Override duck level (0-100) for all content types |
| `AAR_MOPIDY_DUCK_VOLUME` | — | Legacy alias for `MEDIA_DUCK_VOLUME` |

### Music sink — YouTube via Mopidy-Mpv

Shared YouTube on the music sink is routed through the [Mopidy-Mpv]
backend (the `mpv:` URI scheme — mpv + yt-dlp) instead of
Mopidy-YouTube/GStreamer: more reliable resolution, and the duck-for-speech
bridge below works. Single videos are rewritten to `mpv:`; YouTube
*playlists* are enumerated with `yt-dlp --flat-playlist` and queued as one
`mpv:` track each (auto-advances, ducks). Everything else (local library,
search, plain http(s) streams, non-YouTube) stays on GStreamer. Requires the
Mopidy-Mpv backend + its idle mpv (`mopidy-mpv.service`) to be running.

| Variable | Default | Description |
|---|---|---|
| `MEDIA_MUSIC_MPV_SOCKET` | `$XDG_RUNTIME_DIR/mopidy-mpv.sock` | mpv JSON-IPC socket for the Mopidy-Mpv backend; used to mirror `duck`/`unduck` volume onto mpv (MPD `setvol` can't reach mpv's output) |
| `MEDIA_MUSIC_PLAYLIST_MAX` | `50` | Max tracks to pull when expanding a YouTube playlist; the rest are dropped (logged) |
| `MEDIA_YTDLP_BIN` | `yt-dlp` | yt-dlp binary used to enumerate playlists (reads `~/.config/yt-dlp/config`) |

### MPRIS browser/media pause

The coordinator pauses any MPRIS-registered player (Chrome, VLC, etc.)
around TTS. Uses `playerctl` — install it with your package manager.

| Variable | Default | Description |
|---|---|---|
| `MEDIA_MPRIS_PAUSE` | `1` | Set to `0` to disable MPRIS pause entirely |
| `MEDIA_MPRIS_SSH_HOSTS` | — | Comma-separated list of remote hosts to also pause via SSH (e.g. `sp4r` or `sp4r,tablet`) |
| `MEDIA_ANDROID_PAUSE_HOSTS` | — | Comma-separated list of Android phone hosts (Termux + sshd) to pause via a media-button intent — see *Android pause* below |
| `MEDIA_ANDROID_PAUSE_CMD` | `am broadcast -a android.intent.action.MEDIA_BUTTON --ei android.intent.extra.KEY_EVENT 85` | Override the command sent over SSH for play/pause — use this if `am broadcast` doesn't reach your apps and you need `input keyevent`, `termux-keyevent`, ADB, etc. |
| `MEDIA_MPRIS_OWN_MARKERS` | — | Comma-separated extra cmdline markers identifying *our own* mpv brokers, so they're never paused — see below. Adds to the built-in set rather than replacing it |

#### Not pausing ourselves

Where `mpv-mpris` is installed, our own mpv brokers (speech, book, music) are
MPRIS players too — and to this sweep they look like any other non-Mopidy
player, so it could pause the very clip it's making room for.

They can't be told apart by name: `mpv-mpris` uses the `--audio-client-name` as
its bus-name suffix when one is set, and a *random* string when not. So the
coordinator resolves each mpv player through the bus to its owner PID and reads
`/proc/<pid>/cmdline`, which still carries the IPC socket path and client name
it was launched with.

This **fails closed** — an mpv it can't identify is left alone. The two failure
modes aren't symmetric: mistaking our own broker for a stranger cuts off speech
mid-sentence, while mistaking a stranger for ours merely leaves some audio
playing under the clip.

Mopidy is excluded by name (case-insensitively — Mopidy-MPRIS publishes
lowercase `mopidy`), since the coordinator already handles it over MPD.

#### Remote MPRIS (cross-host)

If TTS originates on one machine (e.g. a headless mel) but browser media
plays on another (e.g. sp4r), set `MEDIA_MPRIS_SSH_HOSTS=sp4r` on the
originating host. The coordinator will SSH to each listed host and pause
their MPRIS players before speech starts, then resume them after.

Requirements:
- `playerctl` installed on every remote host
- Passwordless SSH from the originating host to each remote (key auth)
- The remote user's D-Bus session bus at the standard path
  (`/run/user/<uid>/bus`)

**Timing:** on cold start the SSH connection takes ~5 seconds to establish.
The coordinator starts the SSH pause in a background thread *before* the
TTS clip is rendered, so the two overlap. The audio will be briefly delayed
on the very first message after the connection goes cold (after 5 minutes of
silence). Subsequent messages within 5 minutes reuse the SSH ControlMaster
and are instant.

**Chromium / Chrome:** handled correctly despite Chromium unregistering its
MPRIS interface when paused and re-registering with a new instance number on
resume. The coordinator uses base-name prefix matching to find the new
instance.

#### Android pause

Android doesn't expose MPRIS, so phones use a different SSH-based path:
set `MEDIA_ANDROID_PAUSE_HOSTS=phone1,phone2` and the coordinator will SSH
into each, query `dumpsys media_session` for an active playing session,
and if so, broadcast a `KEYCODE_MEDIA_PLAY_PAUSE` intent that most music
apps (Spotify, YouTube Music, Pocket Casts, etc.) listen for. After speech
finishes, a second media-button broadcast resumes playback.

Caveats:
- Android exposes only a toggle, not explicit pause/resume — we check
  state before pausing to avoid accidentally starting playback.
- `am broadcast` works in Termux for media-button intents. If your phone
  needs a different mechanism (root + `input keyevent 85`, `termux-keyevent`
  via `termux-api`, ADB over Wi-Fi, Shizuku, etc.) override the command
  with `MEDIA_ANDROID_PAUSE_CMD`.
- Both `MEDIA_MPRIS_SSH_HOSTS` and `MEDIA_ANDROID_PAUSE_HOSTS` can be
  active for different remote hosts in the same response.

### Text highlight

When a clip starts playing, the source tmux pane automatically enters
copy-mode and jumps to the spoken text so you can read along while
listening. Press `q` to exit copy-mode at any time; playback is unaffected.

The `v` key in the control popup (`prefix + a`) re-triggers the same
jump on demand.

| Variable | Default | Description |
|---|---|---|
| `MEDIA_AUTO_HIGHLIGHT` | `1` | Set to `0` to disable auto copy-mode jump |

### Notifications (Claude Code hook)

| Variable | Default | Description |
|---|---|---|
| `MEDIA_NOTIF_LABEL` | `1` | Set to `0` to disable the "hostname / session / pane" prefix on notifications |
| `MEDIA_NOTIF_LABEL_HOST` | `1` | Set to `0` to omit the hostname from the label (useful on single-machine setups) |
| `MEDIA_NOTIF_FOCUS_SUPPRESS` | `180` | Suppress "waiting" notifications if the user was active in tmux within this many seconds. Set to `0` to disable. |

### Per-session voices (Claude Code hook)

Gives each tmux session its own speech voice, so concurrent Claude Code
sessions are audibly distinguishable. The voice is resolved from the tmux
session name and rides on the event to the sink — no daemon change. Resolution
order: explicit pin → stable hash of the session name → `MEDIA_RENDER_VOICE`.

| Variable | Default | Description |
|---|---|---|
| `MEDIA_SESSION_VOICE_ENABLED` | `1` | Set to `0` to disable per-session voices (falls back to `MEDIA_RENDER_VOICE`). Also inactive when not running inside tmux. |
| `MEDIA_SESSION_VOICE_MAP` | *(none)* | Explicit pins as `name=voice,name=voice` (e.g. `sasonica=en-IE-EmilyNeural,main=en-AU-NatashaNeural`). First exact session-name match wins. |
| `MEDIA_SESSION_VOICE_POOL` | *(built-in)* | Comma-separated voices for the stable-hash fallback. Overrides the built-in distinguishable-accent pool (AU/NZ/GB/IE/CA/ZA/GB-young/IN). Sessions with no explicit pin get `pool[ sha1(session_name) % len(pool) ]`. |

---

## Services

`media-setup` (bundled CLI) installs and wires up all services. It
auto-detects the init system (runit on Termux / host-runit, systemd
`--user` on regular Linux).

Manually, the key services are:

### `sink-speech` (mpv broker)

A long-running `mpv --idle=yes --input-ipc-server=<socket>` process. The
`media` CLI and all hooks talk to it over the socket.

```sh
# systemd user service (Linux)
systemctl --user start agent-media-sink-speech

# Termux runit
sv start agent-media-sink-speech
```

Socket path: `$XDG_STATE_HOME/agent-media/sink-speech.sock`
(default: `~/.local/state/agent-media/sink-speech.sock`)

### `sink-book` (mpv broker)

The book channel's own long-running mpv. Unlike `sink-speech` it has no `run`
script — the unit execs `mpv` directly — so it isn't generated by
`media-setup install-services`. The unit is kept in the repo and copied into
place:

```sh
cp systemd/agent-media-sink-book.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-media-sink-book
```

Socket path: `$XDG_STATE_HOME/agent-media/sink-book.sock`

Note the `--no-config`: it's what keeps a stray `~/.config/mpv` out of the
broker, but it also switches off mpv's script directories, so anything this
broker needs has to be named with an explicit `--script=` (that's why
`mpris.so` is listed, where the other brokers pick it up automatically from
`/etc/mpv/scripts`). `sinks/book.py`'s autospawn path builds the same argv and
must be kept in step with this unit.

### Play history

```sh
media recent                  # music, book and speech, newest first
media recent 50 --channel music
media recent --lines          # display<TAB>uri, for a picker
```

The `history` table always had a `sink` column and only the speech lanes ever
wrote to it. Music and book now write there too, from `set_music_intent` and
`set_book_last` — the two places each channel already remembered what it was
playing, so a new play path cannot forget to record itself. Rows de-duplicate
against the newest row for that channel, so a re-play leaves one row and
coming back to something after playing anything else leaves two.

**One row per item someone deliberately put on.** A queue that auto-advances
through forty tracks is one row, not forty; capturing those needs a poller
watching the renderer (what `book_observer` does for the book channel), which
is a separate feature.

This is also where the music channel's memory lives, because `music stop`
clears the intent key — so before this, stop then resume was silence with no
explanation. `media music resume` (and MCP `music_resume`) now reopen the last
thing played when nothing is loaded, the way `book resume` always has. An
unreadable backend counts as "not idle", so a transport key never starts music
just because Mopidy was unreachable for one probe.

### `media-share` (shared links)

The doorway for a link that arrives from somewhere else — in practice the
Android share sheet, via the companion app's "Play with agent-media" entry.

```sh
media share 'https://youtu.be/…'          # classify and play
media share --dry-run 'https://…'         # say what it would do
media share --channel book 'https://…'    # override the choice
```

The channel is chosen from `yt-dlp` metadata, not from the URL, because the
difference that matters is behavioural: **music ducks** under a spoken clip and
**longform pauses and rewinds**. A three-hour lecture and a three-minute track
are the same shape of link and want opposite treatment. Rules (all in
`share.classify`, all tested): a podcast/audiobook/music host settles it
outright; a live stream is ambient; a `- Topic`/VEVO channel is music at any
length; otherwise anything at or past `MEDIA_SHARE_LONGFORM_S` (default 1800)
goes to the book channel, and short-form stays on music. A probe that fails —
no network, no yt-dlp, a bot-block — falls back to music rather than refusing.

The service is the on-device half: a loopback listener on `127.0.0.1:8771`
(`MEDIA_SHARE_PORT`) that the app POSTs to, because no other app on the phone
can run `media`. It replies with the verdict as soon as it has one and does the
acquisition on a background thread — a shared DJ set is a long download, and
the toast should not wait for it. `requires: observe`, so it installs on
handheld hosts and not on speakers. See
[`android/companion/README.md`](../../android/companion/README.md).

### `media ask` (the conversation that has been talking to you)

The popup's `a`, and the one control on the phone that is not transport. It
puts a question to the conversation whose replies you have been listening to,
and the answer arrives the way every reply does — spoken, minutes later.

```sh
media ask --status                        # who would be asked, and are they there
media ask 'who wrote this?'               # ask, with the listening context
media ask --channel book 'what chapter?'  # prepend a different channel's context
media ask --dry-run 'why?'                # print the line that would be typed
```

**There is no conversation table, and nothing is created.** Every speech row
already carries `source_session` and `source_pane` in its extras, and
`StateStore.session_for_pane` already exists to answer "which conversation is
this pane". Speech history *is* the thread. So this resolves the newest turn
that belongs to a conversation — rows with no session are cron, not anybody's
conversation — and types into the pane that turn came from.

Liveness is three conditions, kept separate because "not live" covers three
different situations and only one of them is worth retrying:

| | |
|---|---|
| the pane is gone | the session ended |
| the pane is someone else's now | tmux recycled it — one observed pane had carried twelve conversations |
| nothing said for `LIVE_S` (30m) | calling it ongoing would be a fiction |

The check runs **session → pane**, never pane → session. The second direction
is a documented heuristic; the first is verified by asking `session_for_pane`
and requiring the same answer back, so a recycled pane refuses rather than
receiving somebody else's mail.

Delivery is verified against the session's **own transcript**, because
`tmux send-keys Enter` reports that the key reached the pane, not that Claude
Code accepted it — a still-initialising TUI swallows text and Enter without
trace. Transcripts are named `<session>.jsonl`, so knowing the session makes
this a lookup rather than a search. Exit codes say which happened: `0` asked,
`3` no live conversation, `4` typed but not accepted.

The line is always **one line** (a literal newline submits) and always tagged
`[media ask]`, so the session knows the question came from somewhere else and
its answer has to be spoken rather than left on a screen.

When nothing is listening it **starts one**, in a window named for what is
playing — `ask Kind of Blue`. That name is the whole mechanism: tmux's window
name is what the speech hook records as `source_window`, which is what a
conversation's label is read back from, so the moment the new session answers
it becomes findable like any other and the next question about the same album
lands in it rather than beside it. `open-pi`'s fresh window never was. Before
the first answer arrives there is nothing in the history to find, so the name
is looked up in tmux too — otherwise two questions a minute apart would open
two windows. `--no-new` keeps the old refusal for a caller that only wants to
extend. The launcher is `MEDIA_ASK_CMD` (default `claude`, and that is
load-bearing: the session tags this all turns on are written by the agent-media
hook inside a Claude Code session).

On the phone this is `GET /ask` (who would be asked) and `POST /ask` (the
question) on `media-share`, and both are run **on the origin** over ssh: a
conversation is a pane on the hub and a transcript beside it, and a render host
has neither.

### Claude Code hook

Wire in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command",
                         "command": "media-hook-claude-code",
                         "timeout": 30}]}],
    "Notification": [{"hooks": [{"type": "command",
                                  "command": "media-hook-claude-code",
                                  "timeout": 30}]}]
  }
}
```

The hook reads the transcript, extracts the latest assistant text, deduplicates
it against recent history, and submits it to the pipeline.

---

## tmux integration

Source `media.tmux` from your `tmux.conf.local` for the control popup and
status bar:

```tmux
# In tmux.conf.local (after oh-my-tmux loads):
source-file ~/.local/share/agent-media/media.tmux

# Add to status-right for live TTS progress (oh-my-tmux):
# tmux_conf_theme_status_right="... #(media status 2>/dev/null) ..."
```

`prefix + a` opens the control popup (play/pause, seek, volume, speed,
view spoken text). `media status` prints a compact progress bar for the
status line.

### `media status --now-playing`

Appends what the **music or book** channel is playing to the **speech** line,
so one process renders the whole status-bar segment:

```tmux
tmux_conf_theme_status_right="... #(MEDIA_STATUS_PANE=#{pane_id} media status --title #{client_width} --now-playing 2>/dev/null) ..."
```

Speech and now-playing answer different questions and never duplicate each
other; both collapse to empty when idle, so a quiet bar stays quiet. A book
outranks music, on the grounds that music under a book is the bed rather than
the subject. Long titles scroll (`MEDIA_STATUS_MARQUEE_CPS`, default 1 col/s),
and the segment sizes itself from the client width
(`MEDIA_STATUS_NOW_PLAYING_{MIN,MAX}`).

Two things make this cheap enough to run once a second in every pane, both of
which were the opposite before:

- **Local timeline first.** `media status` uses the announced timeline when the
  submit process recorded one, instead of asking the phone's broker (~2s on
  that link). It is an optimisation, not a substitute: the `remote-say` path
  records no duration on purpose — nothing local measures audio played on
  another device — so there the far side is still asked, because it is the only
  thing that knows the utterance is running. `MEDIA_STATUS_NO_REMOTE=1` refuses
  the round trip outright, which is fast and, on that path, blind.
- **No service layer.** It reads the mpv sockets directly. The natural-looking
  `book_now_playing()` costs ~2.6s because it reasons about remote targets, and
  building the service module alone is ~0.6s.

Net: ~0.1s per redraw, against ~3s for the speech line plus an MPRIS-based
now-playing plugin doing the same job in two processes.

MPRIS (via `mpv-mpris` and Mopidy-MPRIS) stays published for *outside*
consumers — `playerctl`, a phone lock screen, a status plugin. Nothing inside
agent-media reads its own state back through it.

---

## Snapcast (whole-house audio)

For multi-room setups, TTS and music route through Snapcast:

1. PipeWire null sinks (`am`, `am-music`) receive audio from mpv and Mopidy.
2. `parec` reads from the sink monitors and writes into named FIFOs
   (`/tmp/snapfifo-am`, `/tmp/snapfifo-am-music`).
3. `snapserver` reads the FIFOs and streams to all `snapclient` instances.

Key constraint: `snapserver` and `parec` must run as the **same user** or
the FIFO write will fail with ENXIO. Override snapserver's user in
`/etc/systemd/system/snapserver.service.d/override.conf`:

```ini
[Service]
User=<your-username>
Group=<your-username>
Environment=XDG_RUNTIME_DIR=/run/user/<uid>
```

Pre-create the FIFOs at boot via `/etc/tmpfiles.d/snapfifo.conf`:

```
p /tmp/snapfifo-am      0662 <user> <user> -
p /tmp/snapfifo-am-music 0662 <user> <user> -
```
