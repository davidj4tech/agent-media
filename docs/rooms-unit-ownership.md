# Rooms units: who owns `am-sinks.service`

`media-setup server` generates two systemd user units into `~/.config/systemd/user`:

| unit | owner |
|---|---|
| `am-snapfifo@.service` | agent-media (plain file, regenerated freely) |
| `am-sinks.service`     | **dotfiles**, on hosts that stow it |

On stow-managed hosts (red5, mel) `~/.config/systemd/user/am-sinks.service` is a
**symlink into the dotfiles checkout**. Writing it therefore follows the link and
rewrites the *committed* file in the repo — the breakage is invisible until
someone reads `git status` in `~/dotfiles`.

`media-setup` refuses to write through a symlink and prints a warning naming the
link target. The dotfiles copy wins; agent-media does not update that unit on
those hosts.

## Why the dotfiles copy is the better one

It carries three things the generated template does not:

- `PartOf=pipewire.service` — null sinks live in PipeWire's runtime, so a
  `pipewire` restart destroys them. Without `PartOf` the `RemainAfterExit`
  oneshot stays "active" forever while the sinks are silently gone.
- the **`am-music`** sink — the generator only emits it when `--music` is passed
  (`sinks = [ROOMS_SPEECH_SINK] + ([ROOMS_MUSIC_SINK] if args.music else [])`).
- the default-sink pin, so speech on the default `local` target lands on the
  whole-house feed.

## Symptom when the sinks collapse

Both Snapcast streams carry identical audio — a slight echo on every playback.
`parec --device=am-music.monitor` does not fail when that monitor is missing; it
silently falls back, so nothing logs an error. Diagnose with:

    pactl list short sinks           # expect: am AND am-music
    pactl list short source-outputs  # 2nd column is the source id

If every capture shows the **same** source id, the sinks have collapsed onto one
monitor. Fix by restoring the unit and restarting `am-sinks` plus the
`am-snapfifo*` services.

Occurred on red5 2026-07-26 → 2026-07-28: a `media-setup server` run without
`--music` clobbered the stowed unit, dropping `am-music` for two days.

## If ownership should move to agent-media

The precedent is `agent-media-book-observer.service` (see
`dotfiles/ansible/roles/media/tasks/main.yml`), which agent-media generates and
dotfiles no longer stows. Doing the same for `am-sinks.service` requires first
porting the three properties above into `_AM_SINKS_UNIT` — otherwise handing it
over makes the 2026-07-26 regression permanent.
