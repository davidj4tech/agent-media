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
