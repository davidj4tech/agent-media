# agent-media-intake-hermes

[Hermes Agent](https://hermes-agent.nousresearch.com) intake for
[agent-media](https://github.com/davidj4tech/agent-media): speaks Hermes's
final turn output aloud through agent-media's render + room-routing stack —
the Hermes counterpart of the Claude Code / Codex intake hooks.

Unlike the raw stdin-pipe hooks (codex/pi), Hermes speaks a **JSON wire
protocol** on its shell hooks: a payload arrives on stdin, an optional
directive is read back on stdout. This adapter parses that payload, pulls the
assistant reply, strips markdown, and hands it to core's `submit_event`.

```bash
pip install -e packages/intake-hermes    # pulls in agent-media-core
```

## Wire it up

Add to `~/.hermes/config.yaml` (or the active profile's config):

```yaml
hooks:
  post_llm_call:
    - command: "media-hook-hermes"
      timeout: 15
```

`post_llm_call` fires once per turn, after the tool-calling loop completes,
and carries the assistant's final reply (`assistant_response`). It fires in
**both CLI and gateway** sessions. First use prompts for consent per
`(event, command)` pair unless `hooks_auto_accept: true`.

Then reload: exit and relaunch the CLI, or `hermes gateway restart`.

Playback is **detached** (fork + `setsid`) so the hook returns to Hermes
immediately and the speech outlives the short hook timeout — the same
technique the Claude Code Stop hook uses.

## Config

Checked before the generic `MEDIA_RENDER_*` vars:

| Env | Meaning |
|-----|---------|
| `HERMES_TTS_ENABLED=0` | disable this hook |
| `HERMES_TTS_ENGINE=<name>` | force a render engine for Hermes turns |
| `HERMES_TTS_VOICE` / `HERMES_TTS_VOICE_<ENGINE>` | per-source voice override |
| `MEDIA_HOOK_ENABLED=0` | global agent-media hook kill switch |
| `MEDIA_HOOK_NO_DETACH=1` | play inline (tests/debug) instead of detaching |

See the core repo's `docs/EXTENSIONS.md` (§2 Intake adapters).

## Test

```bash
# synthetic payload → should speak "hello from hermes"
echo '{"hook_event_name":"post_llm_call","assistant_response":"hello from hermes"}' \
  | media-hook-hermes

# via Hermes itself
hermes hooks test post_llm_call
```
