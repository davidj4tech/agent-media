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

**Speech history belongs to the origin.** Everything else here is answered by
the machine the surface is running on, and rightly: the player is local, so its
position, its pause and its speed are local facts. The *words* are not. They are
produced where the conversation happens, and a render host records only what it
rendered itself — which on the phone stopped being anything in July, when the
lane moved to rendering on the hub and pushing audio over. So the card showed a
July sentence under a clip playing now, and the clip picker offered July.
`_origin_clips` asks the origin instead, and the two verbs that act on that list
are run there too. See `ORIGIN_VERBS`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
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
    ("book", "chapter"): ["book", "chapter", "{}"],

    # Speech is not a player in the same sense — there is no queue to page
    # through — so it gets the verbs the popup gives it and no more.
    #
    # `prev`/`next` are the exception, and they are named for the button rather
    # than the thing: on speech they step the reader a sentence, which is the
    # popup's h/l. The phone drew no back-or-forward control at all until
    # 2026-08-17 because the app hides a verb the channel does not publish, and
    # the one speech does publish was called `skip` and took its direction as
    # an argument the transport row has no way to send. So the direction is
    # baked in here, where the argv is written anyway.
    #
    # `chapter` is the other exception, and the same kind: speech has no
    # chapters, but the chapter button's question — what is in this, take me
    # to that part — has an answer on this channel, and it is the history.
    # So the picker lists the clips already said and choosing one replays it
    # by history id, which is what `replay --id` exists for.
    ("speech", "toggle"): ["toggle"],
    ("speech", "chapter"): ["replay", "--id", "{}"],
    ("speech", "next"): ["skip", "--dir", "1"],
    ("speech", "prev"): ["skip", "--dir", "-1"],
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

#: Verbs that address the speech *history* rather than the local player, and so
#: have to run where that history is. `replay` and `chapter` both name a past
#: turn; on a render host the only turns named locally are the ones it rendered
#: itself, so both were reaching into July. Run on the origin they go through
#: the ordinary push path and come back out of this host's speakers, which is
#: what pressing them here means.
ORIGIN_VERBS = frozenset({("speech", "replay"), ("speech", "chapter")})

#: How stale the origin's clip list may be before it is asked again. The card's
#: title reads it on every poll (about once a second while the app is open) and
#: the picker forces a fresh one, so this is the cost of *looking at* the app,
#: not of running it: nothing polls when nobody is looking.
ORIGIN_CLIPS_TTL_S = 20.0

_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]

#: Enough to scroll, few enough that the ask stays one small round trip.
_CLIPS_N = 40

#: `{"at": when, "rows": rows|None}`. A failed ask is cached too — otherwise a
#: hub that is down or asleep is re-dialled once a second for as long as the
#: app is open, at eight seconds of timeout apiece.
_clips_cache: dict = {"at": 0.0, "rows": None}


class ControlError(Exception):
    """The verb cannot be performed, with a reason fit to show someone."""


def _origin_host() -> Optional[str]:
    """The host that produces the speech, when it is not this one.

    None means "answer locally": either this host is the origin, or nothing
    declares one — a standalone install is origin+render+observe and has
    nobody to ask. Roles come from the config file, so the hostname still
    appears in exactly one place.
    """
    try:
        from .. import config

        roles = config.host_roles()
        if roles is None or "origin" in roles:
            return None
        found = config.peer("origin")
        return found.host if found else None
    except Exception as e:  # noqa: BLE001 — a surface renders what it got
        log.debug("origin lookup failed: %s", e)
        return None


def _ask_origin(argv: list, timeout: float = 20.0) -> Optional[str]:
    """Run one `media` subcommand on the origin. None if it could not be asked.

    No ControlPath is named, deliberately: naming one mints a second master
    beside the ambient one every other ssh on the box keeps warm, and a private
    master for something this bursty is cold exactly when it is used. Inherited,
    this hop is about a second.
    """
    host = _origin_host()
    if not host:
        return None
    try:
        r = subprocess.run(["ssh", *_SSH_OPTS, host, "media", *argv],
                           capture_output=True, text=True, timeout=timeout,
                           check=False)
    except Exception as e:  # noqa: BLE001
        log.debug("origin ask failed (%s): %s", argv, e)
        return None
    if r.returncode != 0:
        log.debug("origin ask rc=%s (%s): %s", r.returncode, argv,
                  (r.stderr or "").strip()[:200])
        return None
    return r.stdout


def _origin_clips(max_age: float = ORIGIN_CLIPS_TTL_S) -> Optional[list]:
    """The origin's clip rows, cached; None when this host is the one to ask.

    `max_age=0` forces a fresh ask — what a tap on the picker deserves, and
    cheap because it happens once per tap.
    """
    if _origin_host() is None:
        return None
    if os.environ.get("MEDIA_SHARE_NO_ORIGIN") == "1":
        return None
    now = time.time()
    if _clips_cache["rows"] is not None and now - _clips_cache["at"] <= max_age:
        return _clips_cache["rows"]
    if _clips_cache["rows"] is None and now - _clips_cache["at"] <= max_age:
        return None                      # a recent failure; do not re-dial yet
    out = _ask_origin(["history", str(_CLIPS_N), "--json"])
    rows = None
    if out:
        try:
            got = json.loads(out)
            rows = got if isinstance(got, list) else None
        except ValueError as e:
            log.debug("origin clips were not JSON: %s", e)
    _clips_cache.update(at=now, rows=rows)
    return rows


def _clips(max_age: float = ORIGIN_CLIPS_TTL_S) -> list:
    """The clip list this surface should show: the origin's, else this host's.

    The fallback is not a formality. A hub that is asleep or off the tailnet
    leaves the phone holding whatever it rendered itself, which is a short and
    old list — but it is a true one, and a picker with something in it beats a
    picker that says the machine is down.
    """
    rows = _origin_clips(max_age)
    return rows if rows is not None else _speech_clips(_CLIPS_N)


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
        # The words come from wherever the conversation is. On the origin that
        # is this store; on a render host it is a hub away, and reading locally
        # is how the phone came to caption today's audio with July's sentence.
        rows = _clips()
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


def chapters(channel: str = "music") -> list:
    """What this channel can be jumped around by, 1-based, current marked.

    Music or book: the loaded track's chapters. It was music only, on the
    strength of a comment here saying a book has no chapters — which was true
    when the book channel was streams and false the moment it grew a cache: an
    m4b has chapters by definition and mpv lifts YouTube's marks too. An MPD or
    GStreamer stream has none, and an empty list is the honest answer there
    rather than an error.

    Speech has no chapters and never will — but it has the same *question*
    answered elsewhere: the clips already spoken, which the popup browses.
    That list is what this returns for speech, so one button means one thing
    on all three channels. See `_speech_clips`.
    """
    channel = (channel or "music").strip() or "music"
    if channel == "speech":
        return _clips(max_age=0.0)      # a tap deserves a fresh list
    if channel not in ("music", "book"):
        return []
    try:
        if channel == "book":
            from ..cli import _book_mpv_chapters

            got = _book_mpv_chapters()
        else:
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


def _speech_clips(n: int = 40) -> list:
    """Recent spoken turns, newest first, shaped like a chapter list.

    `ref` is the row's history id and not its position, because position is
    the one thing that does not hold: the list is newest-first, so a clip
    landing while the picker is open shifts every number by one and the tap
    that follows plays the wrong turn. `replay --id` is addressed the same way
    and for the same reason.

    The live turn is deliberately absent. History is written when a turn ends,
    so what you are hearing right now has no id yet — the popup's traversal
    keeps it (`include_live`) because stepping needs to count it, but a picker
    row you cannot replay is not worth drawing. The mark instead follows the
    clip a replay put on: `_replay_row` stamps its id into now_playing.

    Timestamps ride in the title. `start_ms` is an offset into one track, and
    these are not one track — a clock time is what tells two turns apart, and
    it is what the popup's own clip browser shows.
    """
    try:
        from ..cli import _hist_ts, _hist_txt, _speech_history

        rows = _speech_history(n)
    except Exception as e:  # noqa: BLE001
        log.debug("clip read failed: %s", e)
        return []
    playing = None
    try:
        from ..cli import _now_speaking

        playing = ((_now_speaking() or {}).get("extras") or {}).get("history_id")
    except Exception as e:  # noqa: BLE001 — a marker, never the list's problem
        log.debug("now-playing read failed: %s", e)
    import datetime

    today = datetime.date.today()
    out = []
    for r in rows:
        rid = r.get("id")
        if rid is None:
            continue
        text = _hist_txt(r)[:110] or "(no text)"
        out.append({"number": len(out) + 1,
                    "title": f"{_hist_ts(r, today)}  {text}",
                    # The words on their own, for the card's heading. The list
                    # is already the answer to "what was said last"; a second
                    # reader for that one line is how the two disagreed.
                    "text": text,
                    "start_ms": None,
                    "current": playing is not None and rid == playing,
                    "ref": str(rid)})
    return out


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
    if (channel, action) in ORIGIN_VERBS and runner is None:
        # Where the turn is remembered is where it can be played again: the
        # origin still has the clips, still knows this host is its speech
        # target, and pushes them back here exactly as it does for a live
        # reply. Refused rather than run locally when it cannot be reached —
        # replaying the wrong turn is worse than saying the hub is away.
        if _origin_host() is not None:
            out = _ask_origin([*filled])
            if out is None:
                raise ControlError("the hub is not answering — try again")
            _clips_cache.update(at=0.0, rows=None)   # the marker moved
            return 0
    if runner is None:
        from ..cli import main as runner
    return runner(filled)
