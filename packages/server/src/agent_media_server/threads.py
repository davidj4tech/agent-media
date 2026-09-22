"""One conversation: whether it can be replied to, and what was said in it.

Moved out of the canvas's reply.py. `/conversation` (by item or by session),
`/conversation/log`, `/commands` and `/rename` answer from here.

The log's pictures are the canvas's: it remembers what it drew for each reply
in its own spool, which this package does not know about. So the canvas
registers a `pictures_for` callback at startup (`set_pictures_for`) and the
log asks it; without one, the lines simply carry no pictures.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from . import auth, auth_abs, driver, recaps, send, sessions, transcript

log = logging.getLogger("agent-media.server.threads")


def item_for_session(session: str, bearer: str) -> tuple[str | None, bool]:
    """`(item id, ready)` for `session` on the caller's own Audiobookshelf.

    The caller's server and the caller's bearer, on purpose: this host
    publishes to more than one ABS and each gives the same folder a different
    id, so an id from "our" server is a 404 on the phone signed in to the
    other. `ready` is whether ABS has built the item's tracks yet — the app's
    item page cannot open one it has only just created (the first attempt
    sent the phone to a trackless item and it bounced home with "Failed to
    get library item"), so the caller should wait for both.
    """
    folder = sessions._folder_for_session(session)
    if not folder:
        return None, False
    # A device token is never shown to ABS; a device asks under the host's
    # own ABS login (see auth.abs_bearer).
    bearer = auth.abs_bearer(bearer)
    if not bearer:
        return None, False
    url = auth_abs.abs_home(bearer)
    if not url:
        return None, False
    tail = sessions._tail(folder)
    libs, _status = auth_abs._abs_get(url, bearer, "/api/libraries")
    for lib in (libs or {}).get("libraries") or []:
        if lib.get("mediaType") != "book":
            continue
        page, _status = auth_abs._abs_get(
            url, bearer, f"/api/libraries/{lib.get('id')}/items?limit=1000&sort=addedAt&desc=1")
        for item in (page or {}).get("results") or []:
            if sessions._tail(item.get("path") or "") == tail and item.get("id"):
                tracks = int(((item.get("media") or {}).get("numTracks")) or 0)
                return str(item["id"]), tracks > 0
    return None, False


def conversation_for_session(session: str, bearer: str) -> tuple[bool, dict]:
    """`/conversation?session=`: where a session started from the phone got to.

    Same gates as `conversation`. The answer is `ok` from the first poll —
    the session is real — and `item` fills in when the library has it.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    pane = sessions.live_sessions().get(session, "")
    live, resumable = bool(pane), sessions.session_exists(session)
    if not pane:
        hl = driver.headless_state(session)
        if hl is not None:
            # A headless session: live while sessiond runs it, and resumable
            # whenever it has a record (a reply resumes it).
            live, resumable = hl["live"], True
    item, ready = item_for_session(session, bearer)
    return True, {"session": session, "item": item if ready else None,
                  "scanning": bool(item) and not ready,
                  "live": live, "pane": pane or None,
                  "resumable": resumable,
                  # The same ghost prompt `?item=` offers (§6.2.1), so a
                  # client that only knows sessions draws the same box.
                  "suggestion": sessions.suggestion_for(session, pane)}


def conversation(item: str, bearer: str) -> tuple[bool, dict]:
    """Whether `item` is a conversation this caller may reply to.

    The app asks this before drawing the reply box, so it never has to know
    what a conversation is or which library holds them — it shows the box when
    the answer here is yes. Same two gates as `reply`, in the same order, so
    the box cannot appear where the send would be refused.
    """
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    session, err = sessions.session_for_item(item, bearer)
    if not session:
        return False, {"error": err, "status": 404}
    pane = sessions.live_sessions().get(session, "")
    hl = None if pane else driver.headless_state(session)
    return True, {"session": session,
                  "live": bool(pane) or bool(hl and hl["live"]), "pane": pane or None,
                  "resumable": sessions.session_exists(session) or hl is not None,
                  "suggestion": sessions.suggestion_for(session, pane)}


def commands_for(item: str, session: str, project: str, bearer: str,
                 cwd: str = "") -> tuple[bool, dict]:
    """The slash menu for a conversation, or for a project about to start one.

    The menu belongs to a directory, not to a conversation: a project's own
    skills and commands are what make the list worth having, and two sessions
    in the same tree get the same answer. Gated like `/conversation` — the
    menu names this machine's skills, so a caller who may not reply may not
    read it either.
    """
    from agent_media_core import slash_menu

    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if item and not session:
        session, err = sessions.session_for_item(item, bearer)
        if not session:
            return False, {"error": err, "status": 404}
    where = cwd
    cwd = sessions.transcript_cwd(session) if session else ""
    if not cwd and project:
        _name, cwd = sessions.project_target(project)
    # A place from `/targets` names its directory outright — trusted only as
    # far as the list that published it, same as `/ask`.
    if not cwd and where and where in {p["path"] for p in sessions.places(limit=0)}:
        cwd = where
    cwd = cwd or os.path.expanduser("~")
    return True, {"cwd": cwd, "commands": slash_menu.menu(cwd)}


#: What the auto-rename asks for. A resume-list name, not a summary.
AUTO_TITLE_PROMPT = (
    "You name a conversation between a person and their coding assistant, the "
    "way it would be listed in a history of chats. Read it and reply with a "
    "short title for what it is about: two to six words, sentence case, "
    "specific (\"Speech bar on the lock screen\", not \"Fixing a bug\"). No "
    "quotes, no preamble, no trailing full stop. Reply with the title only."
)


def _conversation_text(session: str, budget: int = 8000) -> str:
    """The thread as plain text for naming it: how it opened (what it is
    about) and how it stands now (what it became), inside `budget` chars."""
    ok, detail = session_log(session, limit=MESSAGES_MAX)
    if not ok:
        return ""
    turns = []
    for m in detail.get("messages") or []:
        words = " ".join(str(p.get("text") or "") for p in m.get("parts") or []
                         if p.get("type") == "text").strip()
        if words:
            who = "Person" if m.get("role") == "user" else "Assistant"
            turns.append(f"{who}: {' '.join(words.split())[:1200]}")
    text = "\n\n".join(turns)
    if len(text) <= budget:
        return text
    half = budget // 2
    return text[:half] + "\n\n[…]\n\n" + text[-half:]


def auto_title(session: str) -> str:
    """A name for the conversation from the summary gateway, or "" when it
    cannot be had. The follow-up's model (a small hosted one): the summary's
    local model takes half a minute on red5 for a line."""
    from agent_media_core.intake._summary import DEFAULT_TIMEOUT, _chat, _int_env

    text = _conversation_text(session)
    if not text:
        return ""
    model = (os.environ.get("MEDIA_TITLE_MODEL")
             or os.environ.get("MEDIA_FOLLOWUP_MODEL") or None)
    timeout = _int_env("MEDIA_TITLE_TIMEOUT", _int_env("MEDIA_SUMMARY_TIMEOUT", DEFAULT_TIMEOUT))
    out = (_chat(AUTO_TITLE_PROMPT, text, timeout, model=model) or "").strip()
    line = out.splitlines()[0] if out else ""
    if line.lower().startswith("title:"):
        line = line[len("title:"):]
    line = " ".join(line.split()).strip("\"'`*#").rstrip(".").strip()
    return line if 0 < len(line) <= 80 else ""


#: A thread nobody has named is named for them once its first turn is done
#: (sessiond, §17). "0" switches that off; the name itself is `auto_title`.
def auto_title_enabled() -> bool:
    return (os.environ.get("MEDIA_AUTO_TITLE", "1") or "1").strip() != "0"


def is_named(session: str) -> bool:
    """Whether this conversation has a name somebody chose or Claude chose.

    The shelf's `title` (a `/rename`, kept in the manifest) or Claude Code's
    own name for the session — which `/rename` replaces, so it covers a
    rename made at the terminal too. Deliberately not the folder, which is
    the question the thread opened with rather than a name for it.
    """
    from agent_media_core import conversation

    data = _manifest_for(session) or {}
    if " ".join(str(data.get("title") or "").split()):
        return True
    return bool(conversation.session_name(session))


def name_unnamed(session: str) -> str:
    """Name a thread that has no name, and file the name. "" if it keeps none.

    Everything `rename_conversation` does but telling the running agent,
    which is the caller's to do (sessiond types `/rename` into it directly).
    Silent about every failure: an unnamed thread is still a thread, and this
    runs behind a turn nobody is waiting on.
    """
    if not session or not auto_title_enabled() or is_named(session):
        return ""
    title = auto_title(session)
    if not title:
        return ""
    from agent_media_core import book_tracks

    return book_tracks.rename(session, title)


def rename_conversation(item: str, session: str, title: str, bearer: str,
                        auto: bool = False) -> tuple[bool, dict]:
    """Rename a conversation from the app. Gated like `/reply`.

    The name is kept by agent-media and given to Claude Code as well, so the
    terminal and the shelf call it the same thing. `auto` with no title asks
    the gateway to name it from what was said (`auto_title`).
    """
    from agent_media_core import book_tracks

    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if item and not session:
        session, err = sessions.session_for_item(item, bearer)
        if not session:
            return False, {"error": err, "status": 404}
    title = " ".join((title or "").split())
    if not title and auto:
        title = auto_title(session)
        if not title:
            return False, {"error": "could not think of a name", "status": 502}
    if not title:
        return False, {"error": "no title", "status": 400}
    named = book_tracks.rename(session, title)
    if not named:
        return False, {"error": "could not rename", "status": 500}
    # A running Claude Code never re-reads its name file, so it is told the
    # way a person would: `/rename` typed into its pane. Not being able to
    # (ended, or mid-sentence in the box) is not a failed rename — the shelf
    # and the name file have it, and the next session starts with it.
    why = send.send_rename(session, named)
    return True, {"session": session, "title": named,
                  "terminal": not why, "why": why or None}


#: `pictures_for(key) -> (images, figure)`: what the canvas drew for the reply
#: with that dedup key. Registered by the canvas (`set_pictures_for`); None
#: until it is, and then no line carries pictures.
_PICTURES_FOR: Callable[[str], tuple[list, bool]] | None = None


def set_pictures_for(fn: Callable[[str], tuple[list, bool]] | None) -> None:
    """Hand in the canvas's picture lookup (see `attach_pictures`)."""
    global _PICTURES_FOR
    _PICTURES_FOR = fn


def attach_pictures(lines: list) -> None:
    """Give each log line the picture(s) the canvas drew for that reply.

    The visual channel remembers what it pushed for a reply under the reply's
    dedup key (state.save_push), and the speech row carries the same key, so
    the join is a lookup — the canvas's lookup, `pictures_for`, since the
    spool is its. Each line gains `images`: canvas-relative or absolute URLs
    the app can put straight into an <img>, and `figure`: whether the picture
    was drawn to be read (a [[visual:]] figure) rather than ambient artwork.
    """
    pictures_for = _PICTURES_FOR
    if pictures_for is None:
        return
    for line in lines:
        key = line.get("key")
        if not key:
            continue
        images, figure = pictures_for(key)
        if images:
            line["images"] = images
            line["figure"] = figure


def _manifest_for(session: str) -> dict | None:
    """The book-tracks manifest for `session`, or None when it has none yet.

    A session gets a manifest on its first publish — its first spoken reply,
    debounced — so a session the phone started a moment ago has none. That is
    not the same as "no such conversation", which is why this says None rather
    than an empty dict.
    """
    for f in sorted(sessions._manifest_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if str(data.get("session") or f.stem) == session:
            return data
    return None


#: Messages a log carries unless asked for more: the newest this many. A long
#: thread is hundreds of messages and ~400 KB of them (tool summaries are
#: most of it); the phone shows the end first and asks for the rest with
#: `?before=<the first message's id>`.
MESSAGES_LIMIT = 60
#: The most a caller may ask for at once.
MESSAGES_MAX = 500


def messages_for(session: str, lines: list, *, working: bool, live: bool,
                 limit: int = MESSAGES_LIMIT, before: str = "", around: str = "",
                 jump: dict | None = None) -> tuple[list, bool]:
    """`(messages, older)`: the thread as its transcript has it, with speech
    joined on (transcript.py). `older` is whether messages exist before the
    first one returned.

    Claude Code sessions are read from their transcript. Every other harness
    — or a Claude session whose transcript cannot be found — gets messages
    made from its spoken lines, text only, until it has a parser of its own.
    """
    got = None
    if around:
        # A jump from search (§6.14): the page holding that message, from a
        # few before it on. `jump` says whether it was found and whether the
        # thread goes on past the page; not found, the newest page as usual.
        win = transcript.messages_around(session, around, limit=limit, most=MESSAGES_MAX)
        if win is not None:
            got = win[0], win[1]
            if jump is not None:
                jump.update(found=True, newer=win[2])
        elif transcript.transcript_path(session):
            got = transcript.messages(session, limit=limit)
    if got is None:
        got = transcript.messages(session, limit=limit, before=before)
    if got is None:
        msgs = transcript.messages_from_lines(lines, working=working)
        if around:
            win = transcript.window_around(msgs, around, limit, MESSAGES_MAX)
            if win is not None:
                msgs, older, newer = win
                if jump is not None:
                    jump.update(found=True, newer=newer)
                transcript.strip_markers(msgs)
                return msgs, older
        if before:
            idx = next((i for i, m in enumerate(msgs) if m["id"] == before), None)
            msgs = msgs[:idx] if idx is not None else []
        older = bool(limit) and len(msgs) > limit
        msgs = msgs[-limit:] if older else msgs
        transcript.strip_markers(msgs)
        return msgs, older
    msgs, older = got
    transcript.join_speech(msgs, lines)
    # The canvas's markers are not for reading; the join above needed them.
    transcript.strip_markers(msgs)
    if not live:
        # A turn in a session nobody is running is not running, whatever its
        # last record says (it was killed mid-turn).
        for m in msgs:
            m["turn"]["running"] = False
    return msgs, older


def _envelope(session: str, lines: list, *, limit: int = MESSAGES_LIMIT,
              before: str = "", around: str = "") -> dict:
    """The `/conversation/log` answer around `lines`: the messages, pending,
    working, approval and the suggestion, the same whichever way the thread
    was named."""
    # `pending` is true while the last thing said was the listener's: a reply
    # is in, no answer has landed yet. The app shows a "thinking" line and
    # polls faster until it clears, rather than waiting out a whole idle poll
    # with nothing on screen.
    pending = bool(lines) and lines[-1].get("who") == "you"
    attach_pictures(lines)
    # What the session did for each reply ("Worked for 3m · 14 steps"), and
    # what it is doing now, in place of the dots. A turn typed at the desk
    # counts too: the phone shows it working even before that message reaches
    # the transcript.
    from agent_media_core import activity as _activity
    working = _activity.attach(session, lines)
    pending = pending or bool(working)
    # The ghost prompt rides along with every poll: it appears a few seconds
    # after the turn it follows, so a one-off read at page-open would mostly
    # find it not there yet.
    pane = sessions.live_sessions().get(session, "")
    # A headless session (MEDIA_HEADLESS) has no pane: whether it is live and
    # what it is stopped on come from sessiond instead of a screen.
    hl = None if pane else driver.headless_state(session)
    live = bool(pane) or bool(hl and hl["live"])
    last = lines[-1] if lines else {}
    # The thread as the terminal has it (transcript.py). A prompt is in the
    # transcript the moment it is typed, well before any speech of it, so a
    # live session whose last message is the listener's is pending too.
    jump = {"found": False, "newer": False}
    messages, older = messages_for(session, lines, working=bool(working), live=live,
                                   limit=limit, before=before, around=around, jump=jump)
    if live and not before and messages and messages[-1]["role"] == "user":
        pending = True
    if hl is not None and hl["live"] and hl["state"] == "working":
        pending = True
    suggestion = ("" if pending else
                  sessions.suggestion_for(session, pane, last.get("key") or ""))
    # A session waiting on a permission dialog is not working and not
    # finished: it is stopped until somebody answers, and the phone is often
    # the only place anybody is looking.
    if hl is not None:
        approval = hl["approval"]
    else:
        approval = sessions.approval_for(pane, sessions._agent_of_pane(pane), session) if pane else None
    # Claude Code's own "while you were away" summary, for the card at the top
    # of the thread. Deliberately not a line: nobody said it, and it is not
    # part of the conversation the agent sees. The latest only, not every one
    # since the first line — the app shows one card, and a history of them is
    # `recaps.recaps()` when something wants it. Falls back to the recap the
    # idle reaper wrote before resting the session, when that is newer.
    recap = recaps.recap_for(session)
    out = {"session": session, "lines": lines, "messages": messages, "older": older,
           "pending": pending, "working": working, "approval": approval,
           "suggestion": suggestion, "recap": recap}
    if around:
        out["around"] = {"id": around, **jump}
        out["newer"] = jump["newer"]
    return out


def age_live(detail: dict) -> None:
    """Bring the live reply's clock up to now, in place — on its line and on
    its message. The position was read early in building the answer; the
    phone is 2 s away over the tailnet, and every stale moment here was a
    moment the follow-along bold spent behind the voice."""
    import time

    now = time.time()
    lives = [l for l in detail.get("lines") or [] if l.get("live")]
    lives += [(m.get("spoken") or {}).get("live") for m in detail.get("messages") or []
              if (m.get("spoken") or {}).get("live")]
    for live in lives:
        if live.get("elapsed") is not None and not live.get("paused") \
                and live.get("server_time"):
            live["elapsed"] = round(live["elapsed"] + now - live["server_time"], 3)
            live["server_time"] = round(now, 3)


def _limit(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return MESSAGES_LIMIT
    return max(1, min(MESSAGES_MAX, n))


def log_for_item(item: str, bearer: str, *, limit=None, before: str = "") -> tuple[bool, dict]:
    """The conversation behind `item`, as readable lines. Same gates as a reply.

    Gated identically on purpose: the log is the words of the conversation, so
    anyone who can read it could have read them by listening — but an account
    that may not reply has no business being handed a transcript either.
    """
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    session, err = sessions.session_for_item(item, bearer)
    if not session:
        return False, {"error": err, "status": 404}
    try:
        from agent_media_core import book_tracks

        data = _manifest_for(session)
        if data is not None:
            lines = book_tracks.conversation_log(
                session, Path(str(data.get("folder") or "")),
                target="conversations")
            return True, _envelope(session, lines, limit=_limit(limit), before=before)
    except Exception as e:  # noqa: BLE001
        # Say so in the journal as well as to the caller: the app folds every
        # failed fetch into an empty transcript, so this line is the only
        # record on this side of what actually went wrong.
        log.exception("conversation log for %s (session %s) failed: %s",
                      item, session, e)
        return False, {"error": f"could not read the conversation ({e})",
                       "status": 500}
    return False, {"error": "no manifest for that conversation", "status": 404}


def log_for_session(session: str, bearer: str, *, limit=None,
                    before: str = "", around: str = "") -> tuple[bool, dict]:
    """`/conversation/log?session=`: the same lines, named by the thread's own
    id (server-contract.md §10). Same envelope, same line shapes, same gate.

    Two differences from the item form, both because nothing here asks ABS:

    * **No positions.** `start`/`end` are always null: placing a line in the
      audio item means reading the item's tracks from ABS, and this form is
      the one that has to keep working when ABS is slow, down or gone. They
      are ABS-shaped fields that go at the ABS exit anyway.
    * **No manifest is not a 404 by itself.** A session gets its manifest on
      its first publish, so one the phone started seconds ago has none — but
      speech history already has the listener's turn, and the live tail and
      the live line come from history and the player, not the manifest.
      `conversation_log` reads the manifest by session and needs a folder only
      for positions, so it is asked regardless. Only a session with no
      manifest, nothing said, no pane and no transcript is 404 "no
      conversation for that session yet"; a real session with nothing said
      yet answers 200 with no lines, so a client polling a thread it just
      opened does not see an error.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return session_log(session, limit=limit, before=before, around=around)


def session_log(session: str, *, limit=None, before: str = "",
                around: str = "") -> tuple[bool, dict]:
    """The session form's answer without its gate: what `/conversation/log
    ?session=` answers once the caller is let in, and what the per-thread
    stream sends as its snapshot (thread_events.py). `session` must already
    be a valid id."""
    try:
        from agent_media_core import book_tracks

        data = _manifest_for(session)
        folder = Path(str((data or {}).get("folder") or ""))
        lines = book_tracks.conversation_log(session, folder, target="conversations",
                                             positions=False)
        if data is None and not lines \
                and not sessions.live_sessions().get(session) \
                and not sessions.session_exists(session) \
                and not driver.owned_headless(session):
            return False, {"error": "no conversation for that session yet", "status": 404}
        return True, _envelope(session, lines, limit=_limit(limit), before=before,
                               around=around)
    except Exception as e:  # noqa: BLE001
        log.exception("conversation log for session %s failed: %s", session, e)
        return False, {"error": f"could not read the conversation ({e})",
                       "status": 500}
