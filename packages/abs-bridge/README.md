# agent-media-abs-bridge

Audiobookshelf beside the book channel, in both directions.

**`media-abs-book-bridge`** — while a book plays out to the rooms through mpv,
push its position to ABS, so the phone and web app show the right resume point.
Optionally pull the other way (`ABS_PULL_ON_LOAD=1`): start on the phone, send
it to the rooms, and carry on where you were.

**`media-abs-cast-watcher`** — press play in the ABS app and the rooms pick it
up: detect a session whose position is genuinely advancing, start the same file
on the book channel at the live position, then close the ABS session so the
client stops.

Optional, and nothing in agent-media depends on it. Configure in
`~/.config/agent-media/abs-bridge.env`:

```
ABS_URL=http://127.0.0.1:13378
ABS_TOKEN=…            # ABS → Settings → API Keys
ABS_PULL_ON_LOAD=1     # optional
```

Without `ABS_TOKEN` both daemons log why and exit cleanly, and
`media-setup install-services` skips them on a host with no config file at all.

Installed like the rest: `media-setup install-services abs-book-bridge
abs-cast-watcher --now`.

## History

Both ran for months as untracked files in `~/.local/bin` on one machine — no
version control, no tests, and outside `media doctor`'s health checks, which
only see units named `agent-media-*`. Moving them here was the fix. The cast
watcher also shelled out to a `book-abs` helper that no longer exists anywhere;
it now calls `media book play --start-ms`, resolving the ABS item to a local
path itself.
