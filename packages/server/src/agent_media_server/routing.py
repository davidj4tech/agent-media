"""Where the assistant button's words go.

Moved out of the canvas's reply.py. `/ask` answers from here: a target the
app names, a conversation named in the words themselves ("reply to drones,
…"), the one in the player, the one last spoken to, or a fresh session.
"""

from __future__ import annotations

import difflib
import os
import re

from . import auth, send, sessions, threads


_NEW = re.compile(r"^\s*(?:(?:start|open)\s+a\s+)?(?:new|fresh)\s+(?:(claude|codex|pi|hermes)\s+)?(?:chat|conversation|session|thread)\b[\s,.:;!-]*(.*)$",
                  re.I | re.S)
# Dictation carries no punctuation, so the name is not delimited: it is
# however many words after the verb best fit a title, and the message is
# whatever follows them.
_VERB = re.compile(r"^\s*(?:please\s+)?(?:reply\s+(?:to|in)|continue(?:\s+(?:with|in))?|switch\s+to|back\s+to|go\s+to|open|in|to|tell)\s+(?:the\s+)?(.+)$",
                   re.I | re.S)
_FILLER = {"chat", "conversation", "session", "thread", "the", "one", "please"}
_TRAILING = {"chat", "conversation", "session", "thread", "one"}   # after the name, still the name
_NAME_WORDS = 8


def _score(name: str, title: str) -> float:
    a, b = name.lower().strip(), title.lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Whole words only, and not a scrap: "me" is inside "message".
    if len(a) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", b):
        return 0.9
    words = [w for w in re.findall(r"[a-z0-9]+", a) if len(w) > 2 and w not in _FILLER]
    if words and all(w in b for w in words):
        return 0.85
    return difflib.SequenceMatcher(None, a, b).ratio()


def resolve_target(text: str, index: list[dict]) -> tuple[str, dict | list | None, str]:
    """What the spoken words say about where they go. `(kind, hit, rest)`.

    `kind` is "new" ("new chat …" — the rest goes to a fresh session; "new
    codex chat …" names the agent, and `hit` is then `{"agent": "codex"}`),
    "session" (`hit` is the index row named; `rest` is the message, and an
    empty one means "just take me there"), "ambiguous" (`hit` is the rows it
    could be, and nothing should be sent), or "" (no target spoken; `rest` is
    the whole text).
    """
    m = _NEW.match(text or "")
    if m:
        return "new", ({"agent": m.group(1).lower()} if m.group(1) else None), m.group(2).strip()
    m = _VERB.match(text or "")
    if not m:
        return "", None, (text or "").strip()
    words = m.group(1).split()
    best: tuple[float, int, dict | None] = (0.0, 0, None)
    close: list[dict] = []
    for n in range(1, min(_NAME_WORDS, len(words)) + 1):
        name = " ".join(words[:n]).rstrip(",.:;!-")
        scored = sorted(((_score(name, row["title"]), row) for row in index), key=lambda r: -r[0])
        if not scored:
            continue
        top = scored[0][0]
        # Words found in the title (0.85 and up) are a match at any length; a
        # mere resemblance only counts once the name is long enough for
        # resemblance to mean something — "me" resembles half the shelf.
        floor = 0.85 if len(name) < 10 else 0.72
        if top < floor:
            continue
        # A longer name that fits as well is the better reading: "reply to
        # digital assistant" over "reply to digital".
        if top > best[0] or (top >= best[0] - 0.01 and n > best[1]):
            best = (top, n, scored[0][1])
            close = [row for sc, row in scored if sc >= floor and top - sc < 0.08]
    if best[2] is None:
        return "", None, (text or "").strip()
    n = best[1]
    while n < len(words) and words[n].lower().strip(",.:;!-") in _TRAILING:
        n += 1            # "… the videography chat, add a gimbal": the chat is the name's
    rest = " ".join(words[n:]).lstrip(",.:;- ").strip()
    if len(close) > 1 and best[0] < 1.0:
        return "ambiguous", close, rest
    return "session", best[2], rest


def _title_of(session: str, index: list[dict] | None = None) -> str:
    for row in index if index is not None else sessions.sessions_index():
        if row["session"] == session:
            return row["title"]
    return ""


def ask_routed(text: str, bearer: str, *, target: str = "", player_item: str = "",
               sticky: str = "", parse: bool = True, dry: bool = False,
               project: str = "", agent: str = "", cwd: str = "",
               player_session: str = "", model: str = "",
               plan: bool = False) -> tuple[bool, dict]:
    """The assistant button's words, sent where they belong.

    In order: a target the app names outright (`target`, a session uuid from
    its picker — "new" forces a fresh one); a target spoken at the start of
    the words ("reply to drones, …", "new chat, …"); the conversation loaded
    in the player (`player_session`, or `player_item`, an ABS item id); the
    session the button
    last spoke to (`sticky`); else a fresh session. A spoken name that fits
    more than one conversation is not sent anywhere — the candidates go back
    for the app to ask. `dry` answers where the words WOULD go and sends
    nothing: the app confirms a guess (a spoken name, the player, the last
    thread) with the listener before committing with an explicit `target`.
    A fresh session opens in `project` when one is named, and runs `agent`
    (claude, codex, pi, hermes) when one is named or spoken (see `ask`),
    on `model` and in plan mode when `plan` — a fresh session's only; words
    that land in an existing one leave its settings alone.
    """
    text = " ".join((text or "").split())
    if not text:
        return False, {"error": "empty message"}
    player_session = (player_session or "").strip()
    if player_session and not sessions._SESSION.fullmatch(player_session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err

    index = sessions.sessions_index()
    session, how = "", ""
    if target == "new":
        how = "asked"
    elif target:
        session, how = target, "picked"
    elif parse:
        kind, hit, rest = resolve_target(text, index)
        if kind == "new":
            text, how = rest or text, "spoken"
            agent = (hit or {}).get("agent") or agent
        elif kind == "session":
            session, text, how = hit["session"], rest, "spoken"
            if not text:
                # A name and nothing else: switch to it, send nothing.
                item, ready = threads.item_for_session(session, bearer)
                return True, {"mode": "switched", "how": how, "session": session,
                              "title": hit["title"], "pane": hit.get("pane"),
                              "item": item if ready else None, "text": ""}
        elif kind == "ambiguous":
            return False, {"error": "which conversation?", "status": 300,
                           "ambiguous": hit, "text": rest}
    if not session and how != "spoken" and how != "asked":
        # The thread open in the player. `player_session` is its own id (v1,
        # server-contract.md §10) and wins over `player_item`, which has to
        # be asked of ABS; either way it only counts if the session is real.
        if player_session and (sessions.live_sessions().get(player_session)
                               or sessions.session_exists(player_session)):
            session, how = player_session, "player"
        if not session and player_item:
            sid, _why = sessions.session_for_item(player_item, bearer)
            if sid:
                session, how = sid, "player"
        if not session and sticky and sessions._SESSION.fullmatch(sticky) and sessions.session_exists(sticky):
            session, how = sticky, "sticky"

    if dry:
        item, ready = threads.item_for_session(session, bearer) if session else (None, False)
        return True, {"mode": "continued" if session else "new", "how": how or "default",
                      **({} if session else {"agent": agent or os.environ.get("MEDIA_ASK_AGENT") or "claude"}),
                      "session": session or None, "title": _title_of(session, index) if session else "",
                      "item": item if ready else None, "text": text, "dry": True}
    if not session:
        ok, detail = send.ask(text, bearer, project=project, agent=agent, cwd=cwd,
                              model=model, mode="plan" if plan else "")
        if ok:
            detail.update({"mode": "new", "how": how or "default", "title": "", "text": text})
        return ok, detail
    ok, detail = send.deliver(session, text, text)
    if not ok:
        return False, detail
    item, ready = threads.item_for_session(session, bearer)
    detail.update({"mode": "continued", "how": how, "title": _title_of(session, index),
                   "item": item if ready else None, "text": text})
    return True, detail
