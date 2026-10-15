# Personas in presets, per project, and private from birth

David, 15 Oct 2026: presets (contract §6.24) should be able to start a chat
as one of the personas in `~/agent-personas`; that should fit the
AGENTS.md draft; and private personas need a safe way in, perhaps through
seal. Status: **step 1 built 16 Oct 2026** (personas.py, contract §6.24);
steps 2–4 proposal.

## What there is

- `~/agent-personas` (private repo `davidj4tech/agent-personas`): nine
  personas (aria, bel, frankie, household, lena, lucy, maya, sam, zara) and
  three roles (planner, researcher, reviewer). The public shells are
  routing notes only; identity, voice and relationship material is
  `private/*.sops.md`, age-encrypted.
- `bin/persona-render <id> --adapter <harness> [--role <id>] [--mode
  public|summary|private]` turns them into a prompt shaped for each harness;
  `--list` names them all. `public` reads public files only; `summary` and
  `private` call `sops -d`.
- `adapters/claude/README.md` already says where a persona goes: project
  instructions when durable, a session preface when temporary, and "treat
  rendered private prompts as sensitive because Claude transcripts, debug
  logs, or config files may persist them".
- Presets (§6.24): agent, login, model, plan, AGENTS.md text, opening words,
  applied by the app. `agents_md: "draft"` (c4106e0): the first chat writes
  AGENTS.md from the repo.
- Seal (afd85b7): an ended thread's transcript, speech rows, clips and shelf
  folder are tarred into age and removed; `sealed.is_sealed` keeps search,
  the shelf export and the stored-session scan off it.

## 1. Public personas in presets

A preset gains `persona` and `role` (ids, `""` for none). `GET /presets`
adds `personas` and `roles` from `persona-render --list`
(`MEDIA_PERSONAS_DIR`, default `~/agent-personas`; both lists empty when it
is not there, so a host without the repo just has no persona chips).
`/ask` takes `persona` and `role` as well, so a chat can carry one without a
preset.

At start the server runs `persona-render <persona> --adapter <agent>
[--role <role>] --mode public` and delivers it by the harness's own means:

| Harness | Delivery |
| --- | --- |
| claude | `--append-system-prompt-file <tmp>` |
| pi | `--append-system-prompt <file>` (pi reads text or a file) |
| codex, opencode, hermes | ahead of the first message, under a `# Persona` heading, until each one's system-prompt route is checked |

A system prompt is preferred: it holds for the whole conversation and does
not show in the thread. The thread's header shows the persona's name as a
chip, so it is not invisible either.

## 2. A project's persona, and the AGENTS.md draft

A persona chosen for a **new project** becomes that project's: kept in
`<state_dir>/project-personas.json` (`{"<cwd>": {"persona", "role"}}`), and
every fresh chat opened in that folder — from a place, a worktree of it,
or `project` — gets it as in §1 unless the chat names another or "none".

The repo gets the persona's **name**, never its text. With
`agents_md: "draft"`, DRAFT_PROMPT gains one sentence: "This project is
worked on as the persona <name> (rendered by agent-personas); say so in one
line under Conventions, without describing the persona." With a template,
the server appends that line itself. So a clone of the repo elsewhere
knows a persona is expected, and the content stays in agent-personas where
it is versioned and encrypted.

Rejected: writing the render into AGENTS.md. It would put the persona into
git history, it goes stale when the persona is edited, and a private one
could never go there.

## 3. Private personas, private from birth

Seal alone is too late: it encrypts a thread after the fact, but while the
chat is live its plaintext is written to the transcript, the speech history
and clips, the search index, the shelf (ABS), the org bridge's note, the
hourly memory sync, and fleet-sync's staging. A private persona's chat has
to be kept out of those **from its first turn**.

**The mark.** `private: true` on `/ask` (and on a preset, required whenever
its mode is not `public`). It is recorded before the harness starts, in
`<state_dir>/private.json` (or a `"private"` state in `sealed.json`, so
every path that already asks `is_sealed` skips it unchanged — preferred:
one question, one list). While live, a private thread:

- is not indexed for search, not exported to the shelf/ABS, not exported
  by the org bridge, and not picked up by the memory sync or fleet-sync;
- is listed in the app with its title, a lock mark, and no recap;
- speaks normally (the clips exist only until it is sealed).

**The render.** `summary` or `private` mode runs `sops -d` on red5 only, at
start, to a 0600 file in `$XDG_RUNTIME_DIR` (tmpfs) passed by
`--append-system-prompt-file` and deleted once the harness has read it —
never on a command line (`ps` shows arguments) and never in the first
message.

**The finding that changes this.** Claude Code records the system prompt
by default (`--system-prompt-snapshot on`): on the first request it is
rendered, sent and **recorded with the conversation**, and every resume
replays the record. So a private render passed as a system prompt ends up
in the transcript anyway. Two ways through:

- **(a) Snapshot on, seal covers it.** Accept that the transcript holds the
  render; it is one more reason the thread is sealed. Resume works with no
  re-render.
- **(b) Snapshot off.** `--system-prompt-snapshot off` keeps it out of the
  transcript, but every resume must render and pass it again (sessiond and
  the pane resume paths), and a decrypt per resume.

Recommended: **(a)**. The transcript is already the most sensitive thing
in the thread — the conversation itself is private — so it has to be
sealed regardless; (b) protects the persona and not the conversation.

**The end.** A private thread is sealed automatically when it ends, or
after `MEDIA_PRIVATE_IDLE` (default 2 h) with no turn. Resuming one
unseals it into the same private state, never into an ordinary thread.
Moving a private thread to another project keeps it private.

**What none of this covers.** The model provider sees the persona and the
conversation. For the most private ones, a preset can pin a login that
keeps no logs (pi on the Venice profile); the preset editor should say so
next to the private switch. Anything the agent itself writes to disk —
files in the project, its own memory tools — is outside the mark.

## Order

1. Public personas and roles in presets and `/ask`; the header chip.
2. Project personas and the AGENTS.md line.
3. The private mark (live exclusions) — checked path by path against
   every reader of transcripts, with a test per reader.
4. Private renders (sops at start, tmpfs file) and auto-seal on end/idle.

## To check before building

- Every reader of transcripts and speech history, listed and tested
  against the private mark (search, shelf/ABS, org bridge, memory sync,
  fleet-sync, recaps, the dashboard, session_feed).
- Whether codex, opencode and hermes take a system-prompt file.
- Whether `--system-prompt-snapshot` is on for this host's Claude Code
  ("no effect where system-prompt recording is not yet enabled").
- Where a pane-started Claude writes debug logs that could hold the prompt.

## Open after step 1

- **The render files are never deleted.** Each fresh chat with a persona
  leaves a 0600 file in `$XDG_RUNTIME_DIR/agent-media/personas/`
  (`<random>.md`, linked as `<session>.md`): Claude may be handed it again
  on resume (sessiond passes it at every spawn while it exists), so step 1
  keeps it. tmpfs clears it at reboot; a public render is harmless, but a
  private one (step 4) needs a lifetime — delete after the first request
  with the snapshot on, or on seal.
- **Pane resumes do not re-pass it.** A resumed pane Claude relies on the
  recorded system prompt (snapshot on); a pi resumed in a pane gets no
  persona. A per-session lookup by `<session>.md` would fix both.
- **`prepend` shows in the thread**: codex, opencode and Hermes get the
  render as the first message, and it is recorded as the listener's turn.
- The thread header's persona chip (§1) is not built: nothing records a
  session's persona yet, beyond the `<session>.md` file and `/ask`'s answer.
