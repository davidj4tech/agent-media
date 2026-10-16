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

**Sasonica Shell as the last step, opt-in** (David, 16 Oct 2026). After the
server is installed and paired, on Linux, a Mac and Windows (not Termux),
one question, default No:

    Also let an AI assistant such as Claude run commands on this computer
    (Sasonica Shell)? It needs a free Sasonica account. [y/N]

Yes runs Shell's own hosted install: on Windows its one-liner,
`sasonica.com/shell.ps1` (get.ps1 → `install.ps1 -Hosted`); on Linux and a
Mac its repository as a tarball to `~/.local/share/sasonica/shell` and its
`install.sh --hosted`. Shell gets Node if it is missing and prints a link and
a code to sign in with a Sasonica account. A Shell that fails does not fail
the install; it prints how to try again. The question comes from `/dev/tty`
under `curl | bash` and from Read-Host under `irm | iex`; with no terminal, or
a host that cannot ask, the answer is No.

| | install.sh | install.ps1 |
|---|---|---|
| Shell without asking | `SASONICA_WITH_SHELL=1` | `-Shell`, or the same variable |
| no Shell, no question | `SASONICA_WITH_SHELL=0` | `-NoShell`, or the same variable |
| Shell alone, no server | `SASONICA_ONLY=shell`, `bash -s shell`, or `sasonica.com/shell` (deploy/shell.sh) | `-ShellOnly`, `SASONICA_ONLY=shell`, or `sasonica.com/shell.ps1` |

Through `irm`, a switch needs the script block form:
`& ([scriptblock]::Create((irm https://sasonica.com/install.ps1))) -Shell`.

Both are called `sasonica`, and where both are installed there is one
command, the server's (David, 16 Oct 2026): `sasonica shell …` runs Shell
with any arguments, and Shell's words the server has none of (`status`,
`skills`, `tools`, `url`, `client`, `sign`, `--once`) go to Shell by
themselves; `install`, `pair`, `help` and no words stay the server's, and
its help lists Shell's when Shell is there (packages/sasonica/src/sasonica/
shell.py). Beside the server Shell's installer writes no command: it records
its runner (the node, then `sasonica.mjs`, a line each) in
`~/.config/sasonica/runner` (`%APPDATA%\sasonica\runner`), which it writes
alone too, and its service runs node and the runner directly. A Shell
installed first, or one that wrote a shim beside the server (an older
installer: over `~/.local/bin/sasonica`, which install.sh keeps a hard link
of and puts back, or as `sasonica-shell`/`sasonica-shell.cmd`), has the
shim read into that record and removed, by install.sh, install.ps1 and
`sasonica install`. Shell itself refuses a word it does not know rather
than starting its polling loop. Nothing else collides:
Shell's service is `sasonica-shell` (systemd), `com.sasonica.shell` (launchd)
or the root Scheduled Task `sasonica-shell`, beside the server's
`sasonica-canvas`/`-sessiond`/`-quick-tunnel`, `com.sasonica.*` and
`\Sasonica\*`; Shell listens on no port (8781 and 8789 are the server's); its
config, `~/.config/sasonica` (`%APPDATA%\sasonica`), is one the server only
reads (shell_signin.py), the server's own being `~/.config/agent-media.env`.
Tested: `packages/sasonica/tests/test_install_sh.py` (stand-ins for the
binary, Shell and /healthz) and `test_install_ps1.py` (Windows CI).

## Does it need a multiplexer?

No, and headless-first is the decision (David, 29 Sep). A headless session is
`claude -p --input-format stream-json` held by `sessiond` — no terminal, no
tmux (2026-09-22-headless-sessions.md). tmux is left only where:

- ~~**opencode** runs in a pane on the phone.~~ **Built 29 Sep 2026:** a
  headless opencode chat is a session of **one shared `opencode serve`**
  that sessiond holds (`sessiond_opencode.py`) — one server, not one per
  chat, because each takes ~480 MB. Its events (`/global/event`) drive the
  same states; its permission and question requests become the same
  approval card, and the answer goes back as opencode's `once`/`reject` or
  question labels; interrupt is `abort`, rename a `PATCH`, the model rides on
  each prompt. Strict permissions are an opencode ruleset (ask for all but
  reading). The server is loopback-only with its own password, stops when no
  chat is attached, and starts again on the next message. The transcript is
  opencode's database, read as before, and speech is its plugin (the hook
  finds the chat's workspace in sessiond's record). Tested against real
  opencode 1.18.33 on a free model — approval, allow, deny, interrupt, park
  and resume, close — and in CI against a fake server
  (`tests/test_headless_opencode.py`).
- **Attaching from a desk** to watch a live session. That stays an optional
  mode through `panes.py` (tmux, herdr; WezTerm's headless mux-server builds
  for Windows — 2026-09-23-cross-platform.md), used when one is installed,
  never required.

The alternative of holding a PTY per session in the server (and showing it in
the app as a terminal) is its own small multiplexer, with ConPTY on Windows;
not needed while every agent has a structured protocol.

## The Mac (built 29 Sep 2026)

`sasonica-macos-aarch64` (Apple silicon), built on a `macos-14` runner with
its own cargo and python-build-standalone's `aarch64-apple-darwin`, published
beside the Linux ones; `curl -fsSL https://sasonica.com/install | bash` on a
Mac installs it the same way. `sasonica install` writes two launchd agents
(`~/Library/LaunchAgents/com.sasonica.{canvas,sessiond}.plist`, `RunAtLoad`,
`KeepAlive`, logs in `~/Library/Logs/sasonica`) and bootstraps them in the
login session; `sasonica update` kickstarts them and finds what runs from an
old version with `ps`. Reading other processes moved behind
`agent_media_core/procinfo.py` — /proc on Linux, `ps -E` and `lsof` on a Mac
— for the live sweep, the machinery check and sessiond's orphans (the
cross-platform proposal's step 1). A sessiond socket path past the Mac's
104-byte limit falls back to `/tmp/agent-media-<uid>/`.

Checked in CI, not on a Mac in hand: the binary's smoke test (the server
answers, sessiond listens, the plists lint) and the server's tests on macOS,
which fail only where Linux's do. Not done: an Intel Mac build, signing and
notarising (a curl download is not quarantined, a browser one would be), the
audio calls (`pactl`/`playerctl`; a Mac server speaks through the phone).

## Windows (built 29 Sep 2026)

`sasonica-windows-x86_64.exe`, built on `windows-latest` from Git Bash with
python-build-standalone's MSVC build, published beside the others. Install in
PowerShell:

    irm https://raw.githubusercontent.com/davidj4tech/agent-media/main/deploy/install.ps1 | iex

(`deploy/install.ps1`: the .exe to `%USERPROFILE%\.local\bin`, checked
against SHA256SUMS, that folder on the user's PATH, `sasonica install`, a
pairing QR). What it took:

* **sessiond over loopback TCP** — Python on Windows has no unix sockets. It
  listens on 127.0.0.1 at a port of the system's choosing and writes the port
  and a fresh token to `sessiond.endpoint.json` in its state folder (private
  by the profile's ACL); every request carries the token, and one without it
  is refused. `MEDIA_SESSIOND_TRANSPORT=tcp` forces it anywhere, and the
  headless tests run both transports on Linux.
* **UTF-8 mode** — Windows reads text in its code page; the binary runs itself
  again with `-X utf8` (PyApp's `-I` ignores PYTHONUTF8).
* **locks** through core's `_lock.py` (msvcrt there); a discarded note is
  closed before it is deleted, which Windows insists on.
* **`!` commands** through Git for Windows's bash (Claude Code needs it there
  anyway), else `cmd /c`.
* **`sasonica install`** writes a `.cmd` shim beside each sh one (Git Bash,
  where Claude Code runs hooks, finds the sh one) and two Task Scheduler
  logon tasks (`Sasonica\canvas`, `Sasonica\sessiond`) run under
  `conhost --headless`, so no console window opens. Since 12 Oct 2026 they
  are registered from task XML, restarting when they fail, and `sasonica
  update` upgrades older ones in place. `sasonica update` renames
  the running .exe aside to put the new one in.
* **orphans** are ended by the pid each record kept, and only while it is
  still an agent's program (`tasklist`): no other process's environment can
  be read there.

Checked in CI, not on a Windows machine in hand: the binary's smoke test (the
server answers, sessiond listens on its port) and the server's tests on
Windows, which fail only where Linux's do; tmux-pane and file-mode tests skip
there. Not done: code signing (SmartScreen will warn on a
browser download), a short sasonica.com address for install.ps1, and the
logon tasks and hooks exercised on a real desktop.

**Windows on ARM64** (David, 16 Oct 2026): no ARM64 build. Windows 11 on
ARM64 runs the x64 .exe under its x64 emulation (Prism), and the x64
cloudflared with it (Cloudflare builds no ARM64 Windows cloudflared).
install.ps1 reads the machine's own architecture (`RuntimeInformation.
OSArchitecture`, then `PROCESSOR_ARCHITEW6432`, then
`PROCESSOR_ARCHITECTURE`, so an emulated x64 PowerShell still counts as
ARM64), takes the x64 file and says once that it runs emulated; 32-bit
Windows is refused. Inside the binary, an emulated Python's
`platform.machine()` may say AMD64 or (3.12 asks WMI for the CPU) ARM64:
`tunnel.platform_asset()` and `sasonica update` take the x64 builds for
both. CI's `smoke (windows arm64, x64 emulated)` job in binary.yml runs the
x64 job's .exe through the same smoke test (deploy/binary/smoke-windows.sh)
on GitHub's `windows-11-arm` runner, checks install picks
cloudflared-windows-amd64.exe, runs install.ps1's architecture check in
Windows PowerShell and pwsh, and tries a real quick tunnel (allowed to
fail); it publishes and gates nothing. Windows 10 on ARM emulates only
32-bit x86, so it cannot run the server.

**Speaking on the Windows machine itself** (built 12 Oct 2026, opt-in):
`sasonica install --local-speech` adds a `Sasonica\speech` logon task that
runs the user's own mpv (`winget install --id shinchiro.mpv -e`) on the speech
named pipe, and `--local-speech off` removes it. The phone stays the default.
See 2026-09-23-cross-platform.md, "Audio seam".
