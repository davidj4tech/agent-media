"""Audio → text.

Placeholder. The two real STT entry points in the agent-media universe:

  packages/voice-bridge/  — HA Assist transcribes voice and POSTs the
    text via OpenAI-compatible /v1/chat/completions. voice-bridge
    then injects keystrokes into a target tmux pane (Claude Code,
    Codex, etc.). The agent's own Stop hook produces the spoken
    reply via the agent_media_core.intake pipeline. voice-bridge is
    a peer, not a subordinate, of core/ — keeping it as its own
    package per RESTRUCTURE.md plan B.

  (future) local Whisper, push-to-talk, etc. — would land here as
    a real module with a render-like `transcribe(audio_path) -> str`
    contract.

Today: deliberately empty. The package layout reserves the spot so
adding Whisper later doesn't require structural changes.
"""
