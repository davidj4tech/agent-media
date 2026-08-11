"""A follow-along view of the reply being spoken: the teleprompter pane.

The copy-mode highlight paints the *source* pane, which works only while that
pane has scrollback to search. Claude Code and every other fullscreen TUI hold
the alternate screen, where there is no scrollback and the app redraws whenever
it likes — so the highlight either finds nothing or has to be bought by driving
the app's own transcript keys and fighting its re-renders.

A pane of our own has none of those problems. It reads the same state the
highlight does (`clip_sentences` + `current_sentence_idx` on the speech row,
which every lane now carries), owns its whole surface, and never touches
anybody else's.

The layout is a pure function so it can be tested without a terminal: `frame`
takes the sentences, where we are, and the size of the pane, and returns the
lines to print.
"""

from __future__ import annotations

import os
import textwrap
from typing import Optional


# The current sentence sits this far down the pane when there's enough text on
# either side — reading is more comfortable a little above centre, and it
# leaves the *next* sentence or two visible, which is the point of following.
_ANCHOR = 0.38

DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"
CLEAR = "\x1b[H\x1b[2J"
BAR = "▌"


def _colour() -> bool:
    return not os.environ.get("NO_COLOR")


def _wrap(sentence: str, width: int) -> list[str]:
    text = " ".join(sentence.split())
    if not text:
        return [""]
    return textwrap.wrap(text, width=max(8, width)) or [""]


def layout(sentences: list[str], width: int) -> tuple[list[str], list[int]]:
    """Wrap every sentence to `width`. Returns (lines, first line of each sentence).

    Sentences are separated by a blank line so the eye can find the boundary
    the voice is about to cross.
    """
    lines: list[str] = []
    starts: list[int] = []
    for i, sentence in enumerate(sentences):
        if i:
            lines.append("")
        starts.append(len(lines))
        lines.extend(_wrap(sentence, width))
    return lines, starts


def frame(sentences: list[str], idx: Optional[int], width: int, height: int,
          *, colour: Optional[bool] = None, title: str = "") -> list[str]:
    """The pane's contents: `height` lines of `width` columns, scrolled to `idx`.

    Spoken sentences are dimmed, the current one is marked and bright, the rest
    wait in plain text. `idx` of None (nothing playing) just shows the top.
    """
    if colour is None:
        colour = _colour()
    width = max(12, width)
    height = max(3, height)
    body_w = width - 2                      # room for the current-sentence bar
    lines, starts = layout(sentences, body_w)
    if not lines:
        lines, starts = [""], [0]

    cur = None if idx is None else max(0, min(int(idx), len(starts) - 1))
    # Scroll so the current sentence sits at the reading anchor, clamped to the
    # ends: an anchored view that ran past the last line would show blank pane
    # while the voice was still reading.
    if cur is None:
        top = 0
    else:
        top = max(0, starts[cur] - int(height * _ANCHOR))
        top = min(top, max(0, len(lines) - height))

    # Which sentence each line belongs to, so a line can be styled by state.
    owner: list[int] = []
    s = -1
    for n in range(len(lines)):
        if s + 1 < len(starts) and n >= starts[s + 1]:
            s += 1
        owner.append(max(0, s))

    out: list[str] = []
    for n in range(top, top + height):
        if n >= len(lines):
            out.append("")
            continue
        text = lines[n][:body_w]
        who = owner[n]
        if cur is not None and who == cur:
            marker = BAR if text else " "
            out.append(f"{BOLD}{marker} {text}{RESET}" if colour
                       else f"{marker} {text}")
        elif cur is not None and who < cur:
            out.append(f"{DIM}  {text}{RESET}" if colour else f"  {text}")
        else:
            out.append(f"  {text}")
    if title:
        out[0] = (f"{DIM}{title[:width]}{RESET}" if colour else title[:width])
    return out


def idle_frame(text: str, width: int, height: int,
               *, colour: Optional[bool] = None) -> list[str]:
    """What to show when nothing is being spoken.

    The last reply, dimmed, rather than an empty pane: a blank surface reads as
    broken, and the thing you most often want after a reply ends is to finish
    reading it.
    """
    if colour is None:
        colour = _colour()
    width = max(12, width)
    height = max(3, height)
    lines, _ = layout([text] if text else ["— nothing spoken yet —"], width - 2)
    out = [f"{DIM}  {ln[:width - 2]}{RESET}" if colour else f"  {ln[:width - 2]}"
           for ln in lines[:height]]
    out += [""] * (height - len(out))
    return out
