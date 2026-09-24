# agent-media-notes-paragtd

The [paragtd](https://github.com/davidj4tech/paragtd) layout for agent-media's
Notes tab. Without it, Notes reads plain Org: every top-level `.org` file is a
view, and captures go to `inbox.org`. With it installed, and a tree that looks
like paragtd's (a `next-actions.org` at the top), Notes shows the GTD views:
Inbox, Next actions, Waiting for, Tickler and the rest. Refiling files a
heading the way paragtd's capture templates do, and past astro alerts drop off
the agenda.

```sh
pip install -e packages/notes-paragtd
```

It registers under the `agent_media.notes_profiles` entry point. To choose a
profile rather than rely on detection, set `[notes] profile = "paragtd"` (or
`"none"`) in `~/.config/agent-media/config.toml`, or `MEDIA_NOTES_PROFILE`.

When the tree has a `.paragtd.json` (paragtd writes it from Emacs; see
paragtd's README, "Outside Emacs"), the file list, TODO keywords and astro
settings come from it, so a site's own keywords reach the phone. Without one,
paragtd's defaults are used. Closing a step of a sequenced project runs
paragtd's next-step trigger (`sequence.py`), and Org's dependency blocking is
on, as `paragtd-setup` turns it on in Emacs.

paragtd itself is GPL-3.0 and is never imported. This package reads the files
paragtd writes and runs its commands as separate processes.
See `docs/proposals/2026-09-24-notes-core-and-paragtd.md`.
