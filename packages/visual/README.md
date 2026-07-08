# agent-media-visual

A visual channel alongside TTS:
each spoken reply gets a generated image that cross-fades onto a full-bleed
web canvas any screen can show — phone browser, tablet, TV in kiosk mode.

Design premises (from the discussion that spawned this):

- **Latency is embraced, not fought.** Speech starts in ~1s; a fast image
  model takes 3–15s. The image fades in *mid-utterance*, like album art —
  it is never something speech waits on.
- **Animation is client-side.** Ken Burns pan/zoom + cross-fades on the
  canvas make stills feel alive at zero generation cost. Real video gen is
  a later engine slot, not a spike concern.
- **One canvas, every screen.** A single SSE-driven page instead of
  per-device integrations. A screen that's off just misses the show.

## Pieces

- `media-visual-canvas` — stdlib HTTP server: the canvas page (`/`), an SSE
  stream (`/events`), the image spool (`/img/<f>`), a push endpoint
  (`POST /show`), and the audio-controller backend (`GET /status`,
  `POST /ctl`). Default port **8781** (the speech clip server is 8780).
- **Audio-reactive motion + sound cues** — while the voice is talking the
  scene *moves with it*: the Ken Burns pan runs ~2.6× and a soft vignette
  breathes; both settle when speech stops. A canvas-side poller (runs only
  while screens are connected, ~1 Hz `media popup-status` — the popup's own
  cadence) broadcasts `{"kind":"state", speaking, pos, dur}` over the SSE
  stream. Synthesized sound effects (WebAudio, no assets): a whoosh when a
  new image lands, a chime up/down when speech starts/stops — toggled with
  the 🔈 button, persisted per device.
- **Audio controller** — tap the canvas to reveal a touch version of the
  tmux popup (`prefix a`): channel cycle (♪ speech ⇆ ♫ music ⇆ ☰ book),
  marquee title, live clock, `⏮ ▶/‖ ⏭ − +`, and on speech mute + speed.
  Every button runs the same `media` CLI verb the popup's hotkey runs — one
  code path — including the popup's ⏮ replay-cursor semantics on speech.
  Auto-hides after 12s.
- `media-visual "text"` — shapes the reply into an image prompt (one chat
  call to the same gateway the summary/describe path uses, fallback: raw
  text), generates it, spools the webp, GCs the spool, and pushes to every
  configured canvas. `--say` also fires `media say` first, detached, for
  the full speak-and-show demo.
- **Scene continuity** — with `--session <id>` (the Stop hook passes its
  Claude session id), consecutive replies *evolve one artwork*: the shaper
  is given the previous scene and asked to change what the reply changes,
  keeping setting/palette/subject. The scene memory lives in the spool's
  `scenes.json` with a TTL, so a fresh session (or a long gap) starts a
  fresh scene. Disable with `MEDIA_VISUAL_CONTINUITY=0`.
- **Pluggable engines** — image backends register under the
  `agent_media.visual_engines` entry-point group (mirrors core's render
  engines; see [`docs/EXTENSIONS.md`](../../docs/EXTENSIONS.md)). Two
  built-ins: `venice` (raster, the default) and `svg` — animated clip-art
  emitted directly by the gateway LLM (SMIL loops play inside `<img>`;
  validated: well-formed, no scripts/external refs). Select with
  `MEDIA_VISUAL_ENGINE`; failures fall back to
  `MEDIA_VISUAL_FALLBACK_ENGINE` (default venice).
- **Purposeful visuals & the reveal** — a picture that *says* something
  instead of decorating. The reply's author writes an inline marker:
  `[[visual: description]]` makes the description the image spec (a figure,
  drawn to communicate — the svg engine switches to its labeled-diagram
  prompt, since vector text renders crisply); `[[reveal: description]]`
  additionally **holds the voice at that exact point** until the canvas
  confirms the image is up (`GET /last`, bounded by
  `MEDIA_VISUAL_REVEAL_TIMEOUT` so a hung generator never mutes a reply) —
  the presenter waiting for the slide. Markers are always stripped from
  speech; a marker also bypasses the min-length gate. No marker → ambient
  pretty pictures, as ever.
- **Beats** — a multi-part reply becomes a synced *sequence*: one gateway
  call shapes the scene AND storyboards it across up to 4 parts, the
  images generate concurrently, and the canvas flips between them as the
  voice crosses each part's time fraction (estimated from character
  count), parking on the conclusion when speech ends. Short replies whose
  generation outlasts the speech land directly on the conclusion. Off with
  `--no-beats` / `MEDIA_VISUAL_BEATS=0`.

Keys: Venice key is read from `VENICE_API_KEY`, else
`~/.config/litellm/litellm.env` — the same file the gateway reads, so
nothing new to configure.

## Run

The canvas runs as a systemd user service (see `systemd/`), bound to the
Tailscale IP only — same privacy posture as the clip server on 8780:

```sh
cp systemd/agent-media-visual-canvas.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-media-visual-canvas
# phone/TV browser → http://red5:8781/ (MagicDNS) or http://100.103.43.93:8781/
media-visual --say "All tests pass and the branch is merged."   # manual demo
```

### Stop-hook wiring (automatic pictures)

With `MEDIA_SPEECH_VISUAL=1` in `~/.config/agent-media.env`, the Claude Code
Stop hook illustrates every spoken reply automatically: the detached playback
child hands the raw reply to `media-visual` fire-and-forget (see core
`intake/_visual.py`), so image generation runs concurrently with the
summary/describe rewrites and with playback. Gated on reply length
(`MEDIA_VISUAL_MIN_CHARS`, default 320) and skipped for deduped replies; a
missing `media-visual` binary makes it a silent no-op, so core stays
decoupled from this package.

## Config (env / ~/.config/agent-media.env)

| var | default | |
|---|---|---|
| `MEDIA_SPEECH_VISUAL` | off | `1` = Stop hook illustrates spoken replies |
| `MEDIA_VISUAL_MIN_CHARS` | `320` | only illustrate replies at least this long |
| `MEDIA_VISUAL_PORT` / `MEDIA_VISUAL_BIND` | `8781` / `0.0.0.0` | canvas listen (the service passes `--bind <tailscale-ip>`) |
| `MEDIA_VISUAL_URL` | `http://127.0.0.1:8781` | canvas(es) to push to — space/comma-separated for several; with multiple, images are referenced via the FIRST target's absolute `/img/` URL, so make it the tailnet URL, reachable from every screen |
| `MEDIA_VISUAL_ENGINE` | `venice` | image backend (entry-point group `agent_media.visual_engines`) |
| `MEDIA_VISUAL_FALLBACK_ENGINE` | `venice` | tried when the primary engine fails |
| `MEDIA_VISUAL_CONTINUITY` | on | `0` = every reply is a fresh scene |
| `MEDIA_VISUAL_CONTINUITY_TTL` | `7200` | seconds a session's scene stays alive |
| `MEDIA_VISUAL_SPOOL_KEEP` | `200` | newest images kept by the post-push GC |
| `MEDIA_VISUAL_SHAPE_MODEL` / `_SHAPE_TIMEOUT` | summary model / timeout | prompt-shaping overrides — a fast model (e.g. haiku) matters: prompts must beat the speech they illustrate |
| `MEDIA_VISUAL_BEATS` / `_BEATS_MAX` | on / `4` | `0` disables the synced sequence; max parts per reply |
| `MEDIA_VISUAL_BEATS_ENGINE` | the normal engine | engine for beat images only — pair a slow single-image engine (svg) with a fast one (venice) so sequences stay synced |
| `MEDIA_VISUAL_CHARS_PER_SEC` | `14` | spoken-duration estimate driving beat pacing |
| `MEDIA_VISUAL_SVG_MODEL` / `_SVG_TIMEOUT` | shape model / image timeout | svg engine model (haiku draws far better than a small local model) |
| `MEDIA_VISUAL_REVEAL_TIMEOUT` | `75` | max seconds speech holds at a `[[reveal:]]` before continuing without the picture |
| `MEDIA_VISUAL_MODEL_VENICE` | `z-image-turbo` | venice image model (fast > pretty; `MEDIA_VISUAL_MODEL` also honoured) |
| `MEDIA_VISUAL_STYLE` | cinematic digital painting… | style suffix, one visual voice |
| `MEDIA_VISUAL_SIZE` | `1024x1024` | canvas cover-crops, square splits the difference |
| `MEDIA_VISUAL_TIMEOUT` | `90` | image request timeout (s) |
| `MEDIA_VISUAL_DEBUG` | off | `1` logs canvas requests |

## TODO

- Image-to-image continuity (the *composition* persists via the evolved
  prompt, but character/visual identity still drifts between generations).
- Per-canvas routing (different sessions → different screens).
