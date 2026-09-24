# Proposal: Notes on plain Org, with paragtd as an optional package (24 Sep 2026)

Status: **decided 24 Sep 2026** (open questions answered as recommended); **steps 1 and 2 built the same day** (the profile seam and `packages/notes-paragtd`; agenda files, `#+TODO:` keywords, the Emacs import), step 3 (the `/notes` fields, and Next reading them), and step 4's sequencing (Org's blocking in core, paragtd's trigger in its package). Also built: the manifest (paragtd `c2af9ea`, read by the profile). Then capture templates on the phone (`notes_capture.py`) and the astro timer (notes-paragtd's `astro.py`). Changes §6.10 of `server-contract.md` (new
fields on `GET /notes`, a profile-dependent refile/capture list), moves the
GTD-specific half of `notes.py` / `notes_edit.py` / `notes_setup.py` into a
new `packages/notes-paragtd`, and adds one export command to paragtd.

## What prompted it

David (24 Sep 2026): could paragtd be included in Next or agent-media "as a
package rather than a plug-in", because "I'd like to be conscious of someone
using the app without the paraGTD setup. I think I'd also like it to work for
org-agenda without paraGTD … with the paraGTD functionality being an extra."

## What exists

The Notes tab (§6.10, built 22 Sep) is paragtd-shaped all the way down:

| Assumption | Where |
|---|---|
| The ten paragtd file names, with labels | `notes.GTD_FILES`, `notes_setup.SKELETON` |
| The TODO keywords `TODO NEXT WAITING SOMEDAY \| DONE CANCELLED` | `notes.STATES` (baked into the heading regex), `notes_edit.SETTABLE`, the app's `NoteState` type, `note.tsx`, `org.tsx`, `noteSort.ts` |
| The agenda = those files + `astro.org`, with the astro stale rule | `notes._agenda` |
| Capture = paragtd's `t` template into `inbox.org` | `notes.entry`, `notes.capture` |
| Refile targets next/waiting/tickler/someday/projects/inbox, each under a fixed headline with a fixed state | `notes_edit.REFILE_TARGETS`, the app's `RefileTarget`, `MoveSheet.tsx` |
| Only the GTD files are editable | `notes_edit._gtd_path` |
| Six fixed org-roam folders | `notes.ROAM_FOLDERS`, `notes_setup.ROAM_DIRS` |
| Setup starts a fresh tree as paragtd's files | `notes_setup` |

All of it was copied from paragtd by hand. `notes.py` already says a paragtd
JSON export should replace that copy.

What someone without paragtd gets today is an empty Notes tab. Their
`inbox.org` doesn't exist, so the app shows the setup screen, and setup
offers to create paragtd's files.

What paragtd does in Emacs that the app does not:

- **Sequenced projects** (`paragtd-sequence.el`, 23 Sep). `:ORDERED: t`
  blocks a step from closing while an older sibling is open, and an org-edna
  `:TRIGGER:` makes the next step NEXT, dated N days on. Closing a step from
  the phone does neither.
- **Its capture templates**, including `p` (sequenced project), `j`
  (journal datetree) and the site-local `paragtd-capture-extra-templates`.
- **Astro generation.** The app reads `astro.org` but never regenerates it.

## The shape

[[visual: three stacked layers. Bottom solid box "Org core — agent-media-server": agenda files, #+TODO keywords, agenda, capture file, state/date/refile. Middle solid-but-optional box "org-roam — in core, on when a roam dir exists": shelves from its subfolders, id links. Top dashed box "packages/notes-paragtd — entry point agent_media.notes_profiles": GTD views, refile targets, capture kinds, sequencing, astro. Arrow from top box out to a box "paragtd (Emacs, GPL): bin/paragtd-export → JSON manifest". Arrow from all three up to "GET /notes → Next draws what it's told".]]

### 1. Core: any Org files, the way org-agenda reads them

`agent-media-server` keeps the parts that work for anyone who uses Org:

- **Which files.** `[notes] agenda_files` in `config.toml`, as a list of
  files or directories (a directory means its `*.org`, as in Org). The
  default is every top-level `*.org` in the notes root (`MEDIA_NOTES_DIR`,
  default `~/org`). Setup can also offer to read `org-agenda-files` from a
  running Emacs (`emacsclient --eval`) and write the result into the config,
  once, so the server never needs Emacs to be running.
- **Which keywords.** Each file's `#+TODO:` / `#+SEQ_TODO:` / `#+TYP_TODO:`
  lines, falling back to `[notes] todo_keywords` and then to `TODO | DONE`.
  The heading regex is built per file, not from a constant. This is the
  change that lets a heading titled "NEXT foo" in a non-GTD file stay a
  title.
- **Views.** The agenda, then one file view per agenda file, labelled by its
  `#+title:` and in config order.
- **Capture.** Into `[notes] capture_file` (default `inbox.org`), with the
  plain `* TODO …` + `:CREATED:` entry used today.
- **Edits.** State, date and refile on any agenda file. The core refile
  targets are "the top level of any agenda file". `_gtd_path` becomes "is it
  an agenda file".
- **Repeaters, CLOSED stamps, the heading locator, search and read-aloud**
  stay exactly as they are. None of them were GTD-specific.

### 2. org-roam: in core, on when there is a roam directory

org-roam isn't part of paragtd. People use it on its own. It stays in core,
switched on by `[notes] roam_dir` (default `<root>/roam` if it exists). Its
shelves become its subfolders as found, not a fixed list of six. Keeping the
agent-session notes out of search moves to config (`[notes] search_exclude`);
paragtd's profile provides today's list as its default.

### 3. A notes profile: the seam

A new entry-point group, `agent_media.notes_profiles`, sits beside the
render engines in `extensions.py`. It's discovered the same way, and core
never imports a profile. A profile is an object with any of these
(everything is optional, and a missing piece falls back to the core
behaviour):

| Hook | What it supplies |
|---|---|
| `files()` | the agenda files, with a view name and label each (overrides config) |
| `keywords()` | the TODO keywords when a file doesn't declare its own |
| `capture_kinds()` | `[{name, label, file, headline?, template}]`, where the template is the Org text with `%?` for what was typed and `%U` for now |
| `refile_targets()` | `[{name, label, file, headline?, state?, needs_date?}]` |
| `agenda_filter(item, today)` | drop an item from the agenda (astro's stale rule) |
| `before_state(lines, i, old, new)` | refuse a change, raising `Refused` |
| `after_state(lines, i, old, new)` | further edits to the same file, made under the same lock |
| `setup_components()` | extra rows for `/notes/setup` |

Which profile is used: `[notes] profile = "paragtd"` names one. When the key
is absent, a profile that is installed **and** whose `detect(root)` says yes
is used. For paragtd that means `next-actions.org` or a manifest exists. So
the host where David has installed it keeps working as it does today with no
config, and someone with plain Org files gets the core behaviour.
`profile = "none"` turns detection off.

### 4. `packages/notes-paragtd`

This is a small Python package in this repo (Apache-2.0). It registers the
`paragtd` profile, and its contents are today's constants and rules moved out
of core:

- the ten files and their labels, the refile targets, the keyword set with
  SOMEDAY, the astro stale rule, the skeleton and setup rows;
- **reading the manifest** (below) when paragtd is installed, with the moved
  constants as the fallback when it isn't;
- **sequencing:**
  - `before_state` refuses DONE while an older sibling under an `:ORDERED: t`
    parent is still open. This matches `org-enforce-todo-dependencies`.
  - `after_state` applies the one trigger form paragtd writes,
    `next-sibling todo!(STATE) scheduled!("++Nd")`. It sets the next
    sibling's state and schedules it N days from now.
  - Any other org-edna trigger is left alone, and the response says so
    (`"trigger": "skipped"`), so the app can say "finish this in Emacs to
    run its trigger".
- **astro:** a `setup_components` row that enables a yearly or monthly timer
  running paragtd's `bin/paragtd-astro-generate` (the command it already
  has). The Python is paragtd's own, so nothing is imported.

paragtd is GPL v3, and this package stays Apache. It never imports or copies
paragtd's code. It reads a JSON file that paragtd writes, and runs paragtd's
command as a separate process. Sequencing is written again in Python from the
README's description of the behaviour, not translated from the elisp.

### 5. paragtd publishes a manifest

paragtd gets `bin/paragtd-export`, a batch Emacs run that loads the package
and the user's site settings and writes `<org-dir>/.paragtd.json`:

```json
{"version": 1,
 "org_directory": "~/org", "roam_directory": "~/org/roam",
 "files": ["inbox.org", "next-actions.org", …],
 "todo_keywords": [["TODO", "NEXT", "WAITING"], ["DONE", "CANCELLED"]],
 "capture": [{"key": "n", "label": "Next action", "file": "next-actions.org",
              "headline": "Inbox", "template": "* NEXT %?\n:PROPERTIES:\n…"}],
 "sequence_lag_days": 2,
 "astro": {"generator": "/home/…/bin/paragtd-astro-generate",
           "timezone": "Australia/Melbourne", "stale_days": 2}}
```

It sits in the notes tree, so org-autosync carries it to every host, and the
server reads it from there. Emacs doesn't need to be installed on a host
that runs the server. `paragtd-setup` rewrites it whenever the templates
change (one `after-init` call). Templates that prompt (`%^T`, `%^{…}`) are
exported with the prompt as a named field. The app shows one input per field.
Templates with elisp (`%(…)`) are left out.

### 6. The app draws what it's told

`GET /notes` gains three fields. Next uses them in place of its hard-coded
lists:

```json
{"profile": "paragtd" | null,
 "states": {"open": ["TODO", "NEXT", "WAITING", "SOMEDAY"], "done": ["DONE", "CANCELLED"]},
 "capture_kinds": [{"name", "label", "fields"?}],
 "refile_targets": [{"name", "label", "needs_date"?}]}
```

These replace `NoteState`, `RefileTarget`, the state row in `note.tsx`, the
tickler special case in `MoveSheet.tsx` (it becomes `needs_date`) and the
rank table in `noteSort.ts` (which ranks by position in `states.open`). An
older server sends none of the fields, and the app falls back to today's
lists. The "is it set up" test (`views` has `inbox`) becomes "is there a
capture file".

## Steps

1. **Split with no change in behaviour.** Create the profile seam and move
   the paragtd constants into `packages/notes-paragtd`. red5 installs it.
   `test_notes.py` and `test_notes_edit.py` pass unchanged with the package
   installed. A new core test runs the same tree with `profile = "none"`.
2. **Generic core.** Agenda files from config, `#+TODO:` parsing, capture
   file, roam shelves found on disk, and setup's plain-Org start ("point me
   at your files", or read them from Emacs).
3. **Contract and app.** The three new `/notes` fields and §6.10 updated;
   Next reads them.
4. **Manifest and sequencing.** `paragtd-export` in paragtd, the profile
   reads it, and `before_state` / `after_state` added.
5. **Astro timer.** The setup row and the timer unit.

Steps 1–3 are what makes the app usable by someone without paragtd. Steps
4–5 are what David asked for on the phone. Step 4 does not depend on 2–3, so
it can go second if the phone matters more this week.

## Questions (decided 24 Sep 2026, each as recommended)

1. **Where `notes-paragtd` lives.** It could be here in `packages/` (it's
   released and tested with the server, and Apache), or in the paragtd repo as
   a `python/` subdirectory (GPL, and it would move with the elisp).
   Recommended: **here**. The manifest is the interface between the two, and
   keeping them separate keeps the licence boundary clean.
2. **Reading `org-agenda-files` from a live Emacs.** It could be copied into
   the config once at setup, or read again at every server start. Recommended:
   **once, at setup**. The server has to run on hosts without Emacs, and a
   daemon that's down would otherwise mean an empty tab.
3. **The other org-edna triggers.** Leave them to Emacs (as above), or
   implement a larger subset. Recommended: **leave them**. paragtd writes only
   the one form, and anyone writing their own edna triggers has Emacs open.
