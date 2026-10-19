# Agent notes

agent-media is the agent's audio/music stack: it gives coding agents a voice,
whole-house playback control and music awareness, on any Linux box or Termux.
See README.md for the packages and docs/server-contract.md for the server.

## Restarting services

- Restart with `media restart-services --only <name> --after 10`, e.g.
  `media restart-services --only sessiond --after 10` (short names: `sessiond`,
  `canvas`, `tts-shim`). It refuses while the service is busy (a session
  working, a reply being spoken), runs detached so it outlives your turn,
  re-checks before acting, health-checks each unit and raises the
  `restart-services.<host>` alert on failure. Add `--dry-run` to see the busy
  checks. With `--after`, a busy sessiond is drained rather than refused:
  new messages are held, busy sessions are told to end their turn, it
  restarts once none is busy, and cut-off sessions are resumed. If you are
  told "A restart of the session host is waiting", end your turn. Details: docs/reference/version-skew-monitoring.md ("Restarting
  safely, and from inside a chat") and
  packages/core/src/agent_media_core/restart_services.py.
- Never `systemctl restart agent-media-sessiond` (or any direct restart of
  sessiond) from inside a chat: a phone-started chat is a sessiond child and
  is killed mid-turn.
- A canvas page-only change (static/canvas.{html,css,js}, view.html) needs no
  restart: the page is re-read when its files change. A Python change does.
- `--force` skips the busy checks: use it only when David asks.

## Git

- No `Co-Authored-By` trailers or other AI-attribution lines in commits.
- Parallel sessions share this working tree: stage by path (check
  `git diff --cached`), never `git add -A` or `git commit -a`.
- Staging by path is not enough when another session is editing the same
  file: `git diff` every file you stage and look for lines you did not
  write. To commit only your hunks, split the patch on `@@`, then
  `git apply --cached --recount mine.patch` and `git commit` (no `-a`). Do
  not check a split with `git stash --keep-index`: the pop re-merges
  zero-context hunks by stale line numbers.
- Never `git pull --rebase --autostash` (or stash at all) over another
  session's uncommitted work. `git fetch` and look first.
- The runtime is shared too. Before restarting a shared service or
  installing an APK, check who else is active (next section), batch your
  changes into one restart or install, and say in your reply what you
  restarted or installed.

## Is another session live?

A session started from the phone runs headless under sessiond and has no
tmux pane, so `tmux list-panes -a` does not show it while it is working.
Before treating another session's uncommitted files as abandoned, check
when its transcript last changed:
`ls -lt ~/.claude/projects/<folder-slug>/*.jsonl | head` against `date`.
Seconds old means live, whatever tmux says. Leave its files alone and reach
it through sessiond: `media session list` shows the headless sessions (id,
folder, title, whether a question card is up), and `media session send
<id|folder> --from <you> <text>` puts a message in, queued behind a running
turn and never over a question or permission card (headless_mail.py).
`agent-mail-deliver --to <folder>` does the same when the folder has no pane
(pane first, then sessiond, a new session only when neither has one).
`ListAgents` and `SendMessage` cannot see these sessions: sessiond strips
their messaging socket.

## Testing without touching a real device

A shell on a working install carries the real config, so tests can reach
real players, phones and history. These have all happened.

- **The env file leaks in.** `intake/_env.py:load_env_file()` fills every
  unset variable from `~/.config/agent-media.env` (found through
  `Path.home()`), so isolating `XDG_STATE_HOME` alone does not stop a test
  that builds a real `SinkSpeech()` from playing on the configured device.
  Before a test that could build a real sink or player, strip every
  `MEDIA_*` and `RELAY_*` variable and set a throwaway `HOME`,
  `XDG_CACHE_HOME`, `XDG_STATE_HOME` and `XDG_CONFIG_HOME`. A helper must
  never default to constructing a real sink. When briefing a subagent on
  speech code, say this and forbid "listening checks".
- **The conftest scrubbers are part of every playback route.**
  `packages/core/tests/conftest.py` has autouse fixtures that remove live
  routes by prefix (`MEDIA_PHONE_PLAYER_URL*`, `MEDIA_REMOTE_SAY_CMD*`,
  `MEDIA_CONVERSE_MAILBOX*`, …). A new playback route or remote endpoint
  needs its variable scrubbed there; otherwise a test passes here by moving
  someone's playhead and fails in CI. To see what a run reaches off the
  machine: `cd packages/core && python -m pytest -p tests.phone_guard`.
- **Imported helpers skip isolation.** A test file that imports another test
  module's helpers does not get that module's autouse fixtures, so
  `submit_event` writes into the real `state.db` and clip cache (and the
  fake reply shows up in the user's history). Debug by editing the real test,
  or set `XDG_STATE_HOME`/`XDG_CACHE_HOME` yourself in any ad-hoc harness,
  and check `recent_history` afterwards for anything you made.
- **Muting does not silence a replay.** `media replay`, `play_playlist` and
  `claim_and_play` send `set_property mute false` as part of starting
  (`sinks/speech.py` `_start_cmds`). Do not replay on a real device to
  debug. Watch passively instead (`observe_property` on the frames hub,
  `MEDIA_SPEECH_FRAMES_LISTEN`), or test against a private frames hub with a
  scrubbed `HOME`.
- **An untargeted `media book play` follows the default target.** With the
  speech default on a phone, a local file is rsynced to the phone and plays
  there when the copy ends. To exercise book code, start a private mpv
  (`mpv --ao=null --pause --input-ipc-server=<scratch>.sock <file>`) and run
  `MEDIA_BOOK_SOCKET=<scratch>.sock media book … --target local`. The
  `--target local` is required: without it the default target is used and
  the override is never read. A stale copy leaves `book-stage.json`
  (`sinks.book.stage_status_path()`) showing "copying" until it is deleted.
- **The phone is someone's phone.** Drive it only with `agent-phone-adb`:
  open an app with `agent-phone-adb launch <package>`, never `monkey`
  (it sends a random input event and has switched auto-rotate on after
  installs; the tool refuses it), never press keys or HOME on a phone in
  use, and end any playback test with stop, not pause.
- **Run tests the way CI does.** CI (`.github/workflows/ci.yml`) runs core's
  tests from `packages/core` (`pytest -q`, Python 3.11 and 3.12) and the
  server's from the repo root (`pytest -q packages/server/tests
  packages/sasonica/tests`). Check core with
  `cd packages/core && ../../.venv/bin/pytest -q`. Green here and red in CI
  usually means a test read this machine's env file (it fails locally too
  when run alone, which is the tell), or compared `time.monotonic()` with
  `0.0` as if it meant "never": the monotonic clock counts from boot, so a
  long-up host and a fresh CI runner disagree. Use a `None` sentinel.
- **A worktree's tests import main's code.** The repo `.venv` installs every
  package editable from the main checkout, so pytest on a worktree runs
  main's sources. Put the worktree first:
  `PYTHONPATH=<wt>/packages/server/src:<wt>/packages/core/src .venv/bin/pytest -q --rootdir=<wt>/packages/server <wt>/packages/server/tests`,
  or merge first and test on main.

## Design rules

- The app stays generic and the server knows the setup: when the app needs
  to know about a host, add a server endpoint and render its answer.
  docs/decisions/2026-09-21-app-stays-generic.md
- Nothing new relies on the tailnet: public HTTPS with a scoped credential,
  or a connection the device opens outbound; a tailnet path is only a
  fallback. docs/decisions/2026-10-18-off-the-tailnet.md
- Licensing: Apache-2.0, copyright South Pen Labs. The Audiobookshelf forks
  stay GPL v3 and their code is never copied in; GPL programs (mpv, Piper,
  paragtd) run as separate processes and are never imported.
  docs/decisions/2026-09-22-licensing.md
- The canvas carries the reply's words, not ambient art.
  docs/decisions/2026-09-21-canvas-carries-words.md
