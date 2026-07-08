# agent-media-visual — SPIKE

Status: **prototype spike** (2026-07-08). A visual channel alongside TTS:
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
  stream (`/events`), the image spool (`/img/<f>`), and a push endpoint
  (`POST /show`). Default port **8781** (the speech clip server is 8780).
- `media-visual "text"` — shapes the reply into an image prompt (one chat
  call to the same gateway the summary/describe path uses, fallback: raw
  text), generates via **Venice** `/image/generate`, spools the webp, and
  pushes it to the canvas. `--say` also fires `media say` first, detached,
  for the full speak-and-show demo.

Keys: Venice key is read from `VENICE_API_KEY`, else
`~/.config/litellm/litellm.env` — the same file the gateway reads, so
nothing new to configure.

## Run

```sh
media-visual-canvas &                 # on red5 (the box the phone already
                                      # fetches speech clips from)
# phone/TV browser → http://100.103.43.93:8781/
media-visual --say "All tests pass and the branch is merged."
```

## Config (env / ~/.config/agent-media.env)

| var | default | |
|---|---|---|
| `MEDIA_VISUAL_PORT` / `MEDIA_VISUAL_BIND` | `8781` / `0.0.0.0` | canvas listen |
| `MEDIA_VISUAL_URL` | `http://127.0.0.1:8781` | where `media-visual` pushes |
| `MEDIA_VISUAL_MODEL` | `z-image-turbo` | Venice image model (fast > pretty) |
| `MEDIA_VISUAL_STYLE` | cinematic digital painting… | style suffix, one visual voice |
| `MEDIA_VISUAL_SIZE` | `1024x1024` | canvas cover-crops, square splits the difference |
| `MEDIA_VISUAL_TIMEOUT` | `90` | image request timeout (s) |
| `MEDIA_VISUAL_DEBUG` | off | `1` logs canvas requests |

## Out of scope for the spike (the real feature's TODO)

- Hook wiring: fork an image job from the Stop-hook's detached child (where
  describe already runs) so every spoken reply shows automatically — gated
  like `MEDIA_SPEECH_SUMMARY` (opt-in env, min-length threshold, one image
  per reply, not per block).
- Session continuity ("evolve the current image" prompting) instead of a
  slideshow of unrelated pictures.
- An `agent_media.visual_engines` entry-point group mirroring the TTS
  render-engine registry (Venice is hardcoded here).
- Spool GC, a runit/systemd service for the canvas, multi-canvas targets.
