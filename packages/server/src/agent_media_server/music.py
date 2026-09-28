"""What is playing, and its controls: the app's Media tab.

server-contract.md §6.9a. Step 1 of docs/proposals/2026-09-28-music-tab.md
(David, 28 Sep 2026: "Let's build the Media tab UI").

  GET  /music                → {"ok", "now": {...}, "chapters": [...], "where": {...}}
  GET  /music/recent         → {"ok", "items": [{"uri", "title", "at", "session", …}]}
  POST /music {"action", …}  → the same, after doing it

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


def _answer() -> dict:
    now, chapters = _now()
    return {"now": now, "chapters": chapters, "where": audio.channel_block("music")}


def now(bearer: str) -> tuple[bool, dict]:
    """`GET /music` — gated like /speech/now."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return True, _answer()


def recent(bearer: str) -> tuple[bool, dict]:
    """`GET /music/recent` — gated like /speech/now: what was played, newest
    first, one per track, with the conversation that put it on
    (agent_media_core.music_recent)."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    from agent_media_core import music_recent

    try:
        items = music_recent.recent()
    except Exception as e:  # noqa: BLE001 — an empty list, not a 500
        print(f"music: recent failed: {e}", file=sys.stderr)
        items = []
    return True, {"items": items}


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
