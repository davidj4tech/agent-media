"""Claude Code's recaps (`recaps.py`) and where they surface.

A recap is the `away_summary` system line Claude Code writes into a session's
transcript when you come back to it. These tests write fake transcripts under
the throwaway CLAUDE_CONFIG_DIR the conftest sets — never a real one — and pin
the parsing, the backwards read, the cache, and the two routes that carry it
(`/targets` rows and the `/conversation/log` envelope).
"""

from __future__ import annotations

import json
import os
import time

import pytest

from agent_media_server import recaps

from test_contract import (AUTH, SID, SID2, call, server, shelf,  # noqa: F401
                           signed_in, typed)

HINT = " (disable recaps in /config)"


def _recap(text: str, ts: str = "2026-09-21T07:01:52.291Z", **extra) -> str:
    return json.dumps({"parentUuid": "p", "isSidechain": False, "type": "system",
                       "subtype": "away_summary", "content": text, "timestamp": ts,
                       "uuid": "u", "isMeta": False, "sessionId": "s", **extra})


def _msg(i: int = 0, pad: int = 0) -> str:
    return json.dumps({"type": "assistant", "uuid": f"m{i}", "timestamp": "2026-09-21T06:00:00Z",
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": "x" * pad or "hi"}]}})


def _transcript(session: str, lines: list[str], project: str = "-home-ryer-p") -> str:
    d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", project)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{session}.jsonl")
    with open(path, "w") as fh:
        fh.write("".join(line + "\n" for line in lines))
    return path


def _append(path: str, lines: list[str], raw: str = "") -> None:
    with open(path, "a") as fh:
        fh.write("".join(line + "\n" for line in lines) + raw)
    # Make sure the mtime moves even on a coarse-clock filesystem; the size
    # moves anyway.
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


# --- parsing ------------------------------------------------------------------------

def test_recap_is_parsed_and_the_hint_stripped():
    _transcript(SID, [_msg(1), _recap("We shipped the thing. Next: try it." + HINT), _msg(2)])
    assert recaps.latest_recap(SID) == {"text": "We shipped the thing. Next: try it.",
                                        "at": 1789974112.291}


def test_an_ordinary_trailing_parenthetical_is_kept():
    _transcript(SID, [_recap("Fixed the build (see CI).")])
    assert recaps.latest_recap(SID)["text"] == "Fixed the build (see CI)."


def test_the_latest_recap_wins():
    _transcript(SID, [_recap("first" + HINT, "2026-09-20T01:00:00Z"), _msg(1),
                      _recap("second" + HINT, "2026-09-21T01:00:00Z"), _msg(2)])
    assert recaps.latest_recap(SID)["text"] == "second"
    assert [r["text"] for r in recaps.recaps(SID)] == ["first", "second"]
    first_at = recaps.recaps(SID)[0]["at"]
    assert [r["text"] for r in recaps.recaps(SID, since=first_at)] == ["second"]


def test_no_recap_is_none():
    _transcript(SID, [_msg(1), _msg(2)])
    assert recaps.latest_recap(SID) is None
    assert recaps.recaps(SID) == []


def test_no_transcript_is_none():
    assert recaps.latest_recap(SID) is None


def test_non_claude_sessions_are_none(tmp_path, monkeypatch):
    # A Hermes id is never looked for; a Codex session is a uuid with a
    # rollout file but no Claude transcript, recap-shaped lines or not.
    assert recaps.latest_recap("20260921_102508_f74b02") is None
    codex = tmp_path / "codex" / "sessions" / "2026" / "09" / "21"
    codex.mkdir(parents=True)
    (codex / f"rollout-2026-09-21T10-00-00-{SID2}.jsonl").write_text(_recap("nope") + "\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert recaps.latest_recap(SID2) is None
    assert recaps.latest_recap("not-a-session") is None


def test_malformed_lines_are_ignored():
    _transcript(SID, [
        _recap("good" + HINT, "2026-09-20T01:00:00Z"),
        '{"type":"system","subtype":"away_summary","content":"torn',     # bad JSON
        json.dumps({"type": "user", "message": {"content": "away_summary talk"}}),
        _recap("", "2026-09-21T01:00:00Z"),                           # no words
        _recap("no time", "not a timestamp"),                          # no time
        json.dumps(["away_summary"]),                                  # not an object
    ])
    assert recaps.latest_recap(SID)["text"] == "good"
    assert [r["text"] for r in recaps.recaps(SID)] == ["good"]


def test_a_line_still_being_written_is_left_for_the_next_read():
    path = _transcript(SID, [_msg(1)])
    line = _recap("late" + HINT)
    _append(path, [], raw=line[:40])          # torn: no newline yet
    assert recaps.latest_recap(SID) is None
    _append(path, [], raw=line[40:] + "\n")
    assert recaps.latest_recap(SID)["text"] == "late"


# --- reading big files from the end ----------------------------------------------------

def _big(recap_at_start: bool) -> list[str]:
    body = [_msg(i, pad=2000) for i in range(2500)]           # ~5 MB
    r = _recap("the one" + HINT)
    return [r, *body] if recap_at_start else [*body, r, _msg(9999)]


def test_a_recap_near_the_end_of_a_big_transcript_is_one_read(monkeypatch):
    path = _transcript(SID, _big(recap_at_start=False))
    assert os.path.getsize(path) > 5_000_000
    reads = []
    real_open = open

    class Counting:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.fh.close()

        def seek(self, n):
            return self.fh.seek(n)

        def read(self, n=-1):
            data = self.fh.read(n)
            reads.append(len(data))
            return data

    monkeypatch.setattr(recaps, "open", lambda p, mode="r": Counting(real_open(p, mode)),
                        raising=False)
    t = time.perf_counter()
    assert recaps.latest_recap(SID)["text"] == "the one"
    assert time.perf_counter() - t < 0.2
    # The tail-newline probe and one chunk — not the 5 MB.
    assert sum(reads) <= 2 * recaps._CHUNK


def test_a_recap_far_from_the_end_is_still_found():
    _transcript(SID, _big(recap_at_start=True))
    assert recaps.latest_recap(SID)["text"] == "the one"


def test_a_recap_line_split_across_chunks_is_read_whole(monkeypatch):
    monkeypatch.setattr(recaps, "_CHUNK", 64)       # far shorter than one line
    _transcript(SID, [_msg(1, pad=300), _recap("split" + HINT), _msg(2, pad=300)])
    assert recaps.latest_recap(SID)["text"] == "split"


# --- the cache -------------------------------------------------------------------------

def test_an_unchanged_file_is_not_read_again(monkeypatch):
    _transcript(SID, [_recap("once" + HINT)])
    assert recaps.latest_recap(SID)["text"] == "once"
    monkeypatch.setattr(recaps, "_last_in", lambda *a: pytest.fail("read again"))
    assert recaps.latest_recap(SID)["text"] == "once"


def test_an_append_is_read_and_only_the_append(monkeypatch):
    path = _transcript(SID, [_recap("old" + HINT, "2026-09-20T01:00:00Z"), _msg(1)])
    assert recaps.latest_recap(SID)["text"] == "old"
    before = os.path.getsize(path)
    spans = []
    real = recaps._last_in
    monkeypatch.setattr(recaps, "_last_in",
                        lambda fh, lo, hi: spans.append((lo, hi)) or real(fh, lo, hi))
    _append(path, [_msg(2)])
    assert recaps.latest_recap(SID)["text"] == "old"          # nothing new: kept
    _append(path, [_recap("new" + HINT, "2026-09-21T01:00:00Z")])
    assert recaps.latest_recap(SID)["text"] == "new"
    assert spans and all(lo >= before for lo, _hi in spans)


def test_a_replaced_or_shrunk_file_is_read_from_scratch():
    path = _transcript(SID, [_recap("old" + HINT), _msg(1, pad=500)])
    assert recaps.latest_recap(SID)["text"] == "old"
    os.remove(path)                       # a new inode at the same path
    _transcript(SID, [_msg(1)])
    assert recaps.latest_recap(SID) is None
    with open(path, "w") as fh:           # same inode, rewritten longer
        fh.write(_recap("rewritten" + HINT) + "\n")
    assert recaps.latest_recap(SID)["text"] == "rewritten"
    with open(path, "w") as fh:           # same inode, shorter
        fh.write(_recap("short" + HINT, "2026-09-22T01:00:00Z")[:120] + "\n")
    assert recaps.latest_recap(SID) is None


def test_listing_after_latest_catches_up():
    path = _transcript(SID, [_recap("a" + HINT, "2026-09-20T01:00:00Z")])
    assert recaps.latest_recap(SID)["text"] == "a"            # latest only
    _append(path, [_recap("b" + HINT, "2026-09-21T01:00:00Z")])
    assert [r["text"] for r in recaps.recaps(SID)] == ["a", "b"]
    _append(path, [_recap("c" + HINT, "2026-09-22T01:00:00Z")])
    assert [r["text"] for r in recaps.recaps(SID)] == ["a", "b", "c"]
    assert recaps.latest_recap(SID)["text"] == "c"


def test_the_answer_is_a_copy():
    _transcript(SID, [_recap("mine" + HINT)])
    recaps.latest_recap(SID)["text"] = "changed"
    assert recaps.latest_recap(SID)["text"] == "mine"


# --- on the routes -------------------------------------------------------------------

def test_targets_rows_carry_the_recap(server, shelf, signed_in):
    _transcript(SID, [_recap("shelved one" + HINT)])
    _, obj = call(server, "GET", "/targets", headers=AUTH)
    by = {r["session"]: r for r in obj["sessions"]}
    assert by[SID]["recap"] == {"text": "shelved one", "at": 1789974112.291,
                                "source": "claude"}
    assert by[SID2]["recap"] is None
    _, conv = call(server, "GET", "/conversations", headers=AUTH)
    assert {r["session"]: r["recap"] for r in conv["sessions"]} == \
        {r["session"]: r["recap"] for r in obj["sessions"]}


def test_the_log_carries_the_recap_but_never_as_a_line(server, shelf, signed_in, monkeypatch):
    from agent_media_core import activity, book_tracks

    monkeypatch.setattr(book_tracks, "conversation_log", lambda s, f, target=None, positions=True: [
        {"start": None, "end": None, "who": "you", "text": "One?", "at": 10.0, "key": ""}])
    monkeypatch.setattr(activity, "attach", lambda s, lines: None)
    _transcript(SID, [_recap("where we are" + HINT)])
    for path in ("/conversation/log?item=li_1", f"/conversation/log?session={SID}"):
        res, obj = call(server, "GET", path, headers=AUTH)
        assert res.status == 200, obj
        assert obj["recap"] == {"text": "where we are", "at": 1789974112.291,
                                "source": "claude"}
        assert [line["text"] for line in obj["lines"]] == ["One?"]
