"""The popup's transport, as data — for a surface that is not a terminal.

`media-popup` is the control surface everything else is measured against, and
almost none of it can be ported: half its keys are about tmux (replay the clip
at the copy-mode cursor, page the pane behind, jump to the pane that said it)
and one of them shells out to `tmux new-window`. What *is* portable is the part
that is really about the channels — what is playing, how far in, and the verbs
that change it.

So this module is that part, split from any particular front end:

  `channels()`  one snapshot of all three channels, in one shape
  `control()`   a verb, from a whitelist, turned into a `media` command

Two rules inherited from the popup, both load-bearing:

**Reads never write.** `_music_status_json`'s docstring is explicit that the
pipeline stays the only writer and a surface renders what it got. `channels()`
holds to that: it reads three sources and normalises them, and touches nothing.

**Every field is nullable.** A channel whose backend is down answers None
rather than raising, because a control surface that cannot render half its
screen should still render the other half. The popup's redraw path has the same
property, for the same reason — it is drawn many times a second and must never
show a traceback.

The verbs are a whitelist rather than a passthrough. The listener is loopback
and single-user, but `media` is a large CLI and this endpoint's job is
transport, not remote execution: a surface that can run any subcommand is a
different security question than one that can press pause.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

#: `(channel, action)` pairs a surface may ask for, mapped to the argv `media`
#: already understands. `{}` is where the argument lands; a verb with no `{}`
#: takes none and any argument is ignored.
VERBS = {
    # Music and book share the transport the popup gives them.
    ("music", "toggle"): ["music", "toggle"],
    ("music", "pause"): ["music", "pause"],
    ("music", "resume"): ["music", "resume"],
    ("music", "stop"): ["music", "stop"],
    ("music", "next"): ["music", "next"],
    ("music", "prev"): ["music", "prev", "--restart-first"],
    ("music", "seek"): ["music", "seek", "{}"],
    ("music", "volume"): ["music", "volume", "{}"],
    ("music", "speed"): ["music", "speed", "{}"],
    ("music", "chapter"): ["music", "chapter", "{}"],

    ("book", "toggle"): ["book", "resume"],   # resume reopens when idle
    ("book", "pause"): ["book", "pause"],
    ("book", "resume"): ["book", "resume"],
    ("book", "stop"): ["book", "stop"],
    ("book", "next"): ["book", "next"],
    ("book", "prev"): ["book", "prev", "--restart-first"],
    ("book", "seek"): ["book", "seek", "{}"],
    ("book", "skip"): ["book", "skip", "{}"],
    ("book", "speed"): ["book", "speed", "{}"],

    # Speech is not a player in the same sense — there is no queue to skip
    # through and no volume of its own worth exposing here — so it gets the
    # verbs the popup gives it and no more.
    ("speech", "toggle"): ["toggle"],
    ("speech", "seek"): ["seek", "{}"],
    ("speech", "volume"): ["volume", "{}"],
    ("speech", "speed"): ["speed", "{}"],
    ("speech", "skip"): ["skip", "{}"],
    ("speech", "jump"): ["jump", "{}"],
    ("speech", "replay"): ["replay", "{}"],
    ("speech", "mute"): ["mute"],
    ("speech", "flush"): ["speech-flush"],
}

CHANNELS = ("speech", "music", "book")


class ControlError(Exception):
    """The verb cannot be performed, with a reason fit to show someone."""


def _f(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms(seconds) -> Optional[int]:
    s = _f(seconds)
    return None if s is None else int(s * 1000)


def channels() -> dict:
    """One snapshot of all three channels, normalised to one shape.

    The three sources disagree about everything — names, units, what "idle"
    means — because each grew for its own consumer. Normalising here rather
    than in the front end means a second front end cannot normalise it
    differently, which is the failure this repo keeps finding elsewhere (two
    renderings of one history, two paths into the sinks).
    """
    return {
        "speech": _speech(),
        "music": _music(),
        "book": _book(),
        "focus": _focus(),
    }


def verbs(channel: str) -> list:
    """Which actions this channel actually accepts, sorted.

    Read straight off VERBS, because a front end that hardcodes its own list
    gets it wrong in the direction that shows: the phone drew a `mute` button on
    the book channel for a fortnight, and pressing it could only ever say no —
    there is no `("book", "mute")` and there is no reason for one, a book being
    a thing you pause rather than silence. A surface should be able to ask.
    """
    return sorted(action for (name, action) in VERBS if name == channel)


def _blank(channel: str) -> dict:
    return {"channel": channel, "idle": True, "playing": False, "paused": None,
            "title": None, "chapter": None, "pos_ms": None, "dur_ms": None,
            "speed": None, "volume": None, "backend": None,
            "verbs": verbs(channel)}


def _speech() -> dict:
    out = _blank("speech")
    try:
        from ..cli import _speech_display_state

        idle, pos, dur, paused, muted, speed, playing = _speech_display_state(
            allow_remote=False, prefer_local=True)
        out.update(idle=bool(idle), paused=bool(paused),
                   playing=bool(playing) and not paused,
                   pos_ms=_ms(pos), dur_ms=_ms(dur), speed=_f(speed),
                   muted=bool(muted))
    except Exception as e:  # noqa: BLE001 — a surface renders what it got
        log.debug("speech snapshot failed: %s", e)
        return out
    try:
        from ..state import StateStore

        rows = StateStore().recent_history(sink="speech", limit=1)
        if rows:
            text = (rows[0].get("text") or "").strip()
            out["title"] = text.splitlines()[0][:120] if text else None
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..state import StateStore

        # The popup's "you have N things muted" badge: a durable mute on a pane
        # you are not looking at should not stay forgotten on a phone either.
        #
        # Counted, not len()'d. `list_mutes` returns two buckets —
        # {"panes": {...}, "sessions": {...}} — so len() was 2 whatever was in
        # them, and the phone showed "2 muted" from the day the badge shipped
        # with nothing muted anywhere. A number that never moves is worse than
        # no number: it reads as information.
        #
        # Sessions count as well as panes. The key kept its old name for one
        # release and then stopped lying about what it holds.
        mutes = StateStore().list_mutes() or {}
        out["muted_elsewhere"] = sum(
            1 for bucket in mutes.values() for muted in bucket.values() if muted)
    except Exception:  # noqa: BLE001
        pass
    return out


def _music() -> dict:
    out = _blank("music")
    try:
        from ..cli import _music_status_json
        from ..sinks.music import SinkMusic
        from ..sinks.music_router import SinkMusicRouter

        snap = _music_status_json(SinkMusicRouter(SinkMusic()))
    except Exception as e:  # noqa: BLE001
        log.debug("music snapshot failed: %s", e)
        return out
    paused = snap.get("paused")
    out.update(backend=snap.get("backend"), title=snap.get("title"),
               chapter=snap.get("chapter"), pos_ms=snap.get("pos_ms"),
               dur_ms=snap.get("dur_ms"), paused=paused,
               speed=_f(snap.get("speed")), volume=snap.get("volume"),
               held=bool(snap.get("held")),
               idle=snap.get("backend") is None or snap.get("uri") is None,
               uri=snap.get("uri"))
    out["playing"] = bool(snap.get("uri")) and paused is False
    return out


def _book() -> dict:
    out = _blank("book")
    try:
        from ..mcp_server import book_now_playing

        np = book_now_playing(target="")
    except Exception as e:  # noqa: BLE001
        log.debug("book snapshot failed: %s", e)
        return out
    if np.get("idle"):
        return out
    paused = bool(np.get("paused"))
    out.update(idle=False, paused=paused, playing=not paused,
               uri=np.get("uri"),
               title=np.get("title") or np.get("media_title"),
               chapter=np.get("chapter_title"),
               pos_ms=np.get("position_ms"), dur_ms=np.get("duration_ms"),
               speed=_f(np.get("speed")))
    return out


def _focus() -> Optional[str]:
    """Which channel is in front, as the popup's `focus` line reports it."""
    try:
        from ..state import StateStore

        return StateStore().get_focus()
    except Exception:  # noqa: BLE001
        return None


def chapters() -> list:
    """The live music track's chapters, 1-based, with the current one marked.

    Music only — the popup says so too. An MPD/GStreamer stream has none, and
    neither does a book: an empty list is the honest answer, not an error.
    """
    try:
        from ..cli import _music_mpv_chapters

        got = _music_mpv_chapters()
    except Exception as e:  # noqa: BLE001
        log.debug("chapter read failed: %s", e)
        return []
    if not got:
        return []
    _ep, chaps, cur = got
    rows = []
    for i, ch in enumerate(chaps, start=1):
        title = ""
        start = None
        if isinstance(ch, dict):
            title = str(ch.get("title") or "").strip()
            start = _f(ch.get("time"))
        rows.append({"number": i, "title": title or f"Chapter {i}",
                     "start_ms": None if start is None else int(start * 1000),
                     "current": (cur is not None and i == int(cur) + 1)})
    return rows


def control(channel: str, action: str, arg: str = "", runner=None) -> int:
    """Perform one whitelisted verb. Returns the command's exit code.

    `runner` exists for tests; by default this is `media` itself, in process,
    so a surface's pause is the same code path as a typed one.
    """
    channel = (channel or "").strip()
    action = (action or "").strip()
    argv = VERBS.get((channel, action))
    if argv is None:
        raise ControlError(f"no such control: {channel} {action}".strip())
    arg = (arg or "").strip()
    filled = []
    for part in argv:
        if part == "{}":
            if not arg:
                raise ControlError(f"{channel} {action} needs a value")
            filled.append(arg)
        else:
            filled.append(part)
    if runner is None:
        from ..cli import main as runner
    return runner(filled)
