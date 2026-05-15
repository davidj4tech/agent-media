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
