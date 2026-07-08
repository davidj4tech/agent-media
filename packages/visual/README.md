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
| `MEDIA_VISUAL_URL` | `http://127.0.0.1:8781` | where `media-visual` pushes (set to the tailnet URL when the canvas binds tailnet-only) |
| `MEDIA_VISUAL_SHAPE_MODEL` / `_SHAPE_TIMEOUT` | summary model / timeout | prompt-shaping overrides |
| `MEDIA_VISUAL_MODEL` | `z-image-turbo` | Venice image model (fast > pretty) |
| `MEDIA_VISUAL_STYLE` | cinematic digital painting… | style suffix, one visual voice |
| `MEDIA_VISUAL_SIZE` | `1024x1024` | canvas cover-crops, square splits the difference |
| `MEDIA_VISUAL_TIMEOUT` | `90` | image request timeout (s) |
| `MEDIA_VISUAL_DEBUG` | off | `1` logs canvas requests |

## TODO (post-spike)

- Session continuity ("evolve the current image" prompting) instead of a
  slideshow of unrelated pictures.
- An `agent_media.visual_engines` entry-point group mirroring the TTS
  render-engine registry (Venice is hardcoded here).
- Spool GC, multi-canvas targets.
