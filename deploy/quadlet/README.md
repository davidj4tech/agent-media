# Quadlet units — the Audiobookshelf servers

Podman [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
container units, run by the user's systemd. They live on red5 at
`~/.config/containers/systemd/`; these are the copies under version control.

| unit | port | what it is |
| --- | --- | --- |
| `audiobookshelf.container` | 13378 | Audiobookshelf: the real library, the one the phone and every agent-media service talk to. |

It serves **the React client**, mounting the build from
`~/projects/sasonica-web` (branch `sasonica`) over the image's own — the
conversation page, the reply box and new chat. Only the build output is
mounted, never the checkout: the image is Alpine and red5's `node_modules`
are glibc, so the native binaries are the wrong libc and the server will not
start. The reasoning is written out in the unit file itself.

Redeploy after a client change:

```
cd ~/projects/sasonica-web && pnpm build
systemctl --user restart audiobookshelf
```

## Installing / checking

These are copies, not symlinks, so they can drift from what is running. To
check, and to install a change:

```
diff -u ~/.config/containers/systemd/audiobookshelf.container deploy/quadlet/audiobookshelf.container
cp deploy/quadlet/*.container ~/.config/containers/systemd/
systemctl --user daemon-reload && systemctl --user restart audiobookshelf
```

(Symlinking the live paths into this checkout would remove the drift, at the
cost of making the real Audiobookshelf depend on a project checkout being
present at boot. Not done for that reason.)

## History

- **2026-09-17** — the primary moved from `advplyr/audiobookshelf:latest`
  (2.35.1, Vue client) to `audiobookshelf/audiobookshelf-react:latest`
  (2.36.0). Audiobookshelf's migrations are one way, so a cold copy of the
  2.35.1 database was taken first, with the server stopped:
  `~/backups/audiobookshelf-pre-2.36-20260917-093044`. In the event the
  schema was already current — "No migrations to run" — and the libraries
  came back unchanged (39 / 6 / 55 items, one user). Nightly auto-backups
  were turned on the same day (01:30, keeping 2, into `/metadata/backups`) —
  that server had never had one.
- **2026-09-17** — `audiobookshelf-react.container` on :13379 removed. It was
  a second server with its own database, for trying a client against without
  touching the real one; now that the real one serves the same client it had
  no job left, and having two logins that look identical but hold different
  libraries was a trap (its database only ever had the Conversations library,
  so landing on it looked like the audiobooks had vanished). `ABS_SERVERS`
  and `ABS_URLS` in `~/.config/agent-media/abs-bridge.env` pointed at it and
  are commented out. Its data is still at
  `~/.local/share/audiobookshelf-react/` (22M) and can be deleted.
