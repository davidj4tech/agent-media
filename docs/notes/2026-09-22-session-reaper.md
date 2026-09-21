# The idle-session reaper: installing it, and switching it on

Status: built, not installed
Date: 2026-09-22

`media session-reap` closes agent sessions nobody has spoken in for 12 hours
(6 when the host is short of memory), writes a recap for each first, and marks
it **rested** so the app can tell it from a thread you ended. The rules are in
`packages/server/src/agent_media_server/reap.py`'s docstring and in
`server-contract.md` ("Resting"). This note is only how to run it.

## Try it by hand

```sh
media session-reap --dry-run --no-log     # what it would do now; touches nothing
media session-reap --dry-run --json       # the same, as one JSON object
```

Every line is one live agent session: `would-close` with `why=idle|idle-tight`
and whether a recap is already there, or `kept` with the reasons (`pinned`,
`caller`, `speech-live`, `working`, `approval`, `pane-draft`, `server-draft`,
`unrecognised`, `no-last-message`, `recent`). Run from inside an agent's pane,
that session is always `caller`.

## Install the timer (dry run)

On the desk host (it has the `origin` role):

```sh
media-setup install-services session-reap --dry-run   # see the unit and timer
media-setup install-services session-reap --now       # write and enable them
```

That writes `agent-media-session-reap.service` (oneshot) and
`agent-media-session-reap.timer` (`OnCalendar=*:0/15`) to
`~/.config/systemd/user/` from `packages/core/services/session-reap/`, and
enables the timer. A bare `media-setup install-services` (every template)
installs it too, on a host with the `origin` role. It stays a dry run: each pass appends its decisions to
`~/.local/state/agent-media/session-reap.log` and prints them to the journal:

```sh
tail -f ~/.local/state/agent-media/session-reap.log
journalctl --user -u agent-media-session-reap -n 40
```

## Switch it to apply

Once the log has looked right for a day or two, add to
`~/.config/agent-media.env`:

```sh
MEDIA_REAP_MODE=apply
```

No restart: the next pass reads it. To go back, remove the line (or set
`dry-run`). `media session-reap --apply` runs one real pass by hand.

Tunables, same file: `MEDIA_REAP_IDLE_H` (12), `MEDIA_REAP_TIGHT_IDLE_H` (6),
`MEDIA_REAP_TIGHT_PCT` (20), `MEDIA_REAP_TIGHT_MB` (1500), `MEDIA_REAP_DRAFT_H`
(6), `MEDIA_REAP_RECAP=0` to skip recaps, `MEDIA_REAP_RECAP_MODEL` /
`MEDIA_REAP_RECAP_TIMEOUT` (default: `MEDIA_FOLLOWUP_MODEL`, 20 s).

## Keeping one open

Pin it from the app (`POST /session/pin`). Pins live in
`~/.local/state/agent-media/pinned.json`.

## Old archive marks

Before the server kept the archive flag, archiving was an `archived` tag on the
Audiobookshelf item. Carry those over once:

```sh
media session-archive-import           # what it would mark
media session-archive-import --apply   # mark them
```

It only reads from ABS.
