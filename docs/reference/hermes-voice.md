# Hermes Agent voice — the three lanes and how to run them together

Status: **built + live** (the intake hook), with a coordination guide for the
other two paths. Companion to `packages/intake-hermes/`.

[Hermes Agent](https://hermes-agent.nousresearch.com) can speak through this
stack three different ways. They are *not* redundant — each owns a distinct
job — but any two of them will **double-speak the same turn** if you let them
both voice the same surface. This note is the arbitration rule.

## The three lanes

| Lane | Trigger | Audio lands | Owns |
|---|---|---|---|
| **intake hook** (`packages/intake-hermes`) | every turn, automatic (`post_llm_call`) | the house (Snapcast rooms) | ambient "Hermes always talks out loud" |
| **Hermes `text_to_speech` tool** | the model *decides* to call it | a file / per-device (voice bubble on messaging platforms) | on-demand, or the surface the hook can't reach |
| **agent-media `tts-shim`** (`packages/tts-shim`) | an external front-end POSTs `/v1/audio/speech` | the *caller's* device/tab | lending agent-media's voice to OWUI Call / SillyTavern |

```
intake hook   →  every turn (auto)   →  room-routed house voice   ← the default
tts tool      →  model chooses       →  file / platform bubble    ← remote surfaces
tts-shim      →  external caller asks →  that caller's device      ← OWUI / SillyTavern
```

## The collision (confirmed, not theoretical)

With the intake hook live, *every* Hermes turn is already voiced room-routed.
If the model *also* calls `text_to_speech`, that same turn is voiced twice.
Observed directly: a turn where the model called the tts tool produced both a
tts-tool file **and** a `source=hermes` room-audio history row for the reply.

On the CLI the tts tool only writes a file (no second live player), so the
practical clash there is mild. On a **messaging platform** the tool emits a
real voice bubble that genuinely collides with the hook — that is the case to
coordinate.

## The arbitration rule

> **One question per turn: who owns this turn's voice?**
> Resolved by (a) which surface the turn is on, and (b) agent-media's mute policy.

Clean separation:

- **Room / ambient →** intake hook. Default. Leave it on.
- **A specific device or messaging platform →** the tts tool, *only where the
  hook doesn't reach*. Keep the model from calling it for ordinary room turns.
- **A conversational front-end (OWUI Call, SillyTavern) →** tts-shim, with the
  driven session **muted** so its hook records history but stays silent.

## The levers (all already in agent-media / Hermes — nothing new to build)

### 1. Stop the tts tool from double-speaking room turns

Pick one:

- **Prompt policy (lightest).** Add to the profile's `.hermes.md` or `SOUL.md`:
  > Do not call `text_to_speech` for normal replies — the room already speaks
  > them via agent-media. Only use it when explicitly asked, or when replying
  > on a remote platform (Telegram/Discord/etc.) where the room voice can't be
  > heard.
- **Hard guard (deterministic).** A `pre_tool_call` shell hook that returns
  `{"decision":"block","reason":"room already speaks this turn"}` for
  `text_to_speech` when the session is one the intake hook covers. Wire it the
  same way as `intake-hermes` (see `packages/intake-hermes/README.md`), matcher
  `text_to_speech`.
- **Or just disable it:** `hermes tools disable tts` (per profile) if you never
  want on-demand tool speech at all.

### 2. Shim + hook handshake (the OWUI Call-mode case)

When OWUI drives a *live* Hermes session and that session's hook also speaks,
you get two voices. agent-media's answer is to **mute the driven pane** so its
hook renders + records but stays silent, and let the shim voice it on the
device:

- `MEDIA_COMPLETIONS_MUTE=1` (completions-shim default) runs
  `media mute-pane --pane <p> on` for you — dedicate a session to OWUI.
- Keep the shim's `MEDIA_SHIM_CANVAS` **off** so the session's own hook owns the
  canvas figure (else you get two).

See `packages/completions-shim/README.md` and `packages/tts-shim/README.md`.

### 3. Per-surface routing (already expressible)

The intake hook tags each event with its source tmux session/pane, and the
mute policy is pane- and session-scoped (`state/store.py::resolve_mute`):

```bash
media mute-pane --pane %NN on     # silence one pane's hook (still records history)
media mute-pane --subject toggle  # popup 'M' key — toggle the current subject
```

So "the office session speaks, the bedroom session is silent" needs no code —
just a mute override. A muted pane still renders its clip (replayable) and
still records history; it just doesn't play live or duck music.

## Quick recipes

| You want… | Do this |
|---|---|
| House speaks every reply (current) | nothing — intake hook is live |
| No accidental double-speak locally | prompt policy in `.hermes.md` (lever 1) |
| Voice bubbles on Telegram, silence in the room | keep `text_to_speech` for platform turns; prompt-scope it off for room turns |
| OWUI Call mode with agent-media's voice | tts-shim as OWUI's TTS + `MEDIA_COMPLETIONS_MUTE=1` on the driven session |
| One session silent | `media mute-pane --pane %NN on` |

## Where each piece lives

- intake hook: `packages/intake-hermes/` → `media-hook-hermes`, wired as a
  `post_llm_call` shell hook in the Hermes profile's `config.yaml`.
- tts tool: Hermes built-in; `tts.provider` in the profile `config.yaml`
  (currently `edge`).
- tts-shim / completions-shim: `packages/tts-shim/`, `packages/completions-shim/`.
- mute policy: `packages/core/src/agent_media_core/state/store.py`.
