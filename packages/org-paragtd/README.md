# agent-media-org-paragtd

The [paragtd](https://github.com/davidj4tech/paragtd) layout for agent-media's
Notes tab. Without it, Notes reads plain Org: every top-level `.org` file is a
view, and captures go to `inbox.org`. With it installed, and a tree that looks
like paragtd's (a `next-actions.org` at the top), Notes shows the GTD views:
Inbox, Next actions, Waiting for, Tickler and the rest. Refiling files a
heading the way paragtd's capture templates do, and past astro alerts drop off
the agenda.

```sh
pip install -e packages/org-paragtd
```

It registers under the `agent_media.org_profiles` entry point. To choose a
profile rather than rely on detection, set `[org] profile = "paragtd"` (or
`"none"`) in `~/.config/agent-media/config.toml`, or `MEDIA_ORG_PROFILE`.

When the tree has a `.paragtd.json` (paragtd writes it from Emacs; see
paragtd's README, "Outside Emacs"), the file list, TODO keywords and astro
settings come from it, so a site's own keywords reach the phone. Without one,
paragtd's defaults are used. Closing a step of a sequenced project runs
paragtd's next-step trigger (`sequence.py`), and Org's dependency blocking is
on, as `paragtd-setup` turns it on in Emacs.
See `docs/proposals/2026-09-24-notes-core-and-paragtd.md`.

## Traps

- **Install before you restart.** The server looks up
  `agent_media.org_profiles` entry points once per process and caches the
  result; a profile that is missing or fails to import is logged and
  skipped. So install (or reinstall) this package into the server's venv
  *before* restarting the canvas or server, or the Organiser quietly comes
  back as plain Org. Entry points need a real install: a source tree on
  `PYTHONPATH` alone is not seen, so a worktree that tests this needs its own
  venv with the package installed.
- **`pkill -f` can kill the shell running it.** Inside a scripted shell
  (an agent's Bash call, a test runner) `pkill -f "<pattern>"` also matches
  that shell's own command line. Stop a test server by pid or with
  `fuser -k <port>/tcp`.

## Licence

paragtd itself is GPL-3.0 and is never imported. This package reads the files
paragtd writes and runs its commands as separate processes. This package is
Apache-2.0 (docs/decisions/2026-09-22-licensing.md).
