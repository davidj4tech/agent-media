"""Mic capture.

Placeholder. Mic capture lives in sibling tooling today:

  packages/voice-bridge/  — HA Assist on a phone/earbud is the
    de-facto mic. The audio never lands on this host.
  termux-microphone-record — used by the legacy sam-listener for
    Matrix voice replies; that path retires when the matrix intake
    adapter lands a recording flow.

(future) push-to-talk daemon, Bluetooth-button capture, or
sounddevice-backed continuous capture would land here.

Today: deliberately empty. The slot is reserved.
"""
