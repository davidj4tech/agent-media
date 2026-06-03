"""pi coding-agent STREAMING intake — stdin token-delta pipe.

Streaming sibling of `hook_pi`. The pi streaming extension
(`packages/core/pi/media-tts-stream.ts`) spawns this once per assistant turn
and pipes the model's text deltas to stdin as they arrive. We segment them
into sentences on the fly (`IncrementalSentencer`) and hand each completed
sentence to `submit_stream`, which renders + speaks it through sink-speech
while the model is still generating the rest of the reply.

Honors the same env as `hook_pi`: `MEDIA_HOOK_ENABLED` / `PI_TTS_ENABLED`
gate it, and `PI_TTS_ENGINE` / `PI_TTS_VOICE` override the generic
`MEDIA_RENDER_*` (loaded from ~/.config/agent-media.env).
"""

from __future__ import annotations

import codecs
import os
import queue
import sys
import threading

from ..types import Event, Priority, Source
from ._env import load_env_file
from ._text import IncrementalSentencer
from .submit import submit_stream

_SENTINEL = object()


def main() -> int:
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    if os.environ.get("PI_TTS_ENABLED", "1") == "0":
        return 0

    load_env_file("hook-pi")

    engine = (os.environ.get("PI_TTS_ENGINE")
              or os.environ.get("MEDIA_RENDER_ENGINE"))
    voice = (os.environ.get("PI_TTS_VOICE")
             or os.environ.get("MEDIA_RENDER_VOICE"))

    sentencer = IncrementalSentencer()
    q: "queue.Queue" = queue.Queue()

    def _reader() -> None:
        # os.read on the raw fd returns as soon as bytes are available (unlike
        # buffered read(n), which would wait to fill n) — that's what keeps the
        # sentence pipeline streaming. An incremental UTF-8 decoder tolerates
        # multibyte chars split across reads.
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        fd = sys.stdin.fileno()
        try:
            while True:
                try:
                    b = os.read(fd, 4096)
                except OSError:
                    break
                if not b:
                    for s in sentencer.feed(dec.decode(b"", final=True)):
                        q.put(s)
                    for s in sentencer.close():
                        q.put(s)
                    break
                for s in sentencer.feed(dec.decode(b)):
                    q.put(s)
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(_SENTINEL)

    def _sentences():
        while True:
            item = q.get()
            if item is _SENTINEL:
                return
            yield item

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    submit_stream(
        _sentences(),
        Event(text="", source=Source.PI, priority=Priority.NORMAL,
              engine=engine, voice=voice, metadata={"kind": "stream"}),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
