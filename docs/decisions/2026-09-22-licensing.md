# Decision: Apache-2.0, copyright South Pen Labs; GPL stays out of process

Status: **accepted**
Date: 2026-09-22

## The rule

- agent-media (every package), the Sasonica app (sasonica-app) and Sasonica
  Shell are licensed **Apache-2.0**. The copyright holder is **South Pen
  Labs**, named in each repo's `NOTICE`, not a person or a GitHub handle.
- A new repo or package gets the Apache-2.0 `LICENSE`, a South Pen Labs
  `NOTICE`, and `license = { text = "Apache-2.0" }` in its `pyproject.toml`.
- The Audiobookshelf forks (the old Sasonica app in the `sasonica` repo,
  and `sasonica-web`) are mostly other people's code and cannot be relicensed.
  They stay GPL v3. **Never copy code from them into an Apache repo**: it
  would bring the GPL with it.
- GPL programs are driven, never linked or imported. mpv, Piper, paragtd,
  Snapcast and Termux:API each run as their own process, reached over IPC,
  HTTP or the command line. A package that wraps one (engine-piper,
  org-paragtd) must not `import` it. The root `NOTICE` lists them.

## Why

Apache-2.0 is permissive and carries a patent grant, which the monetization
plan (`docs/proposals/2026-08-20-monetization.md`) relies on. David chose it
on 22 Sep 2026 after briefly choosing MIT.

## Consequences

- Bundling a GPL library into an app (libmpv in an Android build, for
  example) would make that app a derivative work. Keep such engines at arm's
  length, as the `NOTICE` explains.
- A package README that states a licence must say Apache-2.0.
