# agent-media-engine-espeak

A **working example** of an agent-media render-engine extension: offline
text-to-speech via [`espeak-ng`](https://github.com/espeak-ng/espeak-ng).

It exists to show the `agent_media.render_engines` contract end to end. Copy it
to build a real engine (Piper, Coqui, ElevenLabs, …).

## Install

```bash
sudo apt install espeak-ng        # or: brew install espeak-ng
pip install ./examples/agent-media-engine-espeak   # alongside agent-media-core
```

## Use

```bash
MEDIA_RENDER_ENGINE=espeak media say "hello from espeak"
# or per-call: render_text(text, outfile, engine="espeak")
```

Override the voice with `MEDIA_RENDER_VOICE_ESPEAK=en-us` (or any espeak voice).

## The contract

The entire integration is one entry point in `pyproject.toml`:

```toml
[project.entry-points."agent_media.render_engines"]
espeak = "agent_media_engine_espeak:render"
```

…pointing at a function with the signature:

```python
def render(text: str, outfile: Path, *, voice: str | None = None) -> tuple[bool, str]:
    ...  # write audio to outfile; return (ok, error_message)
```

agent-media-core discovers it at runtime via Python entry points and **never
imports this package directly**. See [`docs/EXTENSIONS.md`](../../docs/EXTENSIONS.md)
in the core repo for the full contract.
