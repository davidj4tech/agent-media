"""Listening to a note, with follow-along: the Notes reader's Listen key.

A note (or one heading of it) is rendered as a document is (`docs`): one
clip per paragraph, joined into a file whose chapters are its headings, with
a sidecar of when each paragraph starts and which line of the note it came
from. It plays on the book channel, so it has a place to resume from, the
lock screen's controls and the desk popup's, and replies pause it rather
than talk over it. The reader asks what is playing and marks that line.

  POST /org/listen {"path", "at"?, "line"?} → {"state", "path", "at", "title"}
  POST /org/listen {"doc", "line"?}         → the same with "doc" for "path": a
                                              project's document (doc_read.py)
  GET  /org/listen                          → {"state", …, "line", "pos_ms", "dur_ms"}
  POST /org/listen/ctl {"action"}           → the GET answer, after it

`state` is "rendering" (synthesis has not finished), "copying" (the file is
on its way to the phone, which plays it when it lands), "playing",
"paused" or "idle". `line` is 0-based in the `text` /org/read gave for the
same `path` and `at`: the reader draws that text, so it can find the line.
A `line` in the POST starts there (the paragraph at or after it); without
one a note picks up where it was left, or from the top once it was heard
to the end. server-contract.md §6.10.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from agent_media_core import docs

from . import auth, doc_read, org

#: A render in progress, by its stem: {"path", "at", "title", "line"}.
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_CACHE_TTL_S = 1.0
_CACHE: list = [0.0, None]
#: Within this much of the end, a note counts as heard: it starts again.
_HEARD_S = 3.0
_ACTIONS = ("pause", "resume", "toggle", "stop")


def _target() -> str:
    """Where a note plays: MEDIA_LISTEN_TARGET, else the phone's own book
    player when there is one (MEDIA_BOOK_SOCKET_PHONE), else the book
    channel's default. Not the default first: on a server whose speech goes
    to the app (`sasonica`), the book default follows it there, and the app's
    speech player can open neither a path on this host nor a long file
    without the replies talking over it."""
    import os
    return (os.environ.get("MEDIA_LISTEN_TARGET")
            or ("phone" if os.environ.get("MEDIA_BOOK_SOCKET_PHONE") else "")).strip()


def _book():
    """The book channel's controls, run in-process as the CLI runs them."""
    from agent_media_core import mcp_server

    return mcp_server


def _fmt(path: str) -> str:
    return "org" if path.endswith(".org") else "md"


def _start_ms(timings: dict | None, line: int | None) -> int:
    """Where to start for `line`: the first paragraph at or after it."""
    rows = (timings or {}).get("timings") or []
    if line is None or not rows:
        return 0
    for r in rows:
        if r["line"] >= line:
            return int(r["start"] * 1000)
    return int(rows[-1]["start"] * 1000)


def _line_at(timings: dict, pos_s: float) -> int | None:
    """The line of the paragraph being read at `pos_s`."""
    found = None
    for r in timings.get("timings") or []:
        if r["start"] > pos_s + 0.05:
            break
        found = r["line"]
    return found


def _playing() -> dict:
    """The book channel's state, kept a moment: the reader polls it, and on
    the phone each read is a round trip over the tailnet."""
    with _LOCK:
        at, got = _CACHE
        if got is not None and time.monotonic() - at < _CACHE_TTL_S:
            return got
    try:
        got = _book().book_now_playing(target=_target())
    except Exception:
        got = {"idle": True}
    with _LOCK:
        _CACHE[:] = [time.monotonic(), got]
    return got


def _forget_cache() -> None:
    with _LOCK:
        _CACHE[:] = [0.0, None]


def _clip_of(uri: str) -> Path | None:
    """The render a playing file is, if it is one with timings. On the phone
    the file is a copy with the same name, so the name is what is matched."""
    if not uri:
        return None
    name = Path(uri.split("?", 1)[0]).name
    clip = docs.doc_cache_dir() / name
    return clip if name.endswith(".mp3") and docs.timings_path(clip).exists() else None


def _play(clip: Path, meta: dict, line: int | None) -> str:
    """Put `clip` on the book channel; the state it is then in."""
    srv = _book()
    timings = docs.read_timings(clip)
    now = _playing()
    if line is not None and not now.get("idle") and _clip_of(now.get("uri") or "") == clip:
        # Already this note: a tap on a paragraph is a seek, not a reload.
        srv.book_seek(_start_ms(timings, line) / 1000.0, target=_target())
        if now.get("paused"):
            srv.book_resume(target=_target())
        _forget_cache()
        return "playing"
    if line is not None:
        start = _start_ms(timings, line)
    else:
        start = srv._state().get_resume_pos(str(clip)) or 0
        dur = (timings or {}).get("duration") or 0
        if dur and start >= (dur - _HEARD_S) * 1000:
            start = 0
    r = srv.book_play(str(clip), resume=False, start_ms=start, title=meta["title"],
                      target=_target())
    _forget_cache()
    if r.get("ok"):
        return "playing"
    return "copying" if r.get("fetching") else "error"


def _render_then_play(stem: str, sections: list, meta: dict) -> None:
    try:
        clip = docs.render_sections(sections, stem, force=False, extra=meta)
        if clip:
            with _LOCK:
                line = (_JOBS.get(stem) or {}).get("line")
            _play(clip, meta, line)
    finally:
        with _LOCK:
            _JOBS.pop(stem, None)


def _who(t: dict) -> dict:
    """Which note or document an answer is about: `path` and `at`, or `doc`."""
    if t.get("doc"):
        return {"doc": t["doc"], "title": t.get("title")}
    return {"path": t.get("path"), "at": t.get("at") or 0, "title": t.get("title")}


def listen(rel: str, at: int, line: int | None, bearer: str,
           doc: str = "") -> tuple[bool, dict]:
    """Start listening to a note (or one heading), or with `doc` a project's
    document, from `line` if given. Gated like the speech bar, since it
    makes the phone talk."""
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    if doc:
        ok, got = doc_read.load(doc)
        if not ok:
            return False, got
        fmt = got["fmt"]
        meta = {"doc": got["path"], "title": got["title"]}
    else:
        ok, got = org.read(rel, at, bearer)
        if not ok:
            return False, got
        fmt = _fmt(got["path"])
        meta = {"path": got["path"], "at": at, "title": got["title"]}
    sections = docs.sections_for(got["text"], fmt)
    if not any(s.paras or s.heading for s in sections):
        return False, {"error": "nothing in that note to read", "status": 422}
    stem = docs.note_stem(got["text"], fmt)
    if doc:
        stem = "doc-" + stem.removeprefix("note-")       # its sidecar names the doc
    clip = docs.doc_cache_dir() / f"{stem}.mp3"
    base = {**meta, "line": line}
    if clip.exists() and docs.timings_path(clip).exists():
        state = _play(clip, meta, line)
        if state == "error":
            return False, {"error": "the book channel did not take it", "status": 502}
        return True, {**base, "state": state}
    with _LOCK:
        job = _JOBS.get(stem)
        if job:
            job["line"] = line                 # a second press: the newest place
        else:
            _JOBS[stem] = {**meta, "line": line}
    if not job:
        threading.Thread(target=_render_then_play, args=(stem, sections, meta),
                         daemon=True, name=f"org-listen-{stem}").start()
    return True, {**base, "state": "rendering"}


def _staging() -> dict | None:
    """A note on its way to the phone, as the book channel reports it."""
    try:
        from agent_media_core.sinks.book import read_stage_status
        st = read_stage_status()
    except Exception:
        return None
    if not st or st.get("status") != "copying" or time.time() - float(st.get("ts") or 0) > 90:
        return None
    clip = _clip_of(str(st.get("uri") or ""))
    return docs.read_timings(clip) if clip else None


def now(bearer: str) -> tuple[bool, dict]:
    """What is being listened to, and which line of it."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    np = _playing()
    clip = None if np.get("idle") else _clip_of(np.get("uri") or "")
    if clip:
        t = docs.read_timings(clip) or {}
        pos = np.get("position_ms") or 0
        return True, {"state": "paused" if np.get("paused") else "playing",
                      **_who({**t, "title": t.get("title") or np.get("title")}),
                      "line": _line_at(t, pos / 1000.0), "pos_ms": pos,
                      "dur_ms": np.get("duration_ms") or int((t.get("duration") or 0) * 1000)}
    with _LOCK:
        job = next(iter(_JOBS.values()), None)
    if job:
        return True, {"state": "rendering", **_who(job)}
    staged = _staging()
    if staged and (staged.get("path") or staged.get("doc")):
        return True, {"state": "copying", **_who(staged)}
    return True, {"state": "idle"}


def control(action: str, bearer: str) -> tuple[bool, dict]:
    """Pause, resume or stop the book channel: the reader's own keys."""
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    if action not in _ACTIONS:
        return False, {"error": f"unknown action {action!r}", "status": 400}
    srv = _book()
    if action == "toggle":
        np = _playing()
        action = "resume" if np.get("idle") or np.get("paused") else "pause"
    {"pause": srv.book_pause, "resume": srv.book_resume, "stop": srv.book_stop}[action](
        target=_target())
    _forget_cache()
    return now(bearer)
