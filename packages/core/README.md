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
