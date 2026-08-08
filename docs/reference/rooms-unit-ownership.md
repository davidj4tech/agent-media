# Rooms units: agent-media owns them

`media-setup server` generates both rooms units into `~/.config/systemd/user`:

- `am-sinks.service` — the PipeWire null sinks (`am`, `am-music`)
- `am-snapfifo@.service` — the `parec` -> FIFO bridge, instantiated per sink

**agent-media owns both.** dotfiles no longer stows `am-sinks.service`; it is a
plain generated file, same as `agent-media-book-observer.service`.

## History: why this unit is fussy

dotfiles used to stow `am-sinks.service`, which meant the path in
`~/.config/systemd/user` was a **symlink into the dotfiles checkout**. Writing it
followed the link and rewrote the committed file, so the two owners silently
fought. On red5 (2026-07-26) a `media-setup server` run replaced the dotfiles
copy with a weaker generated one and the rooms audio was wrong for two days.

`media-setup` still refuses to write through a symlink and warns naming the link
target — a safety net for any host whose dotfiles predate this change, and for
any other unit that later ends up stowed.

## The three properties that must not be dropped

The old dotfiles copy carried three things the generated template originally
lacked. They are now in `_AM_SINKS_UNIT` and must stay:

- **`PartOf=pipewire.service`** — the null sinks live in PipeWire's runtime, so
  restarting pipewire destroys them. Without `PartOf`, this `RemainAfterExit`
  oneshot stays "active" forever while the sinks are silently gone.
- **the `am-music` sink** — music is now on by default (`--no-music` opts out).
  It used to require `--music`; a run that forgot the flag is exactly what broke
  red5.
- **the default-sink pin** (`ExecStartPost`) — so speech sent to the default
  `local` target lands on the whole-house feed rather than a local card.

`ExecStop` unloads the modules, one line per sink. It uses `$$`, systemd's escape
for a literal `$`: unescaped, systemd expands `$id` itself and hands `sh` an
empty string, so the unload silently does nothing.

## Symptom when the sinks collapse

Both Snapcast streams carry identical audio — a slight echo on every playback.
`parec --device=am-music.monitor` does not fail when that monitor is missing; it
silently falls back, so nothing logs an error. Diagnose with:

    pactl list short sinks           # expect: am AND am-music
    pactl list short source-outputs  # 2nd column is the source id

If every capture shows the **same** source id, the sinks have collapsed onto one
monitor. Fix: `systemctl --user restart am-sinks.service` then restart the
`am-snapfifo*` services.
