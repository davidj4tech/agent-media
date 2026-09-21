# Redesigning the phone app (21 Sep 2026)

David asked: replace the Audiobookshelf fork with something more modern, more
suited to agent chat, under a more liberal licence. These are the notes behind
the short answer.

## What we actually have

Sasonica = a fork of `advplyr/audiobookshelf-app`, GPL-3.0.

Measured against the merge base:

- **32 files added** by the fork (14 under `android/app`, the rest Vue/JS).
- **44 upstream files modified** (the hook-sized `// Sasonica:` blocks).
- `@capacitor/core` is already **^7.0.0** — the native shell is current.
- `nuxt` is **^2.15.7** — Vue 2, end-of-life since Dec 2023. This is the dead
  weight, and it is where nearly all the friction lives.

So the fork surface is small. A redesign is not years of work being binned.

## The real diagnosis

The pain is not the framework, it is the **data model**. We are describing
agent conversations as an audiobook library, and every recurring bug in memory
is that impedance mismatch:

- progress duration pinned at write time (a growing conversation kept its old
  length)
- `isFinished` sticking, needing two PATCHes
- `/series?limit=0` returning nothing
- ABS refusing tailnet URLs until `SSRF_REQUEST_FILTER_WHITELIST` named the host
- titles re-applied from ABS metadata on every publish
- `archived` as a tag, a project as a *series*, narrowing filters done client
  side because the server cannot know what a session is doing

agent-media already *is* the source of truth — sessions, clips, states, asks,
slash menus, targets. ABS sits in the middle re-describing it badly.

## What is worth keeping

- **The Java/Kotlin.** Audio focus policy, the foreground service that beats
  the freezer, exact 1.6x, the tokened tailnet listener on :8773, MicWatch,
  BookHold. This is the hard-won part and it is ours.
- **Capacitor 7.** Modern, MIT, and it is what the Java plugs into.
- **The agent-media endpoints.** `/sessions/state`, `/ask`, `/conversation`,
  `/targets`, the canvas. Already chat-shaped.

## What is worth dropping

- The Nuxt 2 / Vue 2 UI (all of it).
- The ABS server as the conversation store, and with it the library/series/tag
  contortions.
- The GPL-3.0 obligation — which leaves with the ABS-derived code, not before.
  Note the player service is upstream ABS code; a clean exit means our own
  Media3 service (Apache-2.0), which the MediaPlayer spike already proved.

## Recommendation

**Do not fork another app.** Keep the Capacitor 7 shell and the Java, replace
the web layer with a chat-first front end that talks to agent-media directly.
Liberal licence comes free, because the thing we delete is the GPL part.

Do it as a strangler, not a big bang: new front end in the same app behind its
own entry point, screen by screen, while the Java keeps working. Sasonica only
started playing speech on 19 Sep — parking that for a rewrite would be a poor
trade.

## Candidates, with licences

Native shell (recommended first, alternatives after):

| Option | Licence | Note |
| --- | --- | --- |
| **Capacitor 7** (stay) | MIT | Keeps every Java plugin as-is |
| Expo / React Native | MIT | Bigger ecosystem; the plugin bridge is a rewrite |
| Flutter | BSD-3 | Good audio story (`just_audio`, `audio_service`, MIT) |
| Kotlin + Compose | Apache-2.0 | Most control, least reuse, no web sharing |

Front end to build with / borrow from:

- **assistant-ui** (MIT) — React chat primitives: message list, streaming,
  tool calls, approvals. A component kit, not an app. Closest to what we want.
- **Vercel `ai-chatbot`** (MIT) — a reference implementation to read.
- **LibreChat** (MIT), **LobeChat** (Apache-2.0, verify) — full chat apps worth
  studying, but both are coupled to talking to model providers, which we are
  not: our server drives tmux sessions.
- **Open WebUI** — licence is now BSD-3 *with a branding restriction*; not the
  liberal option it used to be. Verify before relying on it.

Audio: **Media3 / ExoPlayer** (Apache-2.0) directly, which is already the
primary book player.

Optional: **Matrix** (`matrix-rust-sdk`, Apache-2.0) as the transport would
give push, multi-device and threads for free — and there is already a Matrix
server on red5. Weigh it against the tailnet plus tokened listener we already
have working.

## The honest cost

Dropping the ABS server means rebuilding: offline downloads with resume,
cross-device progress sync, auth, and library browse. For an audiobook library
that is a lot. For a few hundred conversations whose truth already lives in
agent-media, it is much less — but it is not nothing, and downloads-with-resume
is the one that will be missed.
