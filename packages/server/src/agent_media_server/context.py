"""How full a Claude Code session's context is, for the line under a thread's
title (David, 7 Oct 2026).

Every assistant line in a transcript carries the request's `message.usage`:

    {"input_tokens": 2, "cache_creation_input_tokens": 4586,
     "cache_read_input_tokens": 47246, "output_tokens": 614, …}

The three input counts together are what the model was sent: the context in
use at that reply. The newest such line is the answer, so it is found by
reading backwards from the end, which is one chunk on any live session (it
writes one every turn). After a /compact the next reply's count is the
smaller context, so nothing special is done for it.

The transcript does not say how big the window is. Claude Code's status
line is given it (`context_window.context_window_size`), and the status line
in agent-config writes it to `<state_dir>/context-window/<session>.json` as
`{"size": N}`; a session that never drew one (a headless one) is taken to
have the usual 200k, or 1M once it is past 200k.

Read-only: this never writes to a transcript.
"""

from __future__ import annotations

import json
import os
import threading

from . import recaps

#: Every line with a usage block has this; a line without it is never parsed.
_MARK = b'"cache_read_input_tokens"'
_CHUNK = 256 * 1024
#: Give up after this much of the end without a usage line: a transcript
#: whose last reply is further back than this is not worth a full read.
_REACH = 4 * 1024 * 1024
_WINDOW = 200_000
_WINDOW_LONG = 1_000_000
_CACHE_MAX = 512

#: path → ((inode, size, mtime), answer)
_CACHE: dict[str, tuple[tuple, int | None]] = {}
_LOCK = threading.Lock()


def _reset_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()


def _used(raw: bytes) -> int | None:
    """The input tokens of one transcript line, or None if it has none."""
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("type") != "assistant" or rec.get("isSidechain"):
        return None
    msg = rec.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    n = sum(int(usage.get(k) or 0) for k in
            ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    # A synthetic line (an error, an interrupt) reports nothing sent.
    return n or None


def _last_used(path: str, size: int) -> int | None:
    """The newest usage in the file, reading backwards from `size`."""
    with open(path, "rb") as fh:
        carry = b""
        pos = size
        floor = max(0, size - _REACH)
        while pos > floor:
            n = min(_CHUNK, pos - floor)
            pos -= n
            fh.seek(pos)
            buf = fh.read(n) + carry
            if pos > floor:
                cut = buf.find(b"\n")
                if cut < 0:
                    carry = buf
                    continue
                carry, buf = buf[:cut], buf[cut + 1:]
            else:
                carry = b""
            end = len(buf)
            while True:
                i = buf.rfind(_MARK, 0, end)
                if i < 0:
                    break
                start = buf.rfind(b"\n", 0, i) + 1
                stop = buf.find(b"\n", i)
                if stop < 0:
                    stop = len(buf)
                used = _used(buf[start:stop])
                if used is not None:
                    return used
                end = start
    return None


def _window(session: str, used: int) -> int:
    from agent_media_core._paths import state_dir

    try:
        with open(state_dir() / "context-window" / f"{session}.json") as fh:
            size = int(json.load(fh).get("size") or 0)
        if size > 0:
            return size
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return _WINDOW_LONG if used > _WINDOW else _WINDOW


def context_for(session: str) -> dict | None:
    """`{"used", "window"}` in tokens, or None for a session with no reply
    yet, one that is not Claude Code's, or a transcript that cannot be read."""
    path = recaps.transcript_path(session)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_ino, st.st_size, st.st_mtime)
    with _LOCK:
        hit = _CACHE.get(path)
    if hit and hit[0] == key:
        used = hit[1]
    else:
        try:
            used = _last_used(path, st.st_size)
        except OSError:
            return None
        with _LOCK:
            _CACHE.pop(path, None)
            _CACHE[path] = (key, used)
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
    if not used:
        return None
    return {"used": used, "window": _window(session, used)}
