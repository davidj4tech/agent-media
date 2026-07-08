"""agent-media-visual — SPIKE.

A visual channel alongside TTS: a full-bleed web canvas (SSE) that any
screen can point a browser at, plus a generator that turns a spoken reply
into an image (Venice API) and pushes it to the canvas.

Deliberately decoupled from the speech hot path: images are fire-and-forget
accompaniment ("album art" pattern), never something speech waits on.
"""
