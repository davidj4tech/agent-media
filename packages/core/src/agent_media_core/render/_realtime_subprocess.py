"""Realtime engine subprocess body. Run in MEDIA_REALTIME_PYTHON's venv.

Reads JSON config from stdin (`{"text", "model", "voice", "outfile"}`),
opens the OpenAI Realtime WebSocket, collects PCM deltas, writes WAV to
`outfile`. Exits non-zero with a one-line reason on stderr if anything
goes wrong. Mirrors the TypeScript flow in
`packages/audio-relay/extensions/pi-tts-extension.ts::ttsOpenAIRealtime`.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
from urllib.parse import quote


def _wav_wrap(pcm: bytes, rate: int, width: int, channels: int) -> bytes:
    byte_rate = rate * channels * width
    block_align = channels * width
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, 1, channels, rate, byte_rate, block_align, width * 8,
        b"data", len(pcm),
    )
    return header + pcm


def main() -> int:
    try:
        from websockets.sync.client import connect
    except ImportError:
        print("realtime: websockets package missing in realtime venv", file=sys.stderr)
        return 1

    try:
        cfg = json.loads(sys.stdin.read())
        text = cfg["text"]
        model = cfg["model"]
        voice = cfg["voice"]
        outfile = cfg["outfile"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"realtime: bad config: {e}", file=sys.stderr)
        return 2

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("realtime: OPENAI_API_KEY not set", file=sys.stderr)
        return 1

    url = f"wss://api.openai.com/v1/realtime?model={quote(model)}"
    headers = {"Authorization": f"Bearer {key}"}
    chunks: list[bytes] = []

    try:
        with connect(url, additional_headers=headers, open_timeout=10, close_timeout=5) as ws:
            ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "audio": {"output": {"voice": voice,
                                          "format": {"type": "audio/pcm", "rate": 24000}}},
                },
            }))
            ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": text}]},
            }))
            ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "output_modalities": ["audio"],
                    "instructions": (
                        "Read the user message aloud verbatim. Do not answer, "
                        "summarize, explain, translate, add commentary, or change "
                        "wording. Preserve punctuation and expressive tone as "
                        "spoken delivery."
                    ),
                },
            }))
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                t = msg.get("type")
                if t == "response.output_audio.delta" and msg.get("delta"):
                    chunks.append(base64.b64decode(msg["delta"]))
                elif t == "error":
                    err = (msg.get("error") or {}).get("message", "realtime error")
                    print(f"realtime: {err}", file=sys.stderr)
                    return 1
                elif t == "response.done":
                    break
    except Exception as e:  # noqa: BLE001
        print(f"realtime: ws: {e}", file=sys.stderr)
        return 1

    pcm = b"".join(chunks)
    if not pcm:
        print("realtime: produced no audio", file=sys.stderr)
        return 1
    with open(outfile, "wb") as f:
        f.write(_wav_wrap(pcm, 24000, 2, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
