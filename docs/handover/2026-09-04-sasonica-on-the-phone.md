# Sasonica on the phone

Written 2026-09-04, at the end of a long session. The job for the next one:
**get Sasonica itself onto p8a** — the fork, as David's own client — rather
than the one-patch debug build we sideloaded to test a PR.

## What Sasonica is

David's fork of the Audiobookshelf Android app, renamed from
`audiobookshelf-app` on 2026-09-04.

- Repo: **github.com/davidj4tech/Sasonica** (still a fork of advplyr/audiobookshelf-app)
- Working copy: **`~/projects/sasonica`** — `origin` = the fork, `upstream` = advplyr
- Open upstream PR from it: **advplyr/audiobookshelf-app#2010**, branch
  `feat/update-outdated-download` — offers the download button again when a
  downloaded copy is behind the server. Device-tested, ready for review,
  waiting on the maintainer. Nothing to do unless they comment.

Why the fork exists at all is a standing direction, not a task: play a
conversation's clips natively and eventually absorb the companion app. The
**server half of that is answered and needs no fork** — see
`docs/proposals/2026-09-02-growing-item-experiment.md`.

## Read these before touching anything

Four facts that each cost an hour to find:

1. **Do not build from `master`.** 0.14.0-beta cannot download anything at all
   (upstream #1970, open since 2026-08-23, many reporters, master unmoved since
   4 August). **`v0.13.0-beta` is the last tag where downloading works.** The
   PR's patch applies to it cleanly.
2. **There is no Android toolchain on red5** and there cannot be — an SDK plus
   Gradle wants ~10GB and the disk sits at 98%. Build with the fork's own
   `.github/workflows/build-apk.yml`, then `gh run download <id> -n audiobookshelf-apk`
   (~17MB, ~4 min).
3. **That workflow has `paths-ignore`, and GitHub skips path-filtered workflows
   on branch creation.** The first push of a new branch never builds. Push a
   commit touching a tracked file onto a branch that *already exists* — use a
   throwaway `ci/...` branch so the PR branch stays one commit.
4. **The debug build is `com.audiobookshelf.app.debug`**, so it installs beside
   the real Audiobookshelf and cannot disturb its login or downloads. Its
   versionCode follows the tag (0.13 = 117, 0.14 = 118), so installing an older
   one needs the newer uninstalled first or Android refuses it as a downgrade
   with a bare "App not installed".

Deploy route: `scp` to `p8a:/storage/emulated/0/Download/` and David taps it in
Files. **adb is not available** — p8a's own wireless-debugging pairing needs a
code read off the screen, and he does not enable it off a trusted network.

## What "Sasonica on the phone" probably means

Worth asking David rather than assuming, because these are different jobs:

- **His daily client**: a release-flavoured build, its own applicationId and
  name so it is not "Audiobookshelf (debug)" on the launcher, installed
  alongside or instead of the stock app. Needs a signing key — note the
  companion app's keystore is gitignored and has bitten us before.
- **A staging ground for changes**: keep it debug, keep the stock app, and
  land things like #2010 in the fork first.
- **The absorption path**: the fork is where the companion app's jobs (audio
  focus, mic detect, the canvas) eventually move. That is a direction, not a
  next step.

If it is the first, the branding pass is the interesting part: app name,
applicationId, icon, and the settings/about strings — and a decision about
whether it tracks upstream (rebasable) or diverges.

## State of the world it inherits

The session that wrote this also consolidated the conversation library. Briefly,
so nothing here is a surprise:

- A conversation is now **one Audiobookshelf item that grows**: tracks are the
  speech clips themselves (hardlinks, zero new bytes), chapters are the turns,
  written by `book_tracks.publish_chapters` because ABS only derives chapters
  when an item is born.
- `book_export`, `media feed books` and the concatenated-mirror layout are
  **gone**; the feed still publishes documents, and `media feed session <id>`
  still archives one conversation as a single file by hand.
- Only **8 of 164** conversations still have their clips; the other 156 exist
  only as concatenated episodes in the spool, which is why those episodes were
  left alone. The 91MB spool is an archive, not a duplicate.
- Music volume is back to **100** (service default, live player, and
  `MEDIA_MUSIC_VOLUME` in `agent-media.env` so the coordinator restores to the
  same number after a duck).

Loose ends nobody is blocked on: the ASI mic app-op block is loose again and
needs adb; red4 is unreachable (expired Tailscale node key).

## Memory worth loading

`sasonica-fork`, `abs-app-pr-and-ci`, `abs-fork-direction`,
`adb-shell-via-self-pairing`, `concurrent-sessions-commit-sweep`.
