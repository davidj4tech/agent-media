"""Small text-cleanup helpers shared across intake adapters."""

from __future__ import annotations

import re


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITAL_RE = re.compile(r"(?<!\*)\*([^*]+)\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_HEAD_RE = re.compile(r"^#{1,6}\s+", flags=re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+", flags=re.MULTILINE)
_BLANK_RE = re.compile(r"\n[ \t]*\n+")


def strip_markdown(text: str) -> str:
    """Strip enough markdown that TTS doesn't read backticks / asterisks /
    fence markers aloud. Loose by design: callers can submit anything.
    """
    if not text:
        return ""
    out = _FENCE_RE.sub("", text)
    out = _HEAD_RE.sub("", out)
    out = _BOLD_RE.sub(r"\1", out)
    out = _ITAL_RE.sub(r"\1", out)
    out = _CODE_RE.sub(r"\1", out)
    out = _BULLET_RE.sub("", out)
    out = _BLANK_RE.sub("\n", out).strip()
    return out


# Sentence-ending punctuation, optional closing quote/bracket, then whitespace.
# The trailing whitespace is what tells us the sentence is *complete* — we only
# split once the following character has arrived in the stream.
_SENT_BOUNDARY = re.compile(r'[.!?]+["\'”’)\]]*\s')

# Abbreviations / initials whose trailing period is NOT a sentence end. Matched
# against the text up to and including the boundary's first punctuation char.
_ABBREV_TAIL = re.compile(
    r'(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|'
    r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)|\b[A-Za-z])\.$'
)

_WS_RE = re.compile(r'\s+')


class IncrementalSentencer:
    """Segment a *streamed* text into complete sentences as they arrive.

    Used by the streaming pi intake: token deltas are `feed()` in as they
    arrive, and each call returns any sentences that have just completed (i.e.
    whose terminating punctuation is now followed by whitespace). Trailing
    partial text is buffered until more arrives. `close()` flushes whatever
    remains as a final sentence.

    Each emitted sentence is run through `strip_markdown` and whitespace-
    collapsed so it's ready to render. Abbreviations and single-letter
    initials ("Dr.", "e.g.", "J.") don't trigger a split.

    Known limitation: fenced code blocks aren't suppressed (strip_markdown
    drops the ``` markers but keeps the lines) — fine for prose / roleplay,
    which is the streaming path's purpose.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buf += chunk
        return self._drain()

    def close(self) -> list[str]:
        out = self._drain()
        tail = self._clean(self._buf)
        self._buf = ""
        if tail:
            out.append(tail)
        return out

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            split_end = -1
            for m in _SENT_BOUNDARY.finditer(self._buf):
                # Text up to and including the first punctuation char of the
                # boundary; skip if it ends in an abbreviation/initial.
                head = self._buf[: m.start() + 1]
                if _ABBREV_TAIL.search(head):
                    continue
                split_end = m.end()  # includes the trailing whitespace
                break
            if split_end < 0:
                break
            raw, self._buf = self._buf[:split_end], self._buf[split_end:]
            cleaned = self._clean(raw)
            if cleaned:
                out.append(cleaned)
        return out

    @staticmethod
    def _clean(raw: str) -> str:
        return _WS_RE.sub(" ", strip_markdown(raw)).strip()
