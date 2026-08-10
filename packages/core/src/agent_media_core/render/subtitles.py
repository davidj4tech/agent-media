"""Word timings from a render, read back as sentence start times.

edge-tts emits a WordBoundary event as it synthesises, and `--write-subtitles`
turns those into an SRT cue per word in the same request as the audio. That is
a measurement of the clip we are about to play, for free — which matters where
the audio is the *only* thing that exists locally.

The phone lane is exactly that case: the whole reply is rendered and played on
the phone, so the host driving the conversation has no clip to probe and no
player it can afford to poll. The phone reads its own word timings, converts
them to `SENTENCE <idx> <offset>` lines on the wire, and the host follows that
timeline on a clock (see intake/submit.py `_SentenceFollower`).

Alignment is deliberately forgiving. The cue stream is what the voice *said*,
not what we wrote: numbers come back expanded ("42" → "forty two"), punctuation
is gone, and an abbreviation may be spelled out. So we walk the two streams
together, allowing the cues to run ahead, and give up — returning None, meaning
"approximate instead" — rather than report boundaries we don't believe.
"""

from __future__ import annotations

import re
from typing import Optional


_CUE_RE = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d+)\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d+)")
_WORD_RE = re.compile(r"[0-9a-z']+")

# How far ahead of our own text the cue stream may be and still count as the
# same word. Covers a spelled-out number or two; beyond that we're guessing.
_LOOKAHEAD = 6


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _stamp(m: re.Match, group: int) -> float:
    return (int(m.group(group)) * 3600 + int(m.group(group + 1)) * 60
            + int(m.group(group + 2)) + float(f"0.{m.group(group + 3)}"))


def parse_srt(srt: str) -> list[tuple[float, float, str]]:
    """(start, end, text) per cue, in seconds, in order. Junk blocks are skipped."""
    cues: list[tuple[float, float, str]] = []
    span: Optional[tuple[float, float]] = None
    content: list[str] = []
    for line in srt.splitlines():
        stripped = line.strip()
        m = _CUE_RE.match(stripped)
        if m:
            if span is not None and content:
                cues.append((span[0], span[1], " ".join(content)))
            span = (_stamp(m, 1), _stamp(m, 5))
            content = []
            continue
        if span is not None and stripped and not stripped.isdigit():
            content.append(stripped)
    if span is not None and content:
        cues.append((span[0], span[1], " ".join(content)))
    return cues


def sentence_offsets(srt: str, sentences: list[str]) -> Optional[list[float]]:
    """Where each sentence starts in the clip, in seconds — or None.

    None means the word stream and the text couldn't be aligned confidently.
    The caller should fall back to apportioning the duration: a smooth guess
    beats confidently pointing at the wrong words.
    """
    if not sentences:
        return None
    cues = parse_srt(srt)
    if not cues:
        return None

    tokens: list[tuple[int, str]] = []       # (sentence index, word)
    for i, sentence in enumerate(sentences):
        tokens.extend((i, w) for w in _words(sentence))
    if not tokens:
        return None

    offsets: list[Optional[float]] = [None] * len(sentences)
    pos = 0
    for start, end, content in cues:
        spoken = _words(content)
        if not spoken:
            continue
        # Walk the cue's words against ours, letting the cue run ahead by a
        # little (an expansion, a word we never wrote). Whichever of our
        # sentences a matched word belongs to starts here — interpolated across
        # the cue, because a cue may hold more than one of our sentences: the
        # splitter merges short fragments that the voice still separates.
        for k, word in enumerate(spoken):
            hit = None
            for j in range(pos, min(pos + _LOOKAHEAD, len(tokens))):
                if tokens[j][1] == word:
                    hit = j
                    break
            if hit is None:
                continue
            pos = hit + 1
            idx = tokens[hit][0]
            if offsets[idx] is None:
                offsets[idx] = start + (end - start) * (k / len(spoken))

    if any(o is None for o in offsets):
        return None                           # a sentence was never reached
    measured = [float(o) for o in offsets if o is not None]
    if measured != sorted(measured):
        return None
    # The first sentence owns the head of the clip whatever the first cue says:
    # a leading breath is still part of hearing it begin.
    measured[0] = 0.0
    return measured
