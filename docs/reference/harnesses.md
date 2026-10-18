# Harnesses: finding, reading and driving the agents' sessions

The phone reaches conversations in Claude Code, Codex, pi, Hermes,
opencode, Gemini CLI and Grok. Two modules hold the differences:

- `packages/core/src/agent_media_core/harnesses.py` — the table: where each
  harness keeps its transcripts, how a live session is recognised, how one is
  resumed. Its module docstring is the reference for every harness; this page
  does not repeat it.
- `packages/server/src/agent_media_server/harnesses.py` — getting a harness
  onto a host and signing into it from the phone (`RECIPES`, run in a
  background tmux window the phone can read and type into).

How each harness is wired for speech is its `media-setup profile` row; for
Claude Code see [claude-code-hooks.md](claude-code-hooks.md).

## Recognising a live session

Each harness needs its own trick, because few of them say which session a
process is on:

- **Claude Code** writes `~/.claude/sessions/<pid>.json` (`claude_sessions`).
- **Codex** holds its rollout file open, so the session is read off
  `/proc/<pid>/fd`. The file exists only after the first message, so a fresh
  Codex started from the phone is asked for its id *after* the send.
- **pi** retitles its process to `pi`, drops its arguments and does not hold
  its file, so nothing outside says which session it is on. The agent-media
  extension registers pane → session on every session start, under
  `$XDG_STATE_HOME/agent-media/agent-panes/` (`register_pane`). Gemini CLI's
  and Grok's SessionStart hooks do the same. A fresh pi or Grok can be given
  its id up front (`--session-id`), so a phone-started chat knows its id
  before anything is said.
- **Hermes** and **opencode** keep conversations in SQLite; the live
  session is found through the process's open database and argv.

pi's model calls may go through Meridian, which runs Claude Code headless
under systemd. Those `claude` processes have no pane and their sessions are
Meridian's: a pi conversation is known only by pi's own session id.

Codex names each thread with a hidden turn answering `{"title": …}`; the
Codex intake filters it so it is neither spoken nor shelved
(`packages/intake-codex`).

## Reading a pane

Whether a session is working, waiting for input or on an approval is read
from its tmux pane (`agent_media_server/panes.py`, `classify_cc` and
`classify`).

**Assume the phone's width, not the desk's.** A phone-sized tmux window is
around 28-34 columns, and Claude Code cuts its footer to fit:
`esc to interrupt` becomes `· e…`. Matching only the full footer made every
working session read as waiting on you (fixed 20 Sep 2026). The classifier
also matches the spinner line (`✽ Gitifying… (thought for 7s)`), which
survives any width. Test captures for anything that reads a TUI should be
taken at about 34 columns.

A permission dialog is checked before the footer, because it covers it.
Codex holds its whole TUI on a **"Hooks need review"** prompt whenever its
`hooks.json` changes, until someone at the desk answers; that reads as
`approval`, and `pane_ready` treats every Codex startup prompt as not ready
so the phone never types into one.

### The ghost prompt

The dim follow-up Claude Code draws on its input line after a turn (taken
with Tab) is offered in the app's reply box as `suggestion`. It exists only
on the screen: the transcript records that a suggestion was accepted, never
its words. So it is scraped (`sessions.ghost_prompt`): the line starting
with `❯`, the dim (SGR 2) run after it, continued down while the lines stay
dim. Typed text is not dim, so a half-typed line reads as no ghost.

On a narrow pane the TUI cuts a long ghost with `…`, and the cut is what gets
scraped. So the Stop hook also writes its own follow-up per reply
(`intake/_followup.py`, filed under
`$XDG_STATE_HOME/agent-media/followups/<session>.json`, keyed to the reply).
`sessions.suggestion_for` offers the real ghost when it does not end in `…`,
else the follow-up written for that same reply. `MEDIA_FOLLOWUP=0` turns the
follow-up off; `MEDIA_FOLLOWUP_MODEL` picks its model (a small hosted model
answers in seconds, where a local CPU model can take half a minute).

## The slash menu

The reply box's `/` menu comes from Claude Code itself
(`agent_media_core/slash_menu.py`, served as `GET /commands`). Run headless,
`claude -p ok --output-format stream-json --verbose` first emits an `init`
event naming every command and skill the session would accept, plus
`claude_code_version`. The menu reads that line and stops the process
before the prompt reaches a model, so the listing costs a few seconds and no
tokens. It is cached per directory and thrown away when the version changes.

The event is names only: descriptions come from each skill's or command's
own front matter, then from Claude Code's bundle. Claude Code's built-ins
are left out on purpose: typed from the phone, `/status` draws a screen
nobody sees, and `/resume` or `/config` open a picker the phone cannot drive,
leaving the pane to swallow the next reply.

`claude` is found on PATH, then in the usual install places; a systemd
service has no shell PATH, so set `MEDIA_CLAUDE_BIN` if it lives elsewhere.

## Opening a session from a service

Starting or reviving a TUI from the phone goes through
`agent_media_server/send.py`: `open_window(session, cwd, resume=…,
host=…, agent=…)`, with `hold_client` behind it. Use it rather than
reinventing it; each of these traps looked like the last:

- **No PATH.** A session created from systemd has no fnm, pyenv or venv on
  PATH, so `claude` (or pi's `node`) is "not found". `open_window` resolves
  the agent's binary itself (`harnesses.program`) and puts its directory
  first.
- **A detached session dies.** A tmux session nobody is attached to cannot
  host a Claude Code TUI, and one created detached can lose its only window
  to an after-new-session hook. `hold_client` creates the session attached
  from its first moment, `script -qfc 'tmux new-session -A -s NAME -c DIR'
  /dev/null`, in a transient unit `agent-media-tmux-hold-<host>` so it
  survives a canvas restart.
- **zsh eats `=name`.** `script -c` runs `$SHELL`; under zsh, `=word`
  expansion swallows a `-t =name` tmux target ("session not found" is zsh's
  message, not tmux's). The holder uses `-s name`. In an interactive shell,
  quote `'=name'`.
- **Ready is not ready.** `pane_ready` says yes at the first sight of the
  TUI's chrome, a beat before it takes Enter, and text sent then sits in the
  box unsent. `_settle` waits for two identical captures, and
  `_ensure_submitted` presses Enter again while the words are still in the
  box.
- **Never two copies.** A resume first checks no process already runs the
  session: two writers interleave their turns into one transcript.

`pane_ready` answers Claude Code's "Resume from summary / full session"
modal itself; resuming from a summary runs a compaction first, which can
hold the first reply for a minute or two on a large transcript.
