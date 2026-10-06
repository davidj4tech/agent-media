"""How full a session's context is (`context.py`) and the log that carries it.

Fake transcripts under the conftest's throwaway CLAUDE_CONFIG_DIR, never a
real one; the window file under its throwaway XDG_STATE_HOME.
"""

from __future__ import annotations

import json
import os

from agent_media_server import context

from test_contract import (AUTH, SID, call, server, shelf,  # noqa: F401
                           signed_in, typed)
from test_recaps import _append, _msg, _transcript


def _reply(i: int, inp: int, created: int, read: int, sidechain: bool = False) -> str:
    return json.dumps({"type": "assistant", "uuid": f"r{i}", "isSidechain": sidechain,
                       "timestamp": "2026-10-07T01:00:00Z",
                       "message": {"role": "assistant", "model": "claude-opus-5-5",
                                   "content": [{"type": "text", "text": "hi"}],
                                   "usage": {"input_tokens": inp,
                                             "cache_creation_input_tokens": created,
                                             "cache_read_input_tokens": read,
                                             "output_tokens": 500}}})


def _user(i: int) -> str:
    return json.dumps({"type": "user", "uuid": f"u{i}",
                       "message": {"role": "user", "content": "go on"}})


def test_the_newest_reply_is_the_context_in_use():
    _transcript(SID, [_reply(1, 2, 1000, 9000), _user(1), _reply(2, 3, 4000, 50000), _user(2)])
    assert context.context_for(SID) == {"used": 54003, "window": 200_000}


def test_a_growing_transcript_is_read_again():
    path = _transcript(SID, [_reply(1, 0, 0, 10)])
    assert context.context_for(SID)["used"] == 10
    _append(path, [_reply(2, 0, 0, 20)])
    assert context.context_for(SID)["used"] == 20


def test_sidechain_and_synthetic_lines_do_not_count():
    _transcript(SID, [_reply(1, 0, 0, 30000), _reply(2, 0, 0, 999, sidechain=True),
                      _reply(3, 0, 0, 0)])
    assert context.context_for(SID)["used"] == 30000


def test_no_reply_yet_is_none():
    _transcript(SID, [_user(1), _msg(1)])
    assert context.context_for(SID) is None
    assert context.context_for("not-a-session") is None


def test_the_window_is_the_status_lines_or_a_guess():
    _transcript(SID, [_reply(1, 0, 0, 250_000)])
    assert context.context_for(SID)["window"] == 1_000_000
    d = os.path.join(os.environ["XDG_STATE_HOME"], "agent-media", "context-window")
    os.makedirs(d)
    with open(os.path.join(d, f"{SID}.json"), "w") as fh:
        fh.write('{"size": 500000}')
    assert context.context_for(SID) == {"used": 250_000, "window": 500_000}


def test_the_log_carries_it(server, shelf, signed_in, monkeypatch):
    from agent_media_core import activity, book_tracks

    monkeypatch.setattr(book_tracks, "conversation_log", lambda s, f, target=None, positions=True: [])
    monkeypatch.setattr(activity, "attach", lambda s, lines: None)
    _transcript(SID, [_reply(1, 1, 2, 3)])
    res, obj = call(server, "GET", f"/conversation/log?session={SID}", headers=AUTH)
    assert res.status == 200, obj
    assert obj["context"] == {"used": 6, "window": 200_000}
