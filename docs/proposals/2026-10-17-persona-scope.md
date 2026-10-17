# Persona scope: by agent, by project, and private mode on trusted models

David, 17 Oct 2026, after the new-chat sheet's lock (4ca4dcb, 459ef32):
personas should be **restricted by agent**, **sorted by project** (a
project's own personas first, the rest still there), and a private persona
should get its **private render only on a trusted model**, its public shell
anywhere else. Each of these takes **defaults, a host's local settings and
the user's own**, layered. Status: proposal. Follows
2026-10-15-personas-in-presets.md, whose steps 3–4 private mode depends on.

## What there is

- A persona's front matter can already carry `render.compatible_adapters`
  (persona.schema.json). Only the three roles fill it in, and nothing reads
  it: `persona-render` ignores it, and `--list` prints bare ids.
- Nothing ties a persona to a project, or to a model.
- `/presets` lists `personas`, `roles` and `private` (ids with a
  `private/<id>…sops.md`); `/ask` always renders `--mode public`.
- red5 has `sops`, `age` and the age key; `persona-render --mode private`
  decrypts.

## The three layers

Each setting is read from three places; the later one wins, field by field:

| Layer | Where | Who edits it |
| --- | --- | --- |
| default | the persona's front matter in agent-personas; built-in lists in agent-media | the repo, versioned |
| local | `~/.config/agent-media/personas.toml` on the host | by hand, per machine |
| user | `<state_dir>/persona-scope.json`, through `POST /personas/scope` | the app's Settings → Personas |

A field left unset in every layer means "no limit", so nothing changes until
something is tagged.

## 1. By agent (a hard limit)

Front matter `render.compatible_adapters: [claude, pi, …]` (the existing
field); local and user layers may set `agents` per id. `persona-render
--list --json` (new, in agent-personas) prints each id with its kind,
adapters, projects and whether it has private material, so agent-media
stops guessing from file names.

- `/presets` gains `scope: {"<id>": {"agents": [...], "projects": [...],
  "private": bool}}` — the merged result.
- The app's Persona and Role rows offer only what the chosen agent takes;
  picking an agent that drops the current persona clears it and says so.
- `/ask` with a persona the agent does not take → 400, as an unknown id is.

## 2. By project (a soft order)

Front matter `projects: [agent-media, sasonica-app]` (folder names, or
paths; a worktree counts as its project). On the card a project's own
personas come first, under a thin rule, the rest after it — none hidden.
The lock still hides private ones wherever they sort.

## 3. Private mode on trusted models

**Trusted models**, layered the same way:

- default: `venice/*` (no-log provider) and `local/*` / `ollama/*` (served
  on David's own hosts);
- local: `trusted_models = [...]` in personas.toml;
- user: the list in Settings → Personas.

Glob patterns on the `/ask` model id (pi's `provider/model`; Claude's
aliases are never trusted by default).

A private persona on a trusted model renders `--mode private`; on any other
it renders `--mode public`, as today. The card says which, next to the
Persona row ("private" / "public shell").

**Gate.** Private mode is not switched on until steps 3–4 of the
2026-10-15 proposal are built: the private mark that keeps a live thread out
of search, shelf/ABS, the org bridge, the hourly memory sync and
fleet-sync, and the auto-seal on end or idle. Without them the decrypted
render and the conversation reach the long-term memory store within the
hour. Until then a trusted model still gets the public shell, and the card
says "private mode not available on this host".

## Order

1. `persona-render --list --json` (agent-personas).
2. The layers: personas.toml and persona-scope.json read and merged; `scope`
   and `trusted_models` on `/presets`; `POST /personas/scope`.
3. App: agent filter and project order on the card; Settings → Personas
   (per persona: agents, projects; the trusted-model list).
4. The private mark and auto-seal (2026-10-15 steps 3–4).
5. Private renders on trusted models, behind 4.

## To decide

- Whether a user-layer edit can **widen** a default (give zara to Codex), or
  only narrow it. Proposed: either way; it is David's own setting.
- Whether Claude via a no-retention arrangement ever counts as trusted.
