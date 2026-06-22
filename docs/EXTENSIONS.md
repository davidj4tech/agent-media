# Extending agent-media

agent-media is a small **core** plus optional, independently-installable
pieces. Core never imports an extension directly — it discovers them at runtime
via Python [entry points](https://packaging.python.org/en/latest/specifications/entry-points/),
so a `pip install` is all it takes to add behaviour, and uninstalling cleanly
removes it.

There are three seams, in decreasing order of how plug-and-play they are.

---

## 1. Render engines — fully pluggable (entry points)

A render engine turns text into an audio file. Core ships exactly one built-in
— `edge`, which is zero-config (no API key) and the universal default.
Everything else is an installable plugin, including the ones maintained in this
repo: `openai` (`agent-media-engine-openai`), `qwen`
(`agent-media-engine-qwen`), and `realtime` (`agent-media-engine-realtime`).
Anyone can add more the same way.

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

- An extension **may not shadow a built-in** name (`edge`) — the collision is
  logged and the built-in wins.
- A broken extension (import error, not callable, raises at render time) is
  logged and skipped/failed-over, never fatal.
- Discovery is cached for the process; engines are resolved on first use.

**Working example:** [`examples/agent-media-engine-espeak`](../examples/agent-media-engine-espeak)
is a complete espeak-ng engine you can install and copy.

---

## 2. Intake adapters — pluggable at the process level (console scripts)

An intake adapter is anything that produces an `Event` and calls
`agent_media_core.intake.submit_event`. Each adapter is a console-script entry
point — the process boundary *is* the isolation, so core never imports an
adapter to use it; the adapter drives core.

Core bundles only the hooks that are part of its identity
(`media-hook-claude-code`, `media-hook-pi`, `media-hook-pi-stream`). The
optional adapters live in their own packages (`packages/intake-*`), each
depending on `agent-media-core` and reusing the same console-script name:

```
agent-media-intake-matrix  -> media-intake-matrix
agent-media-intake-ha      -> media-intake-ha-sse
agent-media-intake-codex   -> media-hook-codex
```

To add a new source (a different chat app, a webhook, a CLI), do the same —
ship a package with a console script that builds an `Event` and submits it,
using core's stable public intake surface (`submit_event`, `strip_markdown`,
`run_hook_stdin`):

```toml
[project]
dependencies = ["agent-media-core"]
[project.scripts]
media-intake-myapp = "my_package:main"
```

```python
from agent_media_core.intake import submit_event
from agent_media_core.types import Event, Source, Priority

def main() -> int:
    submit_event(Event(text="...", source=Source.CLI, priority=Priority.NORMAL, ...))
    return 0
```

---

## 3. Sinks (speech / music / book) — stable core, not an extension seam

The three output channels are core identity: speech and book are mpv-over-IPC,
music is Mopidy/MPD, and the route coordinator's duck-vs-pause policy is written
against exactly these. They are intentionally **not** a third-party seam today.
If a fourth channel ever earns its place, it belongs in core, not a plugin.

---

## Roadmap

Render engines (`packages/engine-*`) and optional intake adapters
(`packages/intake-*`) both now live outside core, wired through the contracts
above — core ships only `edge` plus its identity hooks. The remaining slimming
target is the two large CLI/pipeline modules (`cli.py`, `intake/submit.py`),
tracked under the monorepo's overhaul plan; those are internal refactors, not
extension seams.
