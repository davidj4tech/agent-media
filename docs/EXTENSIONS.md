# Extending agent-media

agent-media is a small **core** plus optional, independently-installable
pieces. Core never imports an extension directly — it discovers them at runtime
via Python [entry points](https://packaging.python.org/en/latest/specifications/entry-points/),
so a `pip install` is all it takes to add behaviour, and uninstalling cleanly
removes it.

There are three seams, in decreasing order of how plug-and-play they are.

---

## 1. Render engines — fully pluggable (entry points)

A render engine turns text into an audio file. Core ships four built-ins
(`edge`, `openai`, `qwen`, `realtime`); anyone can add more.

**The contract** is one callable:

```python
from pathlib import Path

def render(text: str, outfile: Path, *, voice: str | None = None) -> tuple[bool, str]:
    """Render `text` to `outfile`. Return (ok, error_message)."""
```

- Write a WAV/MP3 to `outfile`, return `(True, "")`.
- On failure return `(False, "<why>")`. Core logs it and falls back to `edge`
  (unless the caller disabled fallback).
- `voice` is the caller's requested voice, or `None` → use your own default.
- Read all other config (model, API key, base URL) from `os.environ` yourself.
  Core passes none of it, so engines stay self-describing and core stays
  decoupled. Convention: `MEDIA_RENDER_VOICE_<ENGINE>` for the default voice.

**Register it** in your package's `pyproject.toml`:

```toml
[project.entry-points."agent_media.render_engines"]
myengine = "my_package.module:render"
```

**Use it:**

```bash
pip install my-package           # alongside agent-media-core
MEDIA_RENDER_ENGINE=myengine media say "hello"
```

Rules core enforces (see `agent_media_core/extensions.py`):

- An extension **may not shadow a built-in** name (`edge`/`openai`/`qwen`/
  `realtime`) — the collision is logged and the built-in wins.
- A broken extension (import error, not callable, raises at render time) is
  logged and skipped/failed-over, never fatal.
- Discovery is cached for the process; engines are resolved on first use.

**Working example:** [`examples/agent-media-engine-espeak`](../examples/agent-media-engine-espeak)
is a complete espeak-ng engine you can install and copy.

---

## 2. Intake adapters — pluggable at the process level (console scripts)

An intake adapter is anything that produces an `Event` and calls
`agent_media_core.intake.submit`. Core's own adapters are already separate
console-script entry points:

```
media-hook-claude-code   media-hook-codex   media-hook-pi   media-hook-pi-stream
media-intake-ha-sse      media-intake-matrix
```

To add a new source (a different chat app, a webhook, a CLI), ship **your own
console script** that builds an `Event` and submits it:

```toml
[project.scripts]
media-intake-myapp = "my_package.intake:main"
```

```python
from agent_media_core.intake import submit
from agent_media_core.types import Event, Source, Priority

def main() -> int:
    submit(Event(text="...", source=Source.CLI, priority=Priority.NORMAL, ...))
    return 0
```

Nothing in core needs to change — core never imports intake adapters to use
them, they drive core. This is a documented convention rather than a discovery
mechanism; the process boundary *is* the isolation.

---

## 3. Sinks (speech / music / book) — stable core, not an extension seam

The three output channels are core identity: speech and book are mpv-over-IPC,
music is Mopidy/MPD, and the route coordinator's duck-vs-pause policy is written
against exactly these. They are intentionally **not** a third-party seam today.
If a fourth channel ever earns its place, it belongs in core, not a plugin.

---

## Roadmap

Engines are the proven seam (this document + the espeak example). The natural
next step is to move core's optional engines (`openai`, `qwen`, `realtime`) out
into their own packages using this same contract, leaving core shipping only
the zero-config `edge` engine. Track that under the monorepo's overhaul plan.
