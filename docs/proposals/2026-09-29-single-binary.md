# Sasonica's server as one file

David, 29 Sep 2026: *"Is the sasonica server that gets installed on a user's
machine just a single binary?"* It was not: a git checkout, a venv and three
editable packages (`deploy/android/install.sh`). *"let's do that"* — PyApp,
Linux first, Windows and Mac later, headless first.

## What it is

`build/binary/sasonica-linux-<arch>`, 57 MB, built by `deploy/binary/build.sh`:

- a **PyApp** launcher (Rust, compiled in `rust:1-bullseye`, so glibc 2.31+)
- with a **python-build-standalone** CPython 3.12 embedded, core, server,
  visual and the new `packages/sasonica` already installed in it, byte-compiled.

The first run unpacks it into `~/.local/share/pyapp/sasonica/<id>/<version>`
(~1 s, 177 MB); every run after checks that directory and execs Python —
**65 ms** for `sasonica version`, 240 ms for `media --help`, measured on red5.
Nothing is fetched at run time. That start-up cost is why PyApp and not
PyInstaller's onefile, which unpacks on every start: hooks run it every turn.

Built natively per architecture (the packages are installed by the Python
they run under). CI (`.github/workflows/binary.yml`) builds x86_64 and
aarch64 (on `ubuntu-24.04-arm`) whenever anything the binary carries changes
on main, and publishes both with `SHA256SUMS` to the rolling **`server-latest`**
release.

## One command, many names

PyApp drops argv[0], so a `media` symlink to the binary cannot say which
command it wants. Every console script of our packages (and `edge-tts`, which
the default engine runs as a program) is a word after `sasonica`, found in the
installed metadata, so a new console script needs nothing here:

    sasonica serve        the canvas + the app's API (media-visual-canvas)
    sasonica sessiond     the headless session holder (media sessiond)
    sasonica install      shims, config, agents' hooks, services
    sasonica media say hi … any console script by name

`sasonica install` writes a two-line shim per command into `~/.local/bin`
(`exec "<binary>" media "$@"`), so Claude Code's hooks, opencode's plugin and
the run scripts keep the names they use. A file there that is not a shim (a
checkout's venv link, as on red5) is left alone without `--force`, and a host
already running `agent-media-visual-canvas.service` gets no second canvas.
Inside the binary, children see the bundled scripts on PATH too (PyApp
prepends its `bin`; the build rewrites their `#!` lines to find the Python
beside them).

## Install

`curl -fsSL https://sasonica.com/install | bash` on Linux now downloads the
binary for the machine, checks it against `SHA256SUMS`, renames it into
`~/.local/bin/sasonica` (a running server keeps its old file), runs `sasonica
install` — shims, `media-setup init --roles origin` when there is no config,
`MEDIA_HEADLESS=1`, Claude Code's hooks and opencode's plugin for whichever is
installed, `sasonica-canvas` and `sasonica-sessiond` as systemd --user units —
and prints a pairing QR code. Running it again updates. Tested end to end on
red5 with a scratch HOME and a `file://` release.

**The phone too** (29 Sep 2026): `deploy/android/install.sh` puts the
binary for Debian's architecture into the proot's `~/.local/bin`, checked the
same way, and `sasonica install --no-services` does its config and hooks;
Termux's runit runs `sasonica serve` and `sasonica sessiond`. No git, pip,
Python or venv in Debian, and Node only when Claude Code is chosen (opencode
brings its own runtime). `sasonica-adb` is fetched beside it.
`SASONICA_FROM=source` keeps the checkout install for working on the server
on the phone. Tested in a clean Termux (`termux/termux-docker` under podman):
the binary, opencode, both services up, a pairing code redeemed. The server
is ~237 MB there (the file and its unpacked copy), opencode 240 MB; the
whole Debian 871 MB, where it was 2.0 GB with Node.

## Does it need a multiplexer?

No, and headless-first is the decision (David, 29 Sep). A headless session is
`claude -p --input-format stream-json` held by `sessiond` — no terminal, no
tmux (2026-09-22-headless-sessions.md). tmux is left only where:

- **opencode** runs in a pane on the phone. The way round is its own server,
  `opencode serve` (HTTP), as a second headless driver beside Claude Code's.
  Next after this.
- **Attaching from a desk** to watch a live session. That stays an optional
  mode through `panes.py` (tmux, herdr; WezTerm's headless mux-server builds
  for Windows — 2026-09-23-cross-platform.md), used when one is installed,
  never required.

The alternative of holding a PTY per session in the server (and showing it in
the app as a terminal) is its own small multiplexer, with ConPTY on Windows;
not needed while every agent has a structured protocol.

## Mac and Windows, later

The binary is the easy part: PyApp and python-build-standalone both build for
both. What each still lacks is 2026-09-23-cross-platform.md's list — launchd
(and a Windows service) for `sasonica install`, `/proc/<pid>/environ` reads,
the audio calls. Headless-first takes the pane layer off that list.
