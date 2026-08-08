# Voice Bridge — Home Assistant integration

A ~100-line conversation agent that POSTs Assist transcripts to
tmux-voice-bridge. That's all it does.

## Why it exists

Home Assistant has no built-in way to point a conversation agent at your own
HTTP endpoint — `openai_conversation` has no base-URL field. So the voice path
here previously borrowed a third-party integration (OpenClaw) as a generic
client, which meant:

- the bridge had to satisfy OpenClaw's protocol, not just answer chat
  completions — hence a `/tools/invoke` endpoint and a 30-second alive-check
  poll that exist for no other reason;
- and, on HA 2026.7, the agent was **invisible to the UI**. OpenClaw registers
  via the legacy `conversation.async_set_agent`, which no longer produces a
  `conversation.*` entity, so it can't be picked in the Assist pipeline editor.
  It worked only because a pipeline can address a config entry by id.

This registers a proper `ConversationEntity`. It shows up in the pipeline
picker like any other agent, and nothing else is in the path.

OpenClaw is unaffected — keep it for whatever it's actually for (here: the
Matrix agents on the gateway at :18789).

## Install

```sh
cp -r homeassistant/voice_bridge /path/to/homeassistant/custom_components/
# restart Home Assistant, then:
#   Settings → Devices & Services → Add Integration → "Voice Bridge"
```

A copy, not a symlink: Home Assistant usually runs in a container where the
repo path doesn't exist, so a symlink pointing outside `/config` dangles. After
updating the integration, clear `custom_components/voice_bridge/__pycache__`
and hard-restart the container — a `homeassistant.restart` service call does
not reliably reload changed custom-component bytecode.

Setup asks for the bridge URL (default `http://127.0.0.1:18790`) and a timeout,
and refuses to finish if the bridge doesn't answer — better to find out now than
mid-sentence with earbuds in.

If HA runs in a container with host networking, `127.0.0.1` reaches a bridge
bound to loopback on the host. Otherwise bind the bridge to an interface HA can
see and set the URL accordingly.

## Then point a pipeline at it

Settings → Voice assistants → your pipeline → Conversation agent →
**Voice Bridge**.

Give the pipeline a TTS engine even though the "reply" is only a status line
("Sent to local session scratch", "Sent to the agent"). A voice pipeline with
no TTS stage can fail to start a run at all, while text runs through the same
agent keep working — a confusing failure that looks like a dead microphone.

## What the replies mean

| spoken back | what happened |
|---|---|
| `Sent to local session <name>.` | typed into that tmux pane |
| `Sent to the agent.` | an agent-media `converse` was waiting and took it |
| `Switched to ...` | you spoke a target-switching command |
| `Injection failed: ...` | the pane is gone — check `media errors` |
