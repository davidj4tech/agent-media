# Decision: keep /speech and speech-state.service separate

2026-08-05. Question raised after the /speech breadcrumb peek landed on the
visual-canvas server (a5a1c94): should it and the pre-existing
`speech-state.service` converge into one speech-state service that both the
remote duckers and the peek read from?

**Verdict: no convergence. Two endpoints, two contracts. Version the ducker
script; optionally mirror its probe as a field in /speech for new consumers.**

## (a) The two services today

| | canvas `GET /speech` | `speech-state.service` `GET /speech` |
|---|---|---|
| Where | `agent-media-visual-canvas`, `100.103.43.93:8781` | `~/.local/bin/speech-state-server.py`, `100.94.154.59:8675` |
| Source | state.db `now_playing` + `media popup-status` subprocess + `speech-events.jsonl` | local sink-speech mpv socket, `core-idle` property |
| Question answered | "what is the speech *system* doing" — session, sentence, position, start/end breadcrumbs; sees phone playback | "is local audio *physically* playing" — a single bool |
| Failure direction | fail-informative (rich state, heavier deps) | **fail-open**: can't tell ⇒ `playing: false`, so a remote video never sticks paused |
| Consumers | voice-mode Claude via the tmux-relay fast lane; canvas page | remote duckers (hpo SMTC helper), URL/port baked in on other hosts |
| Cost per poll | `media` subprocess (~hundreds of ms) | 0.5s-capped unix-socket property read (~ms) |

Crucial asymmetry: phone speech travels over TCP to the phone's mpv and never
touches the local sink socket, so the ducker probe cannot see it — and must
not: nothing is audible locally, so nothing local should duck.

## (b) Cost of converging

Code is the cheap part (~a day). The real costs: the ducker URL, port and bind
IP are baked into clients on other machines, so a merged service either keeps
a second listener answering `100.94.154.59:8675` with the exact
`{"playing": bool}` shape — at which point the "single service" is mostly
illusory — or remote hosts get touched. And duckers poll tightly against a
millisecond budget; the canvas path spawns a subprocess per call, ~100×
heavier.

## (c) Risk to ducker behaviour

1. **Semantics** — a `now_playing`-derived signal includes phone playback and
   spans gaps the mpv probe correctly reports silent: duckers would pause
   local media while only the phone talks.
2. **Failure direction flips** — a stale `now_playing` row or wedged canvas
   fails *closed* (stuck "speaking" ⇒ stuck-paused video), exactly the failure
   the tiny script's fail-open design exists to prevent.
3. **Availability coupling** — the canvas is the largest, most-edited server
   in the stack and restarts often during development; the ducker script has
   near-zero reasons to crash. A safety-behaviour poller should not inherit
   that churn.

## (d) Recommendation

Keep both endpoints as deliberate, separate contracts. Do the two tidy-ups
that are actually worth having:

- ~~Move `speech-state-server.py` + its systemd unit into this repo — it is
  load-bearing and currently unversioned in `~/.local/bin`.~~ **CORRECTED
  2026-08-05: not a gap.** Both are already versioned in the dotfiles repo
  (`~/dotfiles/packages/voice/`), stow-symlinked into `~/.local/bin` and
  `~/.config/systemd/user/`. The original claim followed the path but not the
  symlink. No move needed; leave them in dotfiles where the rest of the voice
  package lives.
- If one URL should serve both truths for *new* consumers, add the ducker's
  one-line `core-idle` probe as a `"local_audio": bool` field in the canvas
  `/speech` response (cheap unix-socket read, no subprocess). Leave `:8675`
  untouched for existing clients.

## (e) What would trigger revisiting

- The ducker fleet grows or gets a config-distribution story, making a client
  URL migration cheap.
- The canvas is split so a small, stable "state" server exists apart from the
  big page server — then the fail-open probe could live there without
  inheriting canvas churn.
- Duckers start needing session-level context (e.g. "duck only for rooms
  speech, not phone"), which the mpv probe cannot express.
- `speech_state()` stops shelling out to `media popup-status`, closing the
  per-poll cost gap.
