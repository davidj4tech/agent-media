"""Voice chat spike, step 0 of docs/proposals/2026-09-22-voice-chat.md.

How long from "his words reach red5" to "the first sentence of the reply is
audio, ready to send"? One warm `claude -p` (stream-json both ways, with
--include-partial-messages) per run; the text deltas feed the pi sentencer,
and the first complete sentence is rendered by piper and by edge.

Hooks stay out (`--setting-sources project,local`), so nothing is spoken on
the phone. Network is not in here: add the measured one-way trip to the phone
to both ends (see the notes file).

    python spike/voice/latency.py [--model haiku] [--runs 6]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from agent_media_core.intake._text import IncrementalSentencer
from agent_media_core.render.engines import render_text

PROMPTS = [
    "Hey, what's a good way to wind down after a long day?",
    "Quick one: why is the sky blue?",
    "Can you give me an idea for dinner tonight, something easy?",
    "What's the difference between a latte and a flat white?",
    "Tell me something interesting about octopuses.",
    "How do I stop my headphones tangling in my pocket?",
    "What should I read next if I liked Project Hail Mary?",
    "Remind me how compound interest works, briefly.",
]

VOICE_PROMPT = ("You are in a spoken voice conversation. Answer the way a person talks: "
                "short, plain sentences, no lists, no markdown, two to four sentences "
                "unless asked for more. Start with the answer, not a preamble.")


class Claude:
    """One warm `claude -p` in stream-json, read on a thread."""

    def __init__(self, model: str) -> None:
        self.cwd = tempfile.mkdtemp(prefix="voice-spike-")
        argv = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--model", model,
                "--setting-sources", "project,local", "--append-system-prompt", VOICE_PROMPT]
        self.p = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.lines: list[tuple[float, dict]] = []
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with self.cv:
                self.lines.append((time.monotonic(), ev))
                self.cv.notify_all()

    def send(self, text: str) -> int:
        with self.cv:
            mark = len(self.lines)
        self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        self.p.stdin.flush()
        return mark

    def events(self, mark: int, timeout: float = 90):
        """Yield (t, event) from `mark` until the turn's result."""
        i, end = mark, time.monotonic() + timeout
        while True:
            with self.cv:
                while i >= len(self.lines):
                    left = end - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("no result")
                    self.cv.wait(left)
                t, ev = self.lines[i]
            i += 1
            yield t, ev
            if ev.get("type") == "result":
                return

    def close(self) -> None:
        self.p.stdin.close()
        self.p.wait(timeout=10)


def delta_text(ev: dict) -> str:
    if ev.get("type") != "stream_event":
        return ""
    e = ev.get("event") or {}
    d = e.get("delta") or {}
    return d.get("text", "") if e.get("type") == "content_block_delta" and d.get("type") == "text_delta" else ""


def render_timed(text: str, engine: str, out: Path) -> float:
    t = time.monotonic()
    ok, err = render_text(text, out, engine=engine, fallback_to_edge=False)
    if not ok:
        raise RuntimeError(f"{engine}: {err}")
    return time.monotonic() - t


def one_turn(c: Claude, prompt: str, tmp: Path) -> dict:
    t0 = time.monotonic()
    mark = c.send(prompt)
    s = IncrementalSentencer()
    first_delta = first_sentence = None
    sentence = ""
    text = ""
    for t, ev in c.events(mark):
        d = delta_text(ev)
        if d:
            text += d
            first_delta = first_delta or t
            if not sentence:
                got = s.feed(d)
                if got:
                    sentence, first_sentence = got[0], t
    end = time.monotonic()
    if not sentence:
        tail = s.close()
        sentence, first_sentence = (tail[0] if tail else text.strip()), end
    r = {"prompt": prompt, "first_sentence": sentence,
         "delta_s": round(first_delta - t0, 3) if first_delta else None,
         "sentence_s": round(first_sentence - t0, 3), "turn_s": round(end - t0, 3)}
    for engine in ("piper", "edge"):
        r[f"{engine}_render_s"] = round(render_timed(sentence, engine, tmp / f"{engine}.audio"), 3)
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--runs", type=int, default=6)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="voice-spike-audio-"))
    c = Claude(args.model)
    try:
        for _ in c.events(c.send("Say hi in three words.")):   # warm-up, not counted
            pass
        rows = []
        for prompt in PROMPTS[: args.runs]:
            r = one_turn(c, prompt, tmp)
            rows.append(r)
            print(json.dumps(r), flush=True)
    finally:
        c.close()
    summary = {"model": args.model, "runs": len(rows)}
    for k in ("delta_s", "sentence_s", "turn_s", "piper_render_s", "edge_render_s"):
        vals = [r[k] for r in rows if r[k] is not None]
        summary[k] = {"median": round(statistics.median(vals), 3), "max": round(max(vals), 3)}
    print(json.dumps(summary, indent=1))
    out = Path(__file__).with_name(f"results-{args.model}.json")
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
