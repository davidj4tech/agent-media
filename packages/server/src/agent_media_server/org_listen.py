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
"paused", "ended" (heard to the end: resume starts it again) or "idle". `line` is 0-based in the `text` /org/read gave for the
same `path` and `at`: the reader draws that text, so it can find the line.
A `line` in the POST starts there (the paragraph at or after it); without
one a note picks up where it was left, or from the top once it was heard
to the end. server-contract.md §6.10.

With MEDIA_LISTEN_ENDPOINT set (the server's `book` frames channel,
speech_frames.py) a note plays in the app's own book player instead of the
book channel's mpv: streamed from the clip server, with its headings as the
lock screen's chapters, and a headset's pause reaching it — Termux's mpv has
no media session (David, 15 Oct 2026: "Pausing with my earbuds doesn't seem
to work"). The place it was left is kept here, as the book channel's
observer keeps it for mpv.

Speed and volume are the listener's, not the note's: kept in
listen-prefs.json, written by every press of those knobs (playing or not),
and set on each note as it loads, so they can be set with nothing playing
and a new note does not start at 1× (David, 15 Oct 2026: "change speed and
volume ... even when something's not playing").
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from agent_media_core import docs
from agent_media_core._paths import state_dir

from . import auth, doc_read, org

log = logging.getLogger("agent-media.server.org_listen")

#: A render in progress, by its stem: {"path", "at", "title", "line"}.
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_CACHE_TTL_S = 1.0
_CACHE: list = [0.0, None]
#: Within this much of the end, a note counts as heard: it starts again.
_HEARD_S = 3.0
_ACTIONS = ("pause", "resume", "toggle", "stop", "prev", "next", "seek", "skip",
            "speed+", "speed-", "speed0", "vol+", "vol-", "mute")
#: The sheet's knobs: speed in tenths (as the voice's), volume in tens.
_SPEED_STEP, _SPEED_MIN, _SPEED_MAX = 0.1, 0.5, 3.0
_VOL_STEP = 10
#: mpv's volume runs past 100 (a boost); the phone's book player sits at 150.
_VOL_MAX = 150
#: Paused within this much of the end: heard to the end. The phone's mpv
#: keeps a finished file open, paused at its last frame, so "paused" alone
#: would leave Play resuming at the end, which plays nothing.
_END_S = 1.5
#: Further into a paragraph than this, "prev" goes back to its start.
_RESTART_S = 2.0
#: The app's player tops out at 100 (no boost).
_APP_VOL_MAX = 100
#: The app's player: the last note handed to it, for resume once it is idle.
_LAST: dict = {}
#: The knobs a press may turn with nothing playing.
_KNOBS = ("speed+", "speed-", "speed0", "vol+", "vol-")


def _prefs_path() -> Path:
    return state_dir() / "listen-prefs.json"


def _prefs() -> dict:
    """The speed and volume notes play at: {"speed", "volume"}, each only
    once it has been set."""
    try:
        got = json.loads(_prefs_path().read_text())
    except (OSError, ValueError):
        return {}
    out: dict = {}
    if isinstance(got.get("speed"), (int, float)):
        out["speed"] = round(max(_SPEED_MIN, min(_SPEED_MAX, float(got["speed"]))), 2)
    if isinstance(got.get("volume"), (int, float)):
        out["volume"] = max(0, min(_vol_max(), int(got["volume"])))
    return out


def _keep_prefs(speed: float | None, volume: int | None) -> None:
    prefs = {**_prefs(), **({"speed": speed} if speed is not None else {}),
             **({"volume": volume} if volume is not None else {})}
    try:
        path = _prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs))
        tmp.replace(path)
    except OSError as e:
        log.info("org-listen: could not keep the speed and volume: %s", e)


def _turn(action: str, speed: float | None, volume: int | None) -> tuple[float, int]:
    """A knob's press: the speed and volume it makes of these."""
    speed = float(speed or 1.0)
    volume = int(volume if volume is not None else 100)
    if action.startswith("speed"):
        rate = 1.0 if action == "speed0" else speed + (
            _SPEED_STEP if action == "speed+" else -_SPEED_STEP)
        speed = round(max(_SPEED_MIN, min(_SPEED_MAX, rate)), 2)
    else:
        volume = max(0, min(_vol_max(), volume + (_VOL_STEP if action == "vol+" else -_VOL_STEP)))
    return speed, volume


def _pref_cmds() -> list:
    """The kept speed and volume, as commands for a note being loaded."""
    p = _prefs()
    return ([["set_property", "speed", p["speed"]]] if "speed" in p else []) + (
        [["set_property", "volume", p["volume"]]] if "volume" in p else [])


def _endpoint() -> str:
    """The app's book player (MEDIA_LISTEN_ENDPOINT), or "" for the book channel."""
    import os
    return os.environ.get("MEDIA_LISTEN_ENDPOINT", "").strip()


def _vol_max() -> int:
    return _APP_VOL_MAX if _endpoint() else _VOL_MAX


def _served_url(clip: Path) -> str | None:
    """`clip` on the clip server the phone reads (MEDIA_BOOK_BASEURL_PHONE,
    rooted at MEDIA_BOOK_HTTP_ROOT); None when it is not under that root."""
    import os
    import urllib.parse
    root = os.environ.get("MEDIA_BOOK_HTTP_ROOT", "")
    base = (os.environ.get("MEDIA_LISTEN_BASEURL")
            or os.environ.get("MEDIA_BOOK_BASEURL_PHONE") or "").rstrip("/")
    if not root or not base:
        return None
    try:
        rel = clip.resolve().relative_to(Path(root).expanduser().resolve())
    except ValueError:
        return None
    return f"{base}/{urllib.parse.quote(rel.as_posix())}"


def _keep_place(np: dict) -> None:
    """Where the app's player is in a note, for the next Listen without a line."""
    clip = None if np.get("idle") else _clip_of(np.get("uri") or "")
    if clip:
        _book()._state().set_resume_pos(str(clip), int(np.get("position_ms") or 0))


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


#: What a read asks the player, in one round trip.
_PROPS = ["idle-active", "path", "time-pos", "duration", "pause", "speed",
          "volume", "mute", "eof-reached"]
#: A press acts on a reading this fresh (its position run on by the clock)
#: rather than reading again first: on the phone each read is ~0.9 s.
_PRESS_AGE_S = 6.0
#: A failed read keeps the last good one this long: one dropped packet on a
#: mobile link must not blank the mini player.
_KEEP_S = 15.0


def _socket():
    if _endpoint():
        return _endpoint()
    from agent_media_core.sinks.book import _socket_for
    return _socket_for(_book()._book_target(_target()))


def _read() -> dict | None:
    """The book player, every property at once; None when it did not answer."""
    from agent_media_core.sinks import _mpv_ipc as ipc
    try:
        p = ipc.display_properties(_socket(), _PROPS, timeout=2.0)
    except Exception:
        return None
    if not p:
        return None
    num = lambda k: float(p[k]) if p.get(k) is not None else None  # noqa: E731
    # The app's player goes idle at the end where the phone's mpv stays
    # paused on the last frame: read as that, so the note reads "ended".
    end = bool(p.get("eof-reached")) and bool(p.get("path")) and num("duration") is not None
    if (p.get("idle-active") and not end) or not p.get("path"):
        return {"idle": True}
    return {"idle": False, "uri": p["path"],
            "position_ms": int((num("duration") if end else num("time-pos") or 0) * 1000),
            "duration_ms": int(num("duration") * 1000) if num("duration") is not None else None,
            "paused": end or bool(p.get("pause")), "speed": num("speed") or 1.0,
            "volume": int(round(num("volume"))) if num("volume") is not None else None,
            "muted": bool(p.get("mute"))}


def _send(cmds: list) -> None:
    """Commands to the book player, over one connection."""
    from agent_media_core.sinks import _mpv_ipc as ipc
    ipc.command_batch(_socket(), cmds, timeout=3.0, critical=True)


def _playing(max_age: float | None = None) -> dict:
    """The book player's state, kept a moment: the app polls it, and on the
    phone each read is a round trip over the tailnet. Older than `max_age`
    it is read again; the position of one kept is run on by the clock."""
    max_age = _CACHE_TTL_S if max_age is None else max_age
    with _LOCK:
        at, got = _CACHE
    age = time.monotonic() - at
    if got is None or age >= max_age:
        fresh = _read()
        if fresh is None and got is not None and age < _KEEP_S:
            fresh = got
        got, at, age = fresh or {"idle": True}, time.monotonic(), 0.0
        with _LOCK:
            _CACHE[:] = [at, got]
    if got.get("idle") or got.get("paused") or age <= 0:
        return got
    pos = (got.get("position_ms") or 0) + int(age * 1000 * float(got.get("speed") or 1.0))
    dur = got.get("duration_ms")
    return {**got, "position_ms": min(pos, dur) if dur else pos}


def _predict(np: dict) -> None:
    """What a press made of the player, kept as if read: the next poll reads it."""
    with _LOCK:
        _CACHE[:] = [time.monotonic(), np]


def _forget_cache() -> None:
    with _LOCK:
        _CACHE[:] = [0.0, None]


def _clip_of(uri: str) -> Path | None:
    """The render a playing file is, if it is one with timings. On the phone
    the file is a copy with the same name, so the name is what is matched."""
    if not uri:
        return None
    import urllib.parse
    name = urllib.parse.unquote(Path(uri.split("?", 1)[0]).name)
    clip = docs.doc_cache_dir() / name
    return clip if name.endswith(".mp3") and docs.timings_path(clip).exists() else None


def _start_before_edit(clip: Path, timings: dict | None) -> int:
    """Where an earlier render of the same note or document was left, in ms
    of this one: an edit makes a new render, which has no place of its own
    yet (David, 19 Oct 2026: "when you come back to them they keep the same
    place"). The newest earlier render with a place; its paragraph found
    again by its opening words, else by its line. 0 when there is none, or
    it was heard to the end."""
    def who(t: dict | None) -> tuple:
        t = t or {}
        return (t.get("doc"),) if t.get("doc") else (t.get("path"), t.get("at") or 0)
    me = who(timings)
    if not timings or not any(me):
        return 0
    state = _book()._state()
    try:
        sides = sorted(docs.doc_cache_dir().glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    for side in sides:
        old = side.with_suffix(".mp3")
        if old == clip:
            continue
        t = docs.read_timings(old)
        if not t or who(t) != me:
            continue
        pos = state.get_resume_pos(str(old))
        if not pos:
            continue
        dur = t.get("duration") or 0
        if dur and pos >= (dur - _HEARD_S) * 1000:
            return 0
        row = None
        for r in t.get("timings") or []:
            if r["start"] > pos / 1000.0 + 0.05:
                break
            row = r
        if not row:
            return 0
        for r in timings.get("timings") or []:
            if row.get("text") and r.get("text") == row["text"]:
                return int(r["start"] * 1000)
        return _start_ms(timings, row["line"])
    return 0


def _play(clip: Path, meta: dict, line: int | None) -> str:
    """Put `clip` on the book channel (or the app's book player); the state
    it is then in."""
    srv = _book()
    timings = docs.read_timings(clip)
    now = _playing(_PRESS_AGE_S)
    if line is not None and not now.get("idle") and _clip_of(now.get("uri") or "") == clip:
        # Already this note: a tap on a paragraph is a seek, not a reload.
        to = _start_ms(timings, line) / 1000.0
        _send([["seek", to, "absolute"], ["set_property", "pause", False]])
        _predict({**now, "position_ms": int(to * 1000), "paused": False})
        return "playing"
    if line is not None:
        start = _start_ms(timings, line)
    else:
        start = srv._state().get_resume_pos(str(clip))
        if start is None:
            start = _start_before_edit(clip, timings)
        dur = (timings or {}).get("duration") or 0
        if dur and start >= (dur - _HEARD_S) * 1000:
            start = 0
    if _endpoint():
        return _play_app(clip, meta, timings, start, now)
    r = srv.book_play(str(clip), resume=False, start_ms=start, title=meta["title"],
                      target=_target())
    _forget_cache()
    if r.get("ok"):
        if _pref_cmds():
            try:
                _send(_pref_cmds())
            except Exception as e:                 # it plays; at the old rate
                log.info("org-listen: the book channel kept its speed: %s", e)
        return "playing"
    return "copying" if r.get("fetching") else "error"


def _play_app(clip: Path, meta: dict, timings: dict | None, start: int,
              now: dict) -> str:
    """Load `clip` in the app's book player, from `start` ms: its title, and
    its headings as the chapters its lock-screen next and previous move by."""
    url = _served_url(clip)
    if not url:
        return "error"
    if not now.get("idle"):
        _keep_place(now)                           # the note it replaces
    chapters = [[c["start"], c["title"]] for c in (timings or {}).get("chapters") or []
                if c.get("title") is not None and c.get("start") is not None]
    cmds: list = [["loadfile", url, "replace"],
                  ["set_property", "force-media-title", meta.get("title") or clip.stem]]
    if chapters:
        cmds.append(["set_property", "user-data/agent-media/chapters", chapters])
    if start > 0:
        cmds.append(["seek", start / 1000.0, "absolute"])
    cmds += _pref_cmds()
    try:
        _send(cmds)
    except Exception as e:
        log.info("org-listen: the app's book player did not take %s: %s", clip.name, e)
        return "error"
    _LAST.clear()
    _LAST.update({"clip": clip, "meta": meta})
    _predict({"idle": False, "uri": url, "position_ms": start,
              "duration_ms": int(((timings or {}).get("duration") or 0) * 1000) or None,
              "paused": False, "speed": now.get("speed") or 1.0,
              "volume": now.get("volume"), "muted": bool(now.get("muted")),
              **_prefs()})
    return "playing"


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


def _ended(np: dict) -> bool:
    dur = np.get("duration_ms") or 0
    return bool(np.get("paused")) and dur > 0 and (np.get("position_ms") or 0) >= dur - _END_S * 1000


def now(bearer: str) -> tuple[bool, dict]:
    """What is being listened to, and which line of it."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return True, _answer(_playing())


def _answer(np: dict) -> dict:
    clip = None if np.get("idle") else _clip_of(np.get("uri") or "")
    if clip:
        t = docs.read_timings(clip) or {}
        pos = np.get("position_ms") or 0
        state = "ended" if _ended(np) else "paused" if np.get("paused") else "playing"
        return {"state": state,
                **_who({**t, "title": t.get("title") or np.get("title")}),
                "speed": np.get("speed"), "volume": np.get("volume"),
                "muted": np.get("muted"), "chapters": t.get("chapters") or [],
                "line": _line_at(t, pos / 1000.0), "pos_ms": pos,
                "dur_ms": np.get("duration_ms") or int((t.get("duration") or 0) * 1000)}
    # Nothing loaded: the knobs read what the next note will play at.
    knobs = {"speed": 1.0, "volume": min(100, _vol_max()), **_prefs()}
    with _LOCK:
        job = next(iter(_JOBS.values()), None)
    if job:
        return {"state": "rendering", **_who(job), **knobs}
    staged = _staging()
    if staged and (staged.get("path") or staged.get("doc")):
        return {"state": "copying", **_who(staged), **knobs}
    return {"state": "idle", **knobs}


def control(action: str, bearer: str, value: float | None = None) -> tuple[bool, dict]:
    """The reader's and the mini player's keys. Each is one batch of commands
    to the player, decided from the last reading (run on by the clock), and
    answered with what it made of the player rather than a fresh read: on the
    phone a read is ~0.9 s, and a press used to cost four or five of them
    (David, 15 Oct 2026: "The player controls are quite slow to respond")."""
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    if action not in _ACTIONS:
        return False, {"error": f"unknown action {action!r}", "status": 400}
    if action in ("seek", "skip") and value is None:
        return False, {"error": f"{action} needs {'to' if action == 'seek' else 'by'}", "status": 400}
    srv = _book()
    app = bool(_endpoint())
    if action == "stop":
        if app:
            _keep_place(_playing(_PRESS_AGE_S))
            try:
                _send([["stop"]])
            except Exception as e:
                return False, {"error": f"the player did not take it ({e})", "status": 502}
        else:
            srv.book_stop(target=_target())        # saves the place, clears now-playing
        _predict({"idle": True})
        return True, _answer({"idle": True})
    np = _playing(_PRESS_AGE_S)
    if np.get("idle") and action in _KNOBS:
        # Nothing to send it to: kept for the next note.
        p = _prefs()
        _keep_prefs(*_turn(action, p.get("speed"), p.get("volume")))
        return True, _answer(np)
    if np.get("idle"):
        if action in ("resume", "toggle"):
            if app:
                if not _LAST:
                    return False, {"error": "nothing is being listened to", "status": 409}
                _play(_LAST["clip"], _LAST["meta"], None)
            else:
                srv.book_resume(target=_target())  # reopens the last one
            _forget_cache()
            return now(bearer)
        return False, {"error": "nothing is being listened to", "status": 409}
    if action == "toggle":
        action = "resume" if np.get("paused") else "pause"
    pos = (np.get("position_ms") or 0) / 1000.0
    dur = (np.get("duration_ms") or 0) / 1000.0
    cmds: list = []
    new = dict(np)

    def seek(to: float):
        to = max(0.0, min(to, dur - 0.5) if dur else to)
        cmds.append(["seek", to, "absolute"])
        new["position_ms"] = int(to * 1000)

    if action == "pause":
        cmds.append(["set_property", "pause", True])
        new["paused"] = True
        if app:
            _keep_place(np)
    elif action == "resume":
        if _ended(np):
            seek(0.0)                              # heard to the end: again from the top
        cmds.append(["set_property", "pause", False])
        new["paused"] = False
    elif action == "seek":
        seek(value)
    elif action == "skip":
        seek(pos + value)
    elif action in ("prev", "next"):
        # A paragraph back or on, as a sentence is for a reply. "prev"
        # restarts the paragraph when past its start.
        clip = _clip_of(np.get("uri") or "")
        rows = ((docs.read_timings(clip) or {}).get("timings") or []) if clip else []
        if not rows:
            return False, {"error": "nothing is being listened to", "status": 409}
        cur = max([i for i, r in enumerate(rows) if r["start"] <= pos + 0.05] or [0])
        if action == "next":
            seek(rows[min(cur + 1, len(rows) - 1)]["start"])
        else:
            seek(rows[cur]["start"] if pos - rows[cur]["start"] > _RESTART_S
                 else rows[max(cur - 1, 0)]["start"])
    elif action == "mute":
        new["muted"] = not np.get("muted")
        cmds.append(["set_property", "mute", new["muted"]])
    else:
        speed, volume = _turn(action, np.get("speed"), np.get("volume"))
        if action.startswith("speed"):
            new["speed"] = speed
            cmds.append(["set_property", "speed", speed])
            _keep_prefs(speed, None)
        else:
            new["volume"] = volume
            cmds.append(["set_property", "volume", volume])
            _keep_prefs(None, volume)
    try:
        _send(cmds)
    except Exception as e:
        _forget_cache()
        return False, {"error": f"the player did not take it ({e})", "status": 502}
    _predict(new)
    return True, _answer(new)
