# Claude Code hooks: what agent-media registers, and why

agent-media hears a Claude Code session through its hooks. `media-setup
profile` writes them into `~/.claude/settings.json` (or into a
Sasonica-managed config with `--config-dir`); `media-setup status --json`
reports the same rows without writing. The spec lives in one place,
`CLAUDE_HOOK_SPEC` and `PROFILE_ROWS` in
`packages/core/src/agent_media_core/setup.py`, and this page explains the
values in it.

Other harnesses (Codex, pi, Gemini CLI, Grok, opencode) are wired by their
own profile rows; see [harnesses.md](harnesses.md).

## The speech hook

One command, `media-hook-claude-code`, registered on six events. It reads
the event name from its input and dispatches
(`intake/hook_claude_code.py`, `main`).

| event | timeout | async | matcher | what it does |
|---|---|---|---|---|
| `Stop` | 120 s | yes | — | renders and hands over the whole reply |
| `Notification` | 30 s | yes | — | speaks a notification (deduped against a recent Stop) |
| `PreToolUse` | 30 s | yes | `AskUserQuestion` | speaks a question as it is asked |
| `PostToolUse` | 30 s | yes | `AskUserQuestion` | an answered question: cut its read-out, record the answer |
| `PostToolUseFailure` | 30 s | yes | `AskUserQuestion` | a question answered by typing: the same clear |
| `UserPromptSubmit` | 10 s | yes | — | ends the read-out of the reply being answered |

Why each value is what it is (measured on a working machine, 23 Sep 2026):

- **Stop gets 120 s.** It renders a whole reply; 30 s killed long ones
  halfway.
- **Every entry is async.** A hook that speaks must not hold the turn open
  while it does, or the agent waits on its own voice.
- **The question hook is PreToolUse.** Claude Code fires no Notification
  when an AskUserQuestion modal goes up, so PreToolUse is the only event that
  sees a question in time.
- **The matcher is load-bearing.** Without `AskUserQuestion` on the two
  tool hooks, Claude Code would spawn the hook process on *every* tool call.
  A group of ours that lacks the matcher gets it on the next run.
- **PostToolUse is what ends a question.** `_handle_posttooluse` is the only
  thing that cuts the question's read-out
  (`request_session_speech_cut(session, "ask")`, `_stop_playing_ask`),
  clears the pending ask the phone shows (`pending_asks.clear`) and records
  the chosen option as a listener turn. Until it was registered (23 Sep 2026)
  all of that was dead code, and the options went on being read to somebody
  who had already chosen.
- **PostToolUseFailure is the typed answer.** Answering by typing instead of
  choosing ends the tool call as an error, so Claude Code fires
  `PostToolUseFailure`, not `PostToolUse`, and the typed words arrive as a
  queued command with no UserPromptSubmit. Without this entry nothing was
  cleared, and opening the thread later played the bypassed question
  (18 Oct 2026). The handler clears the same state and records nothing: the
  typed words reach the thread from the transcript.
- **UserPromptSubmit records nothing.** It calls `_handle_user_prompt` with
  `record=False`: a prompt means the last reply was read, so its speech stops
  at the end of the sentence (`session_reply_read`), and it ends a
  `/session/stop` cutoff. The listener's words reach the thread from the
  transcript (below).

The command is written `$HOME/...`-relative (`hook_command`), resolved
beside the installer's own interpreter when it can be. A hook's PATH often
has no venv or fnm shim on it, so a bare name can be installed and still
unreachable; `$HOME` keeps one settings file valid on machines whose home
is elsewhere.

**Tell-tale of an uncut question:** a history row with `kind=notif`,
`ask=True` and `flushed` unset, minutes after the answer. One with
`extras.opened_play` set was played when the thread was opened. Before
reading further into speech code that looks dead, check the hook is
registered at all:

```
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json')))['hooks'])"
```

## The other hooks the profile registers

| row | event | command | timeout | async |
|---|---|---|---|---|
| `heard` | `UserPromptSubmit` | `media-hook-heard` | 5 s | no |
| `context` | `UserPromptSubmit` | `media-hook-context` | 5 s | no |
| `statusline` | `statusLine` | `media-statusline` | — | — |
| `mail` (extra) | `UserPromptSubmit` | `agent-mail-inbox-hook --new` | 5 s | no |
| `catchup` (extra) | `UserPromptSubmit` | `agent-repo-catchup` | 10 s | no |

`heard` and `context` are deliberately not async: their output is added to
the prompt, and only a hook that holds the prompt can do that. They have
their own command names so the speech hook's merge, which matches on
`media-hook-claude-code`, never mistakes them for its own.

An `extra` row is skipped, with a reason, when its tool is not on the
machine. A status line someone set themselves is left alone, and the context
hook then guesses the window instead. The `restart` row adds the exact
`media restart-services --only sessiond|canvas --after 10` commands to
`permissions.allow`.

### What `media-hook-context` tells a session

`intake/context.py`. Two notes, both added to the prompt:

- **Context bands.** At 50, 70 and 85% of the window the session is told how
  full it is: work leaner at 50 and 70, and at 85 write a short handoff note
  before auto-compaction runs (a session cannot run `/compact` itself). Each
  band is told once; a drop below 50% (a compaction) resets them. The window
  is the one `media-statusline` saved from Claude Code's own count; a
  headless session has no status line, so its window is guessed by model id
  (`WINDOWS`), and `CLAUDE_CONTEXT_WINDOW` (tokens) overrides both.
- **The thread link.** Once per session, and again after a compaction, the
  session is told its own link: `<MEDIA_PUBLIC_URL>/app/t/<session>` when
  `MEDIA_PUBLIC_URL` is https, else `sasonica://t/<session>`. It is asked to
  link any session it names the same way. `MEDIA_THREAD_LINK=0` leaves it
  out. See server-contract §19 "Thread links".

Both always exit 0 and print nothing on anything unexpected: a prompt must
never be blocked by them.

## How questions, answers and slash commands reach the thread

A thread is built from the agent's own transcript
(`packages/server/src/agent_media_server/transcript.py`), not from speech
history, so the hooks above speak and clear but do not have to record.

- **A question** is a tool_use of `AskUserQuestion`. It becomes an `ask`
  part (`{"type": "ask", "ask": [...], "status", "answer", "tool_use_id"}`),
  and its `tool_result` fills `answer` and `status`, so a screen can lay out
  the options with the taken one ticked. On the speech side the read-out row
  carries a **list** in `extras.ask`, which is what lets a question (spoken
  on the alert lane, `kind=notif`) still count as part of the conversation
  (`session_feed`, `book_tracks._live_turn`). A bare `ask=True` does not.
- **Prose written before a question** in the same turn is spoken: the
  PreToolUse hook reads it back from the transcript (`_ask_lead_text`) and
  speaks it as the head of the question's clip. An agent should not also say
  it with `media say`; that is a second reading, and one the answer's cut
  may not reach.
- **The harness's own asides** in the prompt stream (task notifications,
  system reminders, a local command's output) are stripped by
  `strip_system_blocks` (`intake/_text.py`), the same rule on the spoken
  path and the thread path. A prompt with nothing left is no message.
- **Slash commands** are judged by one module,
  `packages/core/src/agent_media_core/slash.py`, used by the prompt hook, the
  phone's reply box (`agent_media_server/send.py`) and the transcript reader:
  - `parse()` reads both spellings: the `<command-name>`/`<command-args>`
    tags Claude Code writes when it ran the command, and a typed line. The
    name must be followed by whitespace or the end, so `/home/me/notes.md` is
    a path, not the command `home`.
  - `SETTINGS` (`model`, `clear`, `compact`, `config`, `help`, …) change the
    tool, not the work, and are left out. **Anything unknown counts as
    work**, because a project's own commands are the point.
  - A work command becomes a user message carrying
    `command: {name, args, text}`, which the app draws as a command chip
    rather than a speech bubble.
  - `already_recorded()` keys on name and arguments within the session, so a
    repeating `/loop` is recorded on its first firing only. A time window
    could not work: the loop picks its own interval.

A compaction is a message of its own (`compacted: true`), since the
`/compact` that asked for it is a settings command and is left out.

## Hand edits are rewritten

The profile owns these entries. Each run rewrites any entry whose command
contains one of its match strings (`HOOK_MATCH_SUBSTRINGS`, or the row's
own) back to the spec, and an installer that replaces the `hooks` block
wholesale and then runs `media-setup profile` (as a dotfiles installer
might) undoes hand edits completely. A hook it does not name is left
exactly as it is: the merge removes nothing. So a change to a timeout,
matcher or event belongs in `setup.py`, not in `settings.json`.

A running Claude Code session may not pick up a newly registered hook;
start a new session to test one. A change to the Python behind the
transcript or the hooks' server side needs the canvas restarted
(`media restart-services --only canvas --after 10`).
