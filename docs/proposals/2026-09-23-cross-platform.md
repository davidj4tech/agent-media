# agent-media on macOS and Windows: the boundary is tmux, not fcntl

Status: proposal, nothing built.
Date: 2026-09-23

David: *"It would be nice if agent-media was cross-platform too."*

## Recommendation in one line

The thing that decides this is **tmux**, not the posix calls the earlier note
named — and the answer to tmux is **herdr**, which we already speak and which
already carries Windows shells. macOS is then a week of mechanical work;
Windows becomes arguable rather than out of the question.

## The earlier note named the wrong blocker

[cross-platform-notes.md](../cross-platform-notes.md) (20 Sep) put the cost on
`fcntl` (~35 refs) and posix (~86). Measured again today, that is not where
the weight sits:

| | refs | where |
|---|---|---|
| `fcntl` | 29 | **8 of them in one file**, `core/_lock.py`; 7 in `intake/submit.py`; the rest 2–3 each |
| `tmux` | 94 in core alone | `cli.py` 120, `intake/submit.py` 76, `hook_claude_code.py` 35, `layout.py` 33, `conversation.py` 28, and 16 more modules |
| service units | 8 files, two supervisors | `systemd --user` and Termux runit |
| `sys.platform` / `platform.system()` | **0** | there is not one platform check in the codebase |

`fcntl` is a lock. One file, one concept, and Windows has `msvcrt.locking`;
an hour's work behind an interface we would want anyway. It looked like the
blocker because it is easy to grep.

tmux is not a dependency, it is **the model**. A session *is* a pane: we type
into it with `send-keys`, read it with `capture-pane`, find it by
`TMUX_PANE` in `/proc/<pid>/environ`, and lay panes out with `layout.py`. A
platform without tmux does not need a shim; it needs another answer to "where
does the agent live and how do I reach it".

## What already exists (most of the argument)

Three seams are already cut, by work done for other reasons:

- **Addresses carry their multiplexer.** `server/panes.py` already dispatches
  three verbs — read, send-text, label — on an address that is either a bare
  tmux pane id (`%562`, kept bare so old history rows still resolve) or
  `herdr:w6:p1`. A second multiplexer is already in the tree and working.
  This is the seam a third one would use.
- **The supervisor is already chosen at runtime.** `setup.py
  _service_backend()` picks runit or `systemd --user` per host, and
  `media-setup profile` wires a machine end to end. launchd is a third
  backend in a place that already expects backends.
- **Hosts already have roles.** `~/.config/agent-media/config.toml` decides
  what runs where (observe / render / origin). A Mac that is a *render* host
  and not an *origin* needs a fraction of the daemon.

## herdr is the answer to the tmux problem (David's, 23 Sep)

The hard part above was "a platform without tmux needs another answer to
where the agent lives". We already have one, running, in this tree:

- `server/panes.py` dispatches read / send-text / label on `herdr:w6:p1`
  exactly as it does on `%562`, and discovery reads `HERDR_PANE_ID` from the
  process environment beside `TMUX_PANE`. **The abstraction is not a
  proposal; it is in production.**
- herdr is a single static binary (0.9.0 here, `~/.local/bin/herdr`) that
  calls itself a terminal workspace manager for AI coding agents, with
  `machine` and `--remote` subcommands — multi-machine is its premise, not
  an add-on.
- Its binary carries Windows shell handling (`windows_cmd`,
  `windows_powershell`), which suggests upstream builds for platforms we do
  not run. **Unverified** — the one question to put to the project before
  any of this is planned around it: are there macOS and Windows builds, and
  do `pane read` / `pane send-text` behave the same on them?

If the answer is yes, the port stops being a rewrite. The remaining Linux
assumptions are the small, nameable ones — the lock, `/proc/<pid>/environ`,
the service backend, the PulseAudio calls — and the pane model, which was
the expensive part, travels as it stands. That is the difference between a
platform layer we design and one we already shipped for another reason.

## Zellij, checked against the same three verbs (David's, 23 Sep)

Zellij 0.44.3 is already on this machine (`~/.local/bin/zellij`), so this
part is measured rather than recalled — and it clears the bar the pane layer
actually sets:

| what `panes.py` needs | tmux | zellij 0.44.3 |
|---|---|---|
| read an arbitrary pane | `capture-pane -t %562` | `action dump-screen -p terminal_1` (`--full` for scrollback, `--ansi` for colour) |
| type into an arbitrary pane | `send-keys -t` | `action write-chars -p` |
| label a pane | `select-pane -T` | `action rename-pane -p` |
| find the pane a process is in | `TMUX_PANE` | `ZELLIJ_PANE_ID` (also `ZELLIJ_SESSION_NAME`, `ZELLIJ_SOCKET_DIR`) |

Every action takes `--pane-id`, so nothing has to be focused first — which
was the objection worth checking, since a capture that steals focus would
race with whoever is reading the screen. It does not.

So `zellij:terminal_1` is a third address in exactly the shape `panes.py`
already dispatches, and unlike herdr's wider platform support, this one can
be verified here today without asking anyone.

**Windows, though: no.** Zellij's server terminal IO lives in
`os_input_output_unix.rs`, and there is no Windows counterpart; upstream's
answer there is WSL. So Zellij makes **macOS** cheap and says nothing about
Windows — where herdr, if it builds, remains the only route for the daemon,
and Sasonica Shell remains the route that needs no daemon.

Not a contest: herdr is already integrated and may travel further; Zellij is
verifiable now and is the lower-risk way to prove the pane layer really is a
layer. Either one being added is the test — if a second non-tmux
multiplexer drops in without touching `cli.py`, the abstraction is real.

## GNU screen, considered (David's, 23 Sep)

Not disqualified by age — screen is maintained (5.x, 2024) — and on paper it
has all four: `screen -X -p <window> stuff` types, `hardcopy -h` dumps with
scrollback, `title` labels, and it exports `$STY` and `$WINDOW`, which is the
same discovery shape as `TMUX_PANE`. **Unverified here:** screen is not
installed on red5, so unlike the Zellij row above, this is recollection.

It still loses on three specifics, none of them about age:

- **Windows, not panes.** `-p` addresses a *window*; split regions are a
  display concept and are not addressable the same way. Our model is a pane
  per agent, and `layout.py` splits deliberately.
- **A file, not stdout.** `hardcopy` writes to a path and drops styling;
  there is no equivalent of `capture-pane -e` or `dump-screen --ansi`, and
  the canvas reads colour to classify what an agent is doing.
- **"It's everywhere" is dated.** macOS ships a 2006-era 4.00.03 build, so
  the portability argument for choosing it over Zellij largely evaporates —
  and Windows has no native build either way.

So: it would probably work, it buys nothing Zellij does not, and it costs
colour. Worth knowing it is available as a fallback on an old Unix that has
nothing newer; not worth being the second multiplexer we prove the layer
with.

## kitty and WezTerm (David's, 23 Sep) — and the question they raise

Neither is installed on red5, so this is recollection, not measurement. But
they are a different *kind* of candidate from tmux, Zellij and screen, and
the difference matters more than the verb table:

**They are terminal emulators.** tmux, Zellij, screen and herdr all
multiplex on a machine with no display; kitty and WezTerm multiplex inside a
window on a desk. Our agents live on red5 and are reached over ssh, so the
first question is not "can it send text to a pane" but **"can it run with no
display at all"**. That is where the two part company:

- **kitty.** `kitten @ get-text`, `@ send-text --match id:N`,
  `@ set-window-title`, and `KITTY_WINDOW_ID` in the environment — the four
  needs are met, over its remote-control socket, which works across ssh. But
  the multiplexing *is* the GUI app; there is no headless kitty. On a
  server that is a non-starter, unless the model inverts and agents live in
  the terminal on the laptop instead of on the host that owns the media.
  David is right that there is no Windows build either.
- **WezTerm.** Has `wezterm-mux-server` — an explicitly headless multiplexer
  daemon — alongside `wezterm cli get-text --pane-id`, `send-text
  --pane-id`, and `WEZTERM_PANE` in the environment. **And it builds for
  Windows.** That makes it the only candidate so far that could be both the
  pane layer *and* the answer on Windows, without a display and without
  waiting on herdr.

So WezTerm deserves the same treatment Zellij got: install
`wezterm-mux-server` on red5, try the three verbs against a pane it owns,
and see whether `wezterm:<pane-id>` drops into `panes.py` beside the others.
If it does, the Windows verdict below changes from "herdr, if it builds, or
no daemon at all" to "there is a supported path". That is the single most
valuable unknown in this document.

## The three platforms, honestly

**macOS.** Everything the core shells out to exists: tmux, ssh, mpv, rsync,
fcntl — and Zellij and herdr besides, so the pane layer has three candidates
there rather than one. What is missing is launchd (8 unit files plus the runit scripts), the
PulseAudio calls (`pactl`, `playerctl` — CoreAudio has neither), and
`/proc/<pid>/environ`, which is how we find which pane a process is in;
macOS needs `ps -E` or `libproc`. Nothing here is research. Call it a week,
and the honest version of "nearly free" is "nearly free *if* the Mac is a
render or observe host, not the origin".

**Windows.** No tmux, no `/proc`, no signals, no runit. With herdr — or,
more promisingly, `wezterm-mux-server` — as the pane layer the first of
those stops mattering and the rest are the nameable
ones, so this moves from "a different program" to "expensive but bounded" —
conditional entirely on herdr having a Windows build. Until that is
answered, the shipping answer stands: **Sasonica Shell runs natively there**
— one Node runner, `install.mjs`, tested on hpo and in CI — so a Windows
machine can join as a client without the daemon at all. Those two are not
rivals: the Shell is how Windows joins *now*, herdr is what would let the
daemon follow later.

**Android.** Already the second platform, via Termux, and the `termux-*` and
`/storage/emulated` refs are by design, not debt.

## The order, if we do it

0. **Prove the pane layer with Zellij**, locally, before any platform work:
   a `zellij:` address beside `%562` and `herdr:`, verified on red5 where
   0.44.3 already is. If that lands without touching `cli.py`, the layer is
   real; if it does not, that is the finding. In parallel, **try
   `wezterm-mux-server`** (headless, and it builds for Windows) and **ask
   herdr about macOS and Windows builds** — between them they size the
   Windows half.
1. **A platform module in `core`** — `_lock.py` behind it first, then
   `/proc/environ` reads. One file to import, no scattered conditionals.
   This is worth doing on its own merits: it makes the posix assumptions
   *nameable*, which today they are not.
2. **launchd as a third service backend** in `setup.py`, beside runit and
   systemd. Mechanical, and testable on red5 by generating the plists
   without installing them.
3. **An audio backend seam** — `pactl`/`playerctl` are Linux; the mpv calls
   are not. Small, and the sink code already varies by host.
4. **A Mac as a render host**, end to end, before anything is claimed. The
   phone taught us this: it compiled long before it spoke.
5. **Windows only if herdr is there**, and even then the Shell stays the way
   a Windows box joins without a daemon.

## Audio seam (built 12 Oct 2026)

Step 3. `agent_media_core/audio_backend/` is the one place the host's audio
system is asked about; `backend()` picks a backend once per process
(`MEDIA_AUDIO_BACKEND=linux|windows|none` forces one):

| what callers need | call | linux | windows | anything else |
|---|---|---|---|---|
| pause other players for speech, resume what we paused | `playing_players()`, `pause_players()`, `resume_players()` | MPRIS through `route/_mpris.py`, unchanged | the system media transport controls (WinRT) | `[]`, logged once |
| mpv `--ao` when none is set | `default_ao()` | `pulse` | `wasapi` | mpv chooses |
| the `rooms` feed's device | `named_sink_device(sink)` | `pulse/<sink>` | none: `rooms` reports "not configured" | none |
| a configured device as mpv's id | `device(spec)` | as is | `wasapi/{…}` as is, else matched against mpv's device descriptions (`MEDIA_SPEECH_LOCAL_DEVICE=Headphones`) | as is |
| null sinks + default sink (`media-setup server`) | `sink_plumbing` | yes | no: skipped with a message, exit 0 | no |
| list devices | `output_devices()` | `mpv --audio-device=help`, parsed — the same everywhere | | |

The callers: the coordinator's local pause/resume, `_device_for` in
`sinks/speech.py` and `sinks/book.py` (a tcp:// broker's device is another
host's and passes untouched), the book broker's `--ao`, and `cmd_server`.
Remote MPRIS over ssh (`MEDIA_MPRIS_SSH_HOSTS`) stays in `_mpris.py`: those
hosts are Linux whatever this one is. `MEDIA_MPRIS_PAUSE=0` still turns
pausing off, on every backend. There is no cork to seam: policy.py names a
`pactl` cork as a future mechanism, and nothing implements one.

**Windows: WinRT in process, not PowerShell.** The media controls
(`GlobalSystemMediaTransportControlsSessionManager`) come through the modular
`winrt-*` 3.2 projections (runtime, Foundation, Foundation.Collections,
Media.Control), a core dependency marked `sys_platform == 'win32'` so Linux and
Mac installs never see it. Measured from the wheels: ~610 KB downloaded,
~1.5 MB installed, beside a 57 MB binary that unpacks to 177 MB. Each call is
COM in process, and the manager is fetched once and kept. The alternative,
shelling out to PowerShell, needs no dependency but costs a cold
`powershell.exe` start (hundreds of ms) on every pause and every resume, both
on the speech path. It also leans on Windows PowerShell 5.1's WinRT bridge
(PowerShell 7 dropped it), its `AsTask` await shim, and whatever language mode
a managed machine allows. A player's name is its app id (`Spotify.exe`, `#2`
for a second session of one app). The session objects are kept from pause to
resume, and a session is resumed only if it is still Paused. Every mpv session
is left alone, because a media session cannot say which mpv it is (the same
fail-closed rule as Linux); `MEDIA_SMTC_EXCLUDE` adds app ids to that list.
Failures (winrt missing, no desktop session, an operation past 2 s) leave
players alone and log once. A manager that could not be had is asked for
again after 60 s.

**Tested on red5 and on Windows CI.** On red5, `core/tests/test_audio_backend.py`
covers selection, the stub, Linux through mocked playerctl, mpv's device list,
and the Windows backend against a fake session manager. `test_audio_seam_callers.py`
covers the callers. Core and server suites show the same failures before and
after, all of them already present on main. CI's `pytest (core, Windows)` job
installs core with its win32 dependencies and gates on the audio and mpv-IPC
tests. Proved there (CI run 38208476555, binary run 38208064935, 12 Oct):

- **winrt imports and works, from pip and from the .exe.**
  `sasonica-windows-x86_64.exe media-audio --json` answers
  `"backend": "windows"` and `"winrt": "ok (0 media sessions)"`. The
  projection loads from inside PyApp, and `request_async` hands out a session
  manager in the runner's session. The zipped .exe artifact grew by 0.57 MB
  (50.00 to 50.58 MB).
- **mpv over a named pipe.** A real mpv 0.41 (choco) started with
  `--input-ipc-server=<endpoint_name(path)>` is driven through
  `get_property`, `set_property`, `get_properties` and `quit`. A Python pipe
  server is driven the same way, and `endpoint_exists` sees a pipe appear and
  go.
- **The rooms-hub tests are Linux by nature.** The speech-targets,
  audio-targets and `media-setup server` tests now pin the linux backend. The
  unix-socket fake-mpv tests skip where there is no `AF_UNIX`. On a Mac or
  Windows the picker no longer offers `rooms`, which those hosts cannot play.

The rest of core runs on Windows as a report, not a gate. 109 of 2596 tests
fail there for reasons outside the audio work, and they should be fixed where
they live:

- `os.killpg` in `media replay`'s follower (about 20 tests): a real Windows
  crash.
- `AF_UNIX` in the converse rendezvous and input claim (18).
- strftime's `%-d` in the agenda (12).
- `call_guard` and `_android` (20): phone-side code that shells out to `sh`.
- The docs walker's hidden-file and path rules (9).
- POSIX file modes (`0o600`, `0o700`).
- `$HOME/...` hook paths written with backslashes.
- Two timing tests on Windows' coarse clock.

**Still unverified until a real Windows desktop** (hpo is offline). A CI
runner cannot settle these:

- The runner has no audio endpoints, so mpv lists only `auto` and `openal`.
  The shape of a `wasapi/{…}` line, and the description matching, are still
  inferred from mpv's source.
- The canvas's Task Scheduler logon task under `conhost --headless` is not
  exercised. CI runs the binary from the job's own session.
- The app id mpv's `--media-controls` session carries is still assumed to
  contain `mpv`.
- Whether Edge, Chrome and Spotify obey pause and play sent this way. The
  runner has no media sessions at all.

**A Windows speech player: not built, David's call.** Pipes are the channel;
what is missing is a player to listen on one. The Linux binary installs no
`sink-speech` service either: every binary install speaks through the phone.
mpv is not in the binary. The options:

1. **Opt-in.** `sasonica install --local-speech` adds a third logon task,
   `Sasonica\speech`, that runs mpv (the user's own, via winget) on the speech
   pipe. A small Python launcher would replace the shell `run` script's argv on
   hosts without runit or systemd. The phone stays the default.
2. **Local by default when mpv is found.** The same task, installed whenever
   `mpv` is on PATH. Speech then moves off the phone for anyone who happens to
   have mpv.
3. **Bundle mpv.** The .exe or the installer carries mpv, about 30–40 MB more,
   and mpv is GPL. Local speech then works out of the box.

Each option also faces the limit the canvas and sessiond tasks already have:
`schtasks /Create` cannot ask for restart-on-failure, which would need a task
XML.

## What this costs if we never do it

Little, today — everything runs on red5 and a phone. The reason to do step 1
anyway is that a platform layer makes the posix assumptions explicit, and we
have just spent a morning discovering we did not know where they were.
