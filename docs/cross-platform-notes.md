# agent-media cross-platform notes (20 Sep 2026)

Runlet-side planning lives in the runlet repo: docs/windows-plan.md.
This file covers agent-media only.

## Codebase shape
- 280 first-party .py files (218 of them in packages/core), 92 java (Android),
  12 sh, 5 js, 2 ts. Overwhelmingly Python, so it travels reasonably well.

## Windows blockers
- fcntl (~35 refs) and posix (~86 refs) — no Windows equivalents, threaded
  through core. This is the real cost.
- mpv (705 refs) is itself cross-platform — not a blocker.
- termux (64 refs), /storage/emulated and adb are Android-only by design.

## Recommendation
- Stage it: macOS first (nearly free), Windows second.
- Put posix/fcntl behaviour behind a small platform abstraction layer in
  packages/core rather than scattering per-OS conditionals — mirroring what
  lib/platform.sh already does in runlet.
- iOS: thin client over Runlet, no local daemon. Android can run closer to full.

## Re-checked 23 Sep 2026

Two things had gone stale, and one premise is worth restating.

- **The pointer.** Runlet is Sasonica Shell now (renamed 21 Sep); the plan
  lives at `~/projects/sasonica-shell/docs/windows-plan.md`, which also
  carries a copy of the figures below — update both or neither.
- **The counts.** 388 first-party `.py` (240 in `packages/core`), 89 java,
  12 sh, 4 js/mjs, 2 ts — Python has grown, not shrunk. `fcntl` is down to
  ~29 refs. (The posix figure isn't comparable; it was counted with a
  different pattern.)
- **The premise.** The Shell already runs natively on Windows and macOS —
  one Node runner (`sasonica.mjs`) on every platform, `install.mjs` as the
  installer, tested on a real Windows machine and in CI. So a non-Linux
  machine does not need agent-media ported to be useful: it gets the Shell,
  and agent-media stays where the media is. That makes the Windows
  blockers below a cost to be paid only if we want the *daemon* there, not
  a prerequisite for reaching those platforms. "macOS first" still holds;
  "Windows second" may be "Windows never".
