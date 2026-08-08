"""Mic capture.

No audio is captured on this host. HA Assist on a phone/earbud is the
de-facto mic; the audio never lands here, only the transcript does.

  rendezvous.py — the listening half of the `converse` MCP tool. Transcripts
    still arrive via packages/voice-bridge/ (HA's conversation agent POSTs to
    its OpenAI-compatible endpoint); the rendezvous lets a blocking `converse`
    call claim the next one instead of it being typed into a tmux pane.
  termux-microphone-record — used by the legacy sam-listener for Matrix voice
    replies; that path retires when the matrix intake adapter lands a
    recording flow.

Note what is still missing: nothing here can *open* a mic. `converse` is
half-duplex — the human decides when to start talking, and it only decides
where the words go. Agent-initiated listening needs an `assist_satellite.*`
entity in HA (ESPHome Voice PE, or wyoming-satellite on a host with a mic),
which would replace the speaking half of `converse` and leave the listening
half below unchanged.

(future) a real local capture path — push-to-talk daemon, Bluetooth-button
capture, or sounddevice-backed continuous capture — would land here, paired
with a `transcribe/` implementation.
"""
