# sasonica-web — the fork, the chat page, the reply box

2026-09-09, out of the session that followed `2026-09-09-react-client-parity.md`.
That note is still the spec and the running order; this one says what exists.

## What exists

**The fork.** `davidj4tech/sasonica-web`, from
`audiobookshelf/audiobookshelf-client-react`, cloned at `~/projects/sasonica-web`.
`main` tracks upstream (remote `upstream` is set); `sasonica` is the staging
line and is pushed. `SASONICA.md` at the root carries the fork rule, the
endpoint table, and the build commands.

Three commits, items **1 and 2** of the parity list:

- `src/lib/sasonica/canvas.ts` — the address, the request, the shapes.
  `src/lib/sasonica/strings.ts` — the fork's English, deliberately not in
  upstream's locale files (they are translated by other people and merged
  daily; fork-only keys would make every merge a conflict).
- `src/hooks/sasonica/useConversationSession.ts` — the probe.
  `src/components/sasonica/ReplyBox.tsx` — the composer, the ghost prompt,
  dictation, "go to %pane".
- `src/hooks/sasonica/useConversationLog.ts` — the poll, the cadence, the live
  clock. `src/components/sasonica/{ConversationLog,ConversationPage}.tsx`.

The only upstream file touched is `LibraryItemClient.tsx`: 30 lines, two
imports and a branch, placed below every hook so the swap cannot change the
hook order.

Follow-along (item 3) came with the log rather than after it — the sentence
timeline is in the same payload and the same component. What is NOT there is
the Settings toggle that turns the timing readout on: `ConversationPage` takes
a `debug` prop and nothing sets it yet. That belongs with item 6.

**agent-media.** `6762352`, canvas CORS. The web client is served from the ABS
port and the canvas is on 8781, so every conversation call it makes is
cross-origin; the Capacitor app never needed this. Only the routes whose
credential is the caller's own ABS bearer are opened (`_CORS_PATHS`);
`/input`, `/show`, `/ctl`, `/say`, `/play` spend a token of ours and stay
same-origin. Refusals carry the header too, or the client shows "network
error" in place of the server's own words. `tests/test_cors.py`, 4 tests.

## Where it runs

**2026-09-17: the React client is what red5 serves.** The primary
Audiobookshelf on :13378 — the real library, the one the phone and every
agent-media service talk to — moved from advplyr's image (2.35.1, Vue client)
to `audiobookshelf/audiobookshelf-react:latest` (2.36.0) and mounts the fork's
build. The schema turned out to be current already ("No migrations to run")
and the libraries came back unchanged (39 / 6 / 55 items); a cold copy of the
2.35.1 database was taken first regardless, at
`~/backups/audiobookshelf-pre-2.36-20260917-093044`, because ABS migrations
are one way and that server had never had a backup. Auto-backup is still
disabled there — worth turning on.

:13379 is now a second server running the same client against its own
database. It has no job left unless a client needs trying somewhere safe.

Both units are committed at `deploy/quadlet/`, as copies rather than symlinks
(see the README there).

red5's canvas is restarted, so CORS is live. The React client runs on :13379
as a **Quadlet unit**, `~/.config/containers/systemd/audiobookshelf-react.container`,
the same way the primary Audiobookshelf on :13378 does — so it comes back on
its own after a reboot or a crash (`Restart=always`, `WantedBy=default.target`,
and the user has lingering on). `systemctl --user restart audiobookshelf-react`.

It mounts the built checkout over the image's own output:

```
Volume=%h/projects/sasonica-web/.next:/app/client-react/.next
Volume=%h/projects/sasonica-web/public:/app/client-react/public:ro
Volume=%h/projects/sasonica-web/next.config.ts:/app/client-react/next.config.ts:ro
Volume=%h/projects/sasonica-web/package.json:/app/client-react/package.json:ro
```

Not the whole checkout: the image is Alpine and our `node_modules` were
installed on glibc, so the native `@next/swc` and `@parcel/watcher` binaries
are the wrong libc and the server refuses to start. The image's own
`node_modules` are the right ones and our `package.json` adds no dependencies,
so mounting only the build output is enough. `podman logs` should end with
"Using React client at /app/client-react" and "Listening on port :80".

To go back to stock: comment those four Volume lines out and restart the unit.
(`audiobookshelf-react-stock`, the pre-Quadlet container parked on 2026-09-09,
is now redundant and can be removed — it holds no state, config and metadata
are host mounts.)

Neither quadlet file is under version control, the primary's included — they
live only on red5's disk. Pre-existing, but worth a decision.

**Redeploy after a change is `pnpm build` in the checkout and a container
restart** — the mount is live, but Next reads `.next` at startup.

## What is verified, and what is not

Verified: `pnpm typecheck`, `pnpm find-hardcoded-strings` (0 findings),
eslint over the changed paths, `pnpm build`, the 203 visual tests. Live: the
preflight and the header on the tailnet canvas, and `GET /conversation` +
`GET /conversation/log` for a real conversation item, called with the ABS
bearer from the client's own origin, answering with a live turn on it. The
client serves on :13379.

**Not verified: the page rendered in a browser.** It needs a logged-in ABS
session on :13379, and the only credential on this host is an API key, whose
token has no `exp` — Next's proxy treats it as expired and redirects to
/login. A throwaway preview route is not a way round it either: `pnpm dev`
standalone proxies `/status` back to itself and wedges (the README's dev loop
wants a source checkout of the ABS server beside it). So the next session
should either open it as David and look, or stand up the paired dev loop.

## Item 4 — new chat (same session, after David looked at it)

`/library/<id>/ask`, and a "New chat" entry in the side rail. Under the
library route because a conversation that gets going becomes an item in that
library and the page then navigates to it — which needs the library id.

The routing is the whole of it. Anything the server would be GUESSING is
asked first as a dry run (`dry: true` sends nothing): a fresh session commits
straight through, a guessed thread gets four seconds and a sentence naming
it, and a spoken name matching more than one conversation comes back 300 with
candidates, which become the picker with nothing sent. All three verified
against the running canvas — `how: default` → new, `how: sticky` → continued
with a title, and "reply to sasonica" → 300 with four candidates.

The last thread spoken to is per device (`sasonica.askLast` in
`localStorage`, `lib/sasonica/askLast.ts`) — it is about this screen's train
of thought, not the account.

Also in: `useDictation`, shared by the reply box and this page. And the
conversation page's height — it was measuring the viewport while the region
it gets is `.page-wrapper`, so it ran a media-player's height too tall
whenever something was streaming, which is a small scroll to reach the box
you want to type in. It mirrors both of app.css's rules now; `h-full` cannot
do it, the wrapper between it and the scroll container has no height of its
own.

## Items 5 and 6, and CI — 2026-09-17

**Live shelf.** agent-media tags an item `live` while its session is up, so
the shelf is a plain Audiobookshelf filter (`tags.bGl2ZQ==`) and needs no
canvas. First on the library home page, null when nothing is live, polled
while the tab is visible. The same green dot rides on live cards, wrapped
around upstream's badges rather than woven in — theirs come and go with hover,
the dot should not. Session controls (go to terminal, close, resume) are in
the chat page's title row, not the item page's menu, because a conversation
replaces that page outright.

**Settings** live on the account page and belong to the device, not the
account: the canvas address (blank means this server on 8781, and the box
shows what blank resolves to) and the follow-along timing readout — which had
been wired through the conversation page since day one with nothing able to
turn it on.

**"Series" is "Projects"** on the conversations library — the nav, the shelf
title, the filter menu, the search heading. Detection is the library's name,
the same rule and spelling as the phone app's
`getCurrentLibraryIsConversations`, so the two clients cannot disagree.
Rename the library and `lib/sasonica/conversations.ts` is the one thing to
change. The interface stays shared: this only chooses a word where the word is
displayed, which is what upstream already does for podcast libraries.

**CI.** `.github/workflows/sasonica-client.yml` in the fork builds on every
push to `sasonica` and uploads `.next` + `public` as `sasonica-client`;
`deploy/pull-client-build.sh` here fetches the newest successful one, replaces
the checkout's build and restarts Audiobookshelf. red5 has not built the
client since. Two things learnt the hard way, both now guarded:

- `upload-artifact` skips dot-directories, so the first artifact was
  `public/` alone with no build in it. `include-hidden-files: true`.
- `.next/cache` is the build cache, not the build, and four fifths of the
  size (346M → 71M). Dropped before upload.

CI earned itself on its first run, catching two missing `isConversations`
dependencies that would have left the filter menu with whichever word it was
built with.

## Where it stands

The parity list is done — all six items. David has used the client on his
phone and says it looks good, which closes the "nobody has looked at it" item
that stood through most of this work; the "Series"/"Projects" wording was his
finding, from using it.

Upstream's only scars are hook-sized: the item page's branch to the chat page,
a nav entry, four label swaps, one optional argument on
`searchResultsToShelves`, one shelf id in `types/api.ts`, and the dependency
arrays that go with them. The fork merged 59 upstream commits on 2026-09-17
with no conflicts.

Next, if it is wanted: nothing on the parity list. The ideas that surfaced and
were not taken — the three `.mka` audiobooks whose files are gone from disk,
`~/.local/share/audiobookshelf-react/` (22M) left behind by the retired test
bed, and Audiobookshelf's auto-backup keeping only 2.
