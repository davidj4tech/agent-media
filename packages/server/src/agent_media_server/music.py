"""What is playing, and its controls: the app's Media tab.

server-contract.md §6.9a. Step 1 of docs/proposals/2026-09-28-music-tab.md
(David, 28 Sep 2026: "Let's build the Media tab UI").

  GET  /music                → {"ok", "now": {...}, "chapters": [...], "where": {...}}
  GET  /music/recent[?kind=music|book|podcast]
                             → {"ok", "kind", "items": [{"uri", "title", "at", "session", …}]}
  POST /music {"action", …}  → the same, after doing it

`radio` is the station (agent_media_core.radio; docs/proposals/
2026-09-29-radio.md): `{"on", "seed", "next": [{id, title, channel, state}],
"more"}`. `{"action": "radio"}` starts one from what is playing (or `uri`),
`{"action": "radio", "dj": true}` starts the DJ's station (radio_dj), `{"action": "radio", "off": true}` ends it, `{"action": "radio", "play": id}`
plays a song from its list now, and `{"action": "dislike"}` is 👎:
that song off the station, and the next one on.

`where.rooms` is the house speakers one by one (agent_media_core.music_rooms):
`{"action": "room", "id", "on"?, "volume"?}` switches one on or off and sets
how loud it is. `{"action": "move"}` takes what is playing to where the next
play goes (the picker's choice), at the same spot: it runs in the background
and the answer's `move` says how it went (David, 20 Oct 2026).

`now` is `media music status --json`: the live player's track, position and
state, whichever player that is (Sasonica's own, the Termux mpv, Mopidy), so
the tab and the desk popup can never disagree. `where` is the music block of
`GET /audio/targets`, for the tab's picker.

The controls are the `media music` verbs, run as the CLI runs them rather
than re-implemented here: they already follow the live player, keep the play
history and likes, and know each player's quirks (the app's socket takes
`seek`, not a time-pos write). Nothing here knows which player that is.

Reading asks the phone over the tailnet (two players, ~0.5-1 s each at
worst), so an answer is kept for a moment: the tab polls while it is open.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

from . import audio, auth

#: `action` → the `media music` verb. `seek` and `seek-by` take a number.
ACTIONS = {
    "pause": "pause", "resume": "resume", "toggle": "toggle",
    "next": "next", "prev": "prev", "stop": "stop", "like": "like",
    "seek": "seek", "seek-by": "seek",
    # Recently played: put one on again, now or after what is playing.
    "play": "play", "add": "play",
    # A book or podcast from Recently played: `media book play`, resumed.
    "book": "book",
    # The station, run here rather than by the CLI (radio.py).
    "radio": "radio", "dislike": "dislike",
    # The house speakers one by one, and what is playing to another place.
    "room": "room", "move": "move",
}

_CACHE_TTL_S = 2.5
#: A read that finds nothing right after one that found a track is believed
#: only this long later: over the phone's link (~0.5 s each way on mobile) a
#: read can fail, and a failed read looks like "nothing playing" — the tab
#: flickered between the two (David, 28 Sep 2026).
_STICKY_S = 12.0
_CMD_TIMEOUT_S = 30.0
_LOCK = threading.Lock()
_CACHE: list = [0.0, None]
#: The last answer that found a track: `(at, (now, chapters))`.
_LAST: list = [None]


def _status() -> dict:
    """`media music status --json`, in-process."""
    from agent_media_core import cli
    from agent_media_core.sinks.music import SinkMusic
    from agent_media_core.sinks.music_router import SinkMusicRouter

    return cli._music_status_json(SinkMusicRouter(SinkMusic()))


def _run(argv: list[str]) -> tuple[bool, str]:
    """`media music <argv>`, as the desk runs it."""
    try:
        r = subprocess.run([sys.executable, "-m", "agent_media_core.cli", "music", *argv],
                           capture_output=True, text=True, timeout=_CMD_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()[-300:]


def _run_book(uri: str) -> tuple[bool, str]:
    """`media book play <uri>` (resumes where it was left)."""
    try:
        r = subprocess.run([sys.executable, "-m", "agent_media_core.cli", "book", "play", "--", uri],
                           capture_output=True, text=True, timeout=_CMD_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()[-300:]


def _reset_cache(forget: bool = False) -> None:
    with _LOCK:
        _CACHE[:] = [0.0, None]
        if forget:
            _LAST[0] = None


def _now() -> tuple[dict, list[dict]]:
    """The status and the live file's chapters, kept together a moment."""
    t = time.monotonic()
    with _LOCK:
        if _CACHE[1] is not None and t - _CACHE[0] < _CACHE_TTL_S:
            return _CACHE[1]
    try:
        now = _status()
    except Exception as e:  # noqa: BLE001 — the tab must answer without it
        print(f"music: status failed: {e}", file=sys.stderr)
        now = {"backend": None}
    got = (now, _chapters(now))
    with _LOCK:
        last = _LAST[0]
        if now.get("pos_ms") is None and last and time.monotonic() - last[0] < _STICKY_S:
            got = last[1]
        elif now.get("pos_ms") is not None:
            _LAST[0] = (time.monotonic(), got)
        _CACHE[:] = [time.monotonic(), got]
    return got


def _chapters(now: dict) -> list[dict]:
    """A mix's tracks, when the live file is one the phone can read chapters
    from: ``[{"title", "start_ms"}]``, else []."""
    try:
        from agent_media_core.sinks import music_sasonica

        path = now.get("path") or ""
        if not music_sasonica.phone_path(path):
            return []
        return [{"title": c["title"], "start_ms": int(c["start"] * 1000)}
                for c in music_sasonica.chapters(path)]
    except Exception:  # noqa: BLE001 — a list the tab can do without
        return []


def _art(now: dict) -> str | None:
    from agent_media_core.music_recent import art

    return art(now.get("uri") or "") or art(now.get("media_id") or "")


def _radio() -> dict:
    from agent_media_core import radio

    try:
        return radio.snapshot()
    except Exception as e:  # noqa: BLE001 — the tab can do without it
        print(f"music: radio snapshot failed: {e}", file=sys.stderr)
        return {"on": False, "seed": None, "next": [], "available": False}


_ROOMS_TTL_S = 2.5
_ROOMS: list = [0.0, None]


def _rooms(fresh: bool = False) -> list[dict] | None:
    """The house speakers (music_rooms.rooms), kept a moment; None when the
    Snapcast server cannot be reached, [] when this host has no rooms."""
    from agent_media_core import music_rooms, snapcast

    t = time.monotonic()
    with _LOCK:
        if not fresh and _ROOMS[1] is not None and t - _ROOMS[0] < _ROOMS_TTL_S:
            return _ROOMS[1]
    try:
        got = music_rooms.rooms(timeout=1.5)
    except (snapcast.SnapcastError, OSError) as e:
        print(f"music: rooms failed: {e}", file=sys.stderr)
        return None
    with _LOCK:
        _ROOMS[:] = [time.monotonic(), got]
    return got


#: The last move: `{"to", "state": "moving" | "done" | "failed", "error", "at"}`.
_MOVE: dict = {}
#: A finished move is reported this long, so the tab that asked sees it.
_MOVE_SHOWN_S = 60.0


def _move_state() -> dict | None:
    with _LOCK:
        m = dict(_MOVE)
    if not m or (m["state"] != "moving" and time.time() - m["at"] > _MOVE_SHOWN_S):
        return None
    return {k: m[k] for k in ("to", "state", "error")}


def _answer() -> dict:
    now, chapters = _now()
    where = audio.channel_block("music")
    out = {"now": now, "art": _art(now), "chapters": chapters,
           "where": {**where, "rooms": _rooms()}, "radio": _radio()}
    move = _move_state()
    if move:
        out["move"] = move
    return out


def _room_control(body: dict) -> tuple[bool, dict]:
    """`room`: one house speaker on or off, and how loud."""
    from agent_media_core import music_rooms, snapcast

    cid, on, volume = body.get("id"), body.get("on"), body.get("volume")
    if not isinstance(cid, str) or not cid:
        return False, {"error": "id must be a room's id", "status": 400}
    if on is not None and not isinstance(on, bool):
        return False, {"error": "on must be true or false", "status": 400}
    if volume is not None and (isinstance(volume, bool) or not isinstance(volume, (int, float))
                               or not 0 <= volume <= 100):
        return False, {"error": "volume must be 0-100", "status": 400}
    if on is None and volume is None:
        return False, {"error": "give on or volume", "status": 400}
    try:
        music_rooms.set_room(cid, on=on, volume=None if volume is None else int(round(volume)))
    except ValueError as e:
        return False, {"error": str(e), "status": 400}
    except (snapcast.SnapcastError, OSError) as e:
        return False, {"error": f"the speakers did not answer: {e}", "status": 502, **_answer()}
    _rooms(fresh=True)
    return True, _answer()


#: How long a move waits for the new place to start playing.
_MOVE_WAIT_S = 90.0


def _replay_uri(now: dict) -> str | None:
    """What to play elsewhere to carry on with `now`: the track asked for, or
    the YouTube video its file is; None for anything else (a book, a file)."""
    uri = now.get("uri") or ""
    if uri.startswith(("yt:", "http://", "https://")):
        return uri
    from agent_media_core.sinks.music_fetch import watch_id

    vid = watch_id(now.get("media_id") or "") or watch_id(uri)
    return f"yt:https://www.youtube.com/watch?v={vid}" if vid else None


def _kind(where: str) -> str:
    """`_resolve_music_where`'s answer, or a status backend, as rooms/phone."""
    return "rooms" if where in ("rooms", "mopidy") else "phone"


def _move(uri: str, to: str, from_kind: str, pos_s: float, paused: bool) -> None:
    """Pause where it is, play `uri` at `to`, and once it plays there stop
    the old place and seek to where it was. Back where it was on a failure."""
    from agent_media_core import cli
    from agent_media_core.sinks.music import SinkMusic
    from agent_media_core.sinks.music_router import SinkMusicRouter

    def done(state: str, error: str | None = None) -> None:
        with _LOCK:
            _MOVE.update(state=state, error=error, at=time.time())
        _reset_cache()
        print(f"music: move to {to}: {state}{f' ({error})' if error else ''}", file=sys.stderr)

    _run(["pause"])
    ok, said = _run(["play", uri, "--where", to])
    if not ok:
        _run(["resume"])
        return done("failed", said or "play failed")
    from agent_media_core.sinks.music_fetch import watch_id

    # A track left loaded there is not this one starting.
    want = watch_id(uri)
    deadline = time.monotonic() + _MOVE_WAIT_S
    while True:
        try:
            st = cli._music_status_json(SinkMusicRouter(SinkMusic()), where=_kind(to))
        except Exception:  # noqa: BLE001 — keep asking until the deadline
            st = {}
        if st.get("pos_ms") is not None and _kind(st.get("backend") or "") == _kind(to) \
                and (not want or st.get("media_id") == want):
            break
        if time.monotonic() > deadline:
            _run(["resume"])
            return done("failed", "it did not start there")
        time.sleep(1.0)
    # The old place: the house's Mopidy is stopped directly (a phone player
    # is the live one now); a phone player is still the live one, so the
    # verb reaches it.
    if from_kind == "rooms":
        try:
            SinkMusic().stop()
        except OSError:
            pass
    else:
        _run(["stop"])
    if pos_s > 2:
        _run(["seek", _clock(pos_s)])
    if paused:
        _run(["pause"])
    done("done")


def _move_control() -> tuple[bool, dict]:
    """`move`: what is playing to where the next play goes."""
    from agent_media_core import radio
    from agent_media_core.cli import _resolve_music_where

    now, _ = _now()
    if now.get("pos_ms") is None:
        return True, _answer()
    to = _resolve_music_where("default")
    if _kind(to) == _kind(now.get("backend") or ""):
        # Already there: phone players are one place to a move.
        return True, _answer()
    station = radio.read()
    if station.get("on"):
        # The radio goes with it: the station's own player is moved, so it
        # keeps its list (a play there would end it).
        if to not in radio.WHERES or station.get("where") == "handoff":
            return False, {"error": f"the radio does not play on {to}", "status": 409, **_answer()}
        return _begin_move(to, _move_station, to, int(now.get("pos_ms") or 0),
                           bool(now.get("paused")))
    uri = _replay_uri(now)
    if not uri:
        return False, {"error": "this can only play where it is", "status": 409, **_answer()}
    return _begin_move(to, _move, uri, to, _kind(now.get("backend") or ""),
                       (now.get("pos_ms") or 0) / 1000, bool(now.get("paused")))


def _begin_move(to: str, fn, *args) -> tuple[bool, dict]:
    """`fn(*args)` in the background, reported as the move to `to`."""
    with _LOCK:
        if _MOVE.get("state") == "moving":
            return False, {"error": "a move is under way", "status": 409}
        _MOVE.clear()
        _MOVE.update(to=to, state="moving", error=None, at=time.time())
    _spawn(fn, *args)
    return True, _answer()


def _move_station(to: str, pos_ms: int, paused: bool) -> None:
    """The radio to `to` (agent_media_core.radio.move), reported as a move."""
    from agent_media_core import radio

    from . import radio as loop

    try:
        radio.move(to, pos_ms, paused)
        state, error = "done", None
    except (ValueError, RuntimeError) as e:
        state, error = "failed", str(e)
    with _LOCK:
        _MOVE.update(state=state, error=error, at=time.time())
    _reset_cache()
    loop.wake()
    print(f"music: radio to {to}: {state}{f' ({error})' if error else ''}", file=sys.stderr)


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, name="music-move", daemon=True).start()


def _radio_control(body: dict) -> tuple[bool, dict]:
    """`radio` (start, or `off`) and `dislike`."""
    from agent_media_core import radio
    from agent_media_core.cli import _resolve_music_where

    from . import radio as loop

    if body.get("action") == "dislike":
        if radio.dislike() is None:
            # No station: 👎 is a skip.
            _run(["next"])
        _reset_cache()
        loop.wake()
        return True, _answer()
    if body.get("off"):
        radio.stop()
        return True, _answer()
    if "play" in body:
        # A song from Up next, now.
        vid = body.get("play")
        if not isinstance(vid, str) or not radio._ID.match(vid):
            return False, {"error": "play must be a song id from the station's list", "status": 400}
        try:
            if radio.play(vid) is None:
                return False, {"error": "no station is on", "status": 409, **_answer()}
        except ValueError as e:
            return False, {"error": str(e), "status": 400, **_answer()}
        except RuntimeError as e:
            return False, {"error": str(e), "status": 502, **_answer()}
        _reset_cache()
        loop.wake()
        return True, _answer()
    if not radio.available():
        return False, {"error": radio.OFF_HERE, "status": 409, **_answer()}
    if body.get("dj"):
        # The DJ's station: what plays now plays on until its first pick.
        # On the named player, else the default (the hand-off player where
        # YouTube is off; MEDIA_RADIO_PLAYER).
        where = body.get("player") if body.get("player") in radio.radio_io.PLAYERS \
            else radio.radio_io.default_player(_resolve_music_where("default"))
        try:
            radio.start_dj(where)
        except ValueError as e:
            return False, {"error": str(e), "status": 409, **_answer()}
        loop.wake()
        return True, _answer()
    uri = body.get("uri")
    playing = False
    if uri is None:
        now, _ = _now()
        uri = now.get("uri") or now.get("media_id") or ""
        playing = True
    if not isinstance(uri, str) or not uri.strip() or uri.lstrip().startswith("-"):
        return False, {"error": "uri must be a YouTube track", "status": 400}
    where = _resolve_music_where("default")
    if where not in radio.WHERES:
        return False, {"error": f"the radio does not play on {where}", "status": 409, **_answer()}
    if not playing:
        done, said = _run(["play", uri.strip()])
        if not done:
            return False, {"error": said or "play failed", "status": 502, **_answer()}
    try:
        radio.start(uri.strip(), where, playing=True)
    except ValueError as e:
        return False, {"error": str(e), "status": 400, **_answer()}
    except RuntimeError as e:
        return False, {"error": str(e), "status": 502, **_answer()}
    _reset_cache()
    loop.wake()
    return True, _answer()


def now(bearer: str) -> tuple[bool, dict]:
    """`GET /music` — gated like /speech/now."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return True, _answer()


def recent(bearer: str, kind: str = "music") -> tuple[bool, dict]:
    """`GET /music/recent` — gated like /speech/now: what was played, newest
    first, one per track, with the conversation that put it on
    (agent_media_core.music_recent). `kind` is music, book or podcast."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    from agent_media_core import music_recent

    if kind not in music_recent.KINDS:
        return False, {"error": "kind must be music, book or podcast", "status": 400}
    try:
        items = music_recent.recent(kind=kind)
    except Exception as e:  # noqa: BLE001 — an empty list, not a 500
        print(f"music: recent failed: {e}", file=sys.stderr)
        items = []
    return True, {"kind": kind, "items": items}


def _played_book(uri: str) -> bool:
    """Whether `uri` is one Recently played lists — the only books this
    route will start, so it is not a way to play any file on the host."""
    from agent_media_core import music_recent

    return any(i.get("uri") == uri for k in ("book", "podcast")
               for i in music_recent.recent(kind=k, limit=200))


def _clock(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def control(body: dict, bearer: str) -> tuple[bool, dict]:
    """`POST /music` — gated like /speech/ctl: the listener's controls."""
    ok, err = auth.may_control_speech(bearer)
    if not ok:
        return False, err
    action = str(body.get("action") or "")
    if action not in ACTIONS:
        return False, {"error": "unknown action", "status": 400}
    if action in ("radio", "dislike"):
        return _radio_control(body)
    if action == "room":
        return _room_control(body)
    if action == "move":
        return _move_control()
    argv = [ACTIONS[action]]
    if action in ("seek", "seek-by"):
        key = "to" if action == "seek" else "by"
        v = body.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False, {"error": f"{key} must be a number of seconds", "status": 400}
        if action == "seek":
            argv.append(_clock(v))
        else:
            argv.append(f"{'+' if v >= 0 else '-'}{abs(int(round(v)))}")
    elif action == "prev":
        argv.append("--restart-first")
    elif action == "book":
        uri = body.get("uri")
        if not isinstance(uri, str) or not uri.strip() or not _played_book(uri.strip()):
            return False, {"error": "uri must be a book or podcast from Recently played", "status": 400}
        done, said = _run_book(uri.strip())
        _reset_cache()
        if not done:
            print(f"music: book play failed: {said}", file=sys.stderr)
            return False, {"error": said or "book failed", "status": 502, **_answer()}
        return True, _answer()
    elif action in ("play", "add"):
        uri = body.get("uri")
        # A name the CLI would read as an option is not a track.
        if not isinstance(uri, str) or not uri.strip() or uri.lstrip().startswith("-") \
                or not uri.strip().startswith(("yt:", "http://", "https://")):
            return False, {"error": "uri must be a yt: or web address", "status": 400}
        argv.append(uri.strip())
        if action == "add":
            argv.append("--add")
    done, said = _run(argv)
    _reset_cache(forget=action == "stop")
    if not done:
        print(f"music: {' '.join(argv)} failed: {said}", file=sys.stderr)
        return False, {"error": said or f"{action} failed", "status": 502, **_answer()}
    return True, _answer()
