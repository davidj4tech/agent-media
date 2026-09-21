"""The app's route table: every route in docs/server-contract.md §6.

Moved out of the canvas's Handler. The canvas still serves these on its own
port, in its own process — its handler answers its own routes (the page,
`/events`, `/show`, `/img`, …) first and falls through to `dispatch` here —
because clients know one address and changing it is a migration nobody needs.
`Handler` below serves only these routes, for a server with no canvas in it.

Endpoints (all gated by `auth.gate`: a paired device's token, else the
caller's Audiobookshelf bearer — see auth.py; except /pair, which is how a
device gets its token):

  POST /pair      {"code", "device"} → trade a one-time pairing code (minted
                  by `media-visual-canvas pair --device NAME`) for a device
                  token. No auth; failures rate-limited per source address.
                  (GET /pair is the canvas's own amux page, not this.)
  GET  /conversation?item=<abs item id>   → the session behind that item and
                  whether it is still live
  GET  /conversation?session=<uuid>   → a session the phone started: its item
                  id once the library has one, and whether it is live
  GET  /conversation/log?item=<abs item id>|session=<uuid>   → the
                  conversation, read
  GET  /item?id=<abs item id>   → that library item carrying only what the
                  app reads, gzipped (1267 KB → 25 KB on a long
                  conversation); see abs_item.py
  GET  /commands?item=|session=|project=|cwd=   → the slash menu
  POST /reply     {"session"|"item", "text": "...", "quote": "...",
                   "mode": "continue"|"branch"} → type into that session (or
                  the one behind that item), reviving it in a background tmux
                  window if it has ended
  POST /ask       {"text", "target"?, "player_session"?|"player_item"?, "sticky"?,
                   "parse"?, "project"?, "agent"?}
                  → the assistant button's words, routed: a picked session, a
                  session named in the words ("reply to drones, …"), the
                  player's conversation, the one last spoken to, else a FRESH
                  session in the scratch tmux session — or in `project` (a
                  series name) or `cwd` (a directory from /targets), 404 if
                  neither is known. 300 + candidates when a spoken name is
                  ambiguous.
  GET  /conversations   → live sessions and recent conversations, by title
                  (the picker)
  GET  /targets   → what a message can be pointed at: those same sessions,
                  plus `places` — the directories sessions have run in, newest
                  first, which a fresh chat can be opened in (`{"cwd": …}` to
                  /ask)
  GET  /harnesses → the four harnesses: installed, which version, signed in
                  or not, and which of install and login this host has a
                  recipe for
  POST /harnesses/run {"agent", "action": "install"|"login"} → run it in a
                  background tmux window; answers with the pane
  GET  /harnesses/screen?pane=%23 → that window's screen, and whether the
                  command has finished
  POST /harnesses/keys {"pane", "text"?, "key"?} → type into it (an OAuth
                  code pasted back, a y, an Enter)
  POST /harnesses/close {"pane"} → end that window
  GET  /sessions/state  → every live session's working / waiting / approval,
                  by uuid and item folder tail
  POST /session/resume {"session"} → bring that session back in a tmux
                  window (a reply's revive, without the reply)
  POST /session/close  {"session"} → close the pane it runs in
  POST /session/archive {"session", "archived": true|false} → file the
                  thread under Archived, or take it back out (a flag this
                  server keeps; see archive.py). Ends nothing
  POST /session/answer {"session", "choice", "key"} → answer the dialog that
                  session is stopped on (a permission prompt); refused unless
                  that same question, fingerprinted by `key`, is still on its
                  screen
  GET  /draft?session=<uuid>   → what was left half-typed in that
                  conversation's reply box
  POST /draft     {"session", "text", "at"?} → hold it (empty text drops it)
  POST /rename    {"item"|"session", "title"} → rename a conversation
  POST /share     {"text", "channel"?} → play a shared link
  GET  /speech/now   → what is being said, named for the speech bar
  POST /speech/ctl   {"action", "arg"?} → a listener's speech verb
  POST /focus     {"pane": "%23"} → bring the attached tmux client to a pane
                  (the canvas's own token also admits this one)
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler
from typing import Callable
from urllib.parse import parse_qs

from . import (abs_item, archive, auth, devices, drafts, harnesses, routing, send, sessions,
               share, speech, threads)

# The endpoints a browser on another origin may reach. Everything here
# carries its own credential — a paired device's token, or the caller's
# Audiobookshelf bearer handed back to ABS to ask who they are — and none of
# it is reachable with the ambient authority a browser attaches by itself, so
# opening them to any origin gives a drive-by page nothing it did not already
# have. The canvas's token-guarded
# routes (/input, /show, /ctl, /say, /play) are deliberately NOT here: their
# credential is the host's, not the caller's, and CORS is what keeps a page
# you happen to be visiting from spending it.
#
# Needed because the web client is served from a different port than the
# canvas (Audiobookshelf on :13379, this on :8781). The Capacitor app never
# needed it — a native HTTP client is not subject to the same-origin policy.
CORS_PATHS = frozenset({
    "/conversation", "/conversation/log", "/conversations", "/targets", "/item",
    "/reply", "/ask", "/focus", "/session/resume", "/session/close", "/draft",
    "/session/answer", "/session/archive",
    "/speech/now", "/speech/ctl", "/sessions/state", "/commands", "/rename",
    "/harnesses", "/harnesses/run", "/harnesses/screen",
    "/harnesses/keys", "/harnesses/close", "/share",
})

# Paths opened to other origins for POST (and its preflight) ONLY. `/pair` is
# the one: `POST /pair` is how the chat bundle, served from another origin,
# trades a pairing code for a device token, and it needs no credential. But
# `GET /pair` on the same path is the canvas's page that hands a browser the
# host's amux token for a (different) one-time code. An
# Access-Control-Allow-Origin on that answer would let a script on any page
# read the amux token out of it, so the GET must stay same-origin — which is
# why this is a separate set, keyed on the method, and NOT in `CORS_PATHS`
# (the canvas's own `_cors` reads that set for every answer it sends).
CORS_POST_PATHS = frozenset({"/pair"})

# Long enough that a chat page's polling is not preceded by a preflight every
# time; short enough that a change here is picked up the same day.
CORS_MAX_AGE = "3600"

# Cap request bodies: an unbounded Content-Length (e.g. 5 GB) would force a
# multi-GB read/alloc — a trivial remote OOM on a RAM-tight host (#139).
MAX_BODY = 64 * 1024

_SPEECH_NOW_SEEN: set[str] = set()

#: Whether a request carries the host's own token (the canvas's amux token).
#: Only /focus looks at it — the desk's browser may focus a pane with either
#: credential. Handed in by the canvas (`register`); without one, only the
#: ABS bearer admits.
_TOKEN_OK: Callable[[BaseHTTPRequestHandler], bool] | None = None


def register(*, speech_state: Callable[[], dict] | None = None,
             speech_ctl: Callable[[str, int], str] | None = None,
             pictures_for: Callable[[str], tuple[list, bool]] | None = None,
             token_ok: Callable[[BaseHTTPRequestHandler], bool] | None = None) -> None:
    """What the server needs from the process serving it, handed in rather
    than imported: the canvas's speech snapshot and transport, its picture
    spool, and its own token check."""
    global _TOKEN_OK
    speech.set_speech(speech_state, speech_ctl)
    threads.set_pictures_for(pictures_for)
    _TOKEN_OK = token_ok


# --- the envelope -----------------------------------------------------------------

def _cors(h: BaseHTTPRequestHandler) -> None:
    """Allow a browser on another origin, on the bearer-authed routes only.

    `*` rather than the caller's origin, and no Allow-Credentials: the
    credential is the Authorization header the client sets by hand, so the
    browser never attaches anything of its own to these.
    """
    path = h.path.split("?", 1)[0]
    if path not in CORS_PATHS and not (path in CORS_POST_PATHS
                                       and h.command in ("POST", "OPTIONS")):
        return
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Expose-Headers", "Content-Encoding")


def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, ctype: str) -> None:
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    _cors(h)
    h.end_headers()
    h.wfile.write(body)


def _json(h: BaseHTTPRequestHandler, code: int, obj: dict) -> None:
    _send(h, code, json.dumps(obj).encode(), "application/json")


def _json_z(h: BaseHTTPRequestHandler, code: int, obj: dict) -> None:
    """JSON, compressed if the caller said it could take it.

    Audiobookshelf itself does not compress — it ignores Accept-Encoding
    and sends its item JSON whole, which on a conversation is 1.27 MB of
    highly repetitive text. Ours is already a tenth of that; gzip takes it
    to a fortieth. Only worth the CPU on a body big enough to matter.
    """
    body = json.dumps(obj).encode()
    accepts = "gzip" in (h.headers.get("Accept-Encoding") or "").lower()
    if accepts and len(body) > 4096:
        import gzip as _gzip

        packed = _gzip.compress(body, 6)
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Encoding", "gzip")
        h.send_header("Content-Length", str(len(packed)))
        h.send_header("Cache-Control", "no-store")
        _cors(h)
        h.end_headers()
        h.wfile.write(packed)
        return
    _send(h, code, body, "application/json")


def _read_json(h: BaseHTTPRequestHandler) -> dict | None:
    try:
        # Never read past the cap even if a caller reached here without the
        # POST guard (defence in depth for the #139 OOM).
        n = min(int(h.headers.get("Content-Length", "0")), MAX_BODY)
        return json.loads(h.rfile.read(n) or b"{}")
    except (ValueError, json.JSONDecodeError):
        return None


def _bearer(h: BaseHTTPRequestHandler) -> str:
    return (h.headers.get("Authorization") or "").removeprefix("Bearer").strip()


# --- the routes -------------------------------------------------------------------

def dispatch(h: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    """Answer `method path` if it is one of the app's routes. False if not —
    the caller's 404 (or its own route) is then the answer.

    `path` is the path without its query; the query is read off `h.path`.
    """
    # Who is on the other end, for a paired device's `last_ip` (auth.py
    # reads it when the bearer turns out to be a device token).
    auth.set_client_ip(h.client_address[0] if h.client_address else "")
    if method == "GET":
        return _get(h, path)
    if method == "POST":
        return _post(h, path)
    if method == "OPTIONS":
        return _options(h, path)
    return False


def _options(h: BaseHTTPRequestHandler, path: str) -> bool:
    """CORS preflight. Anything not on the list is not ours to allow."""
    if path not in CORS_PATHS and path not in CORS_POST_PATHS:
        return False
    h.send_response(204)
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Allow-Methods",
                  "POST, OPTIONS" if path in CORS_POST_PATHS else "GET, POST, OPTIONS")
    h.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    h.send_header("Access-Control-Max-Age", CORS_MAX_AGE)
    h.send_header("Content-Length", "0")
    h.end_headers()
    return True


def _get(h: BaseHTTPRequestHandler, path: str) -> bool:
    query = h.path.partition("?")[2]
    if path == "/item":
        # The library item, carrying only what the app reads. Sasonica asks
        # here first and falls back to Audiobookshelf, so this is a way of
        # being quick rather than a thing to depend on. See abs_item.py for
        # the measurements and for what is left out.
        item_id = parse_qs(query).get("id", [""])[0]
        ok, detail = abs_item.item_for_app(item_id, _bearer(h))
        if ok:
            _json_z(h, 200, detail)
        else:
            _json(h, detail.pop("status", 404), {"ok": False, **detail})
    elif path == "/conversation":
        # "Is this item a conversation I can reply to?" — what the app asks
        # before it draws the reply box. Authed by the caller's own ABS
        # bearer, like /reply. `?session=` instead of `?item=` asks the
        # other way round: a session the phone just started (see /ask),
        # and whether the library has an item for it yet.
        qs = parse_qs(query)
        item = qs.get("item", [""])[0]
        bearer = _bearer(h)
        if qs.get("session", [""])[0] and not item:
            ok, detail = threads.conversation_for_session(qs["session"][0], bearer)
        else:
            ok, detail = threads.conversation(item, bearer)
        _json(h, 200 if ok else detail.pop("status", 404), {"ok": ok, **detail})
    elif path == "/commands":
        # The slash menu for the reply box: what this session's terminal
        # would offer. `?item=`, `?session=`, `?project=` or `?cwd=` (a
        # new chat, in a place `/targets` published).
        qs = parse_qs(query)
        ok, detail = threads.commands_for(qs.get("item", [""])[0],
                                          qs.get("session", [""])[0],
                                          qs.get("project", [""])[0], _bearer(h),
                                          cwd=qs.get("cwd", [""])[0])
        # One line per ask: this is a new route and the app is the only
        # caller, so "did the box even ask?" is the first question every
        # time it does not appear.
        print(f"commands: {h.client_address[0]} {query} -> "
              f"{len(detail.get('commands') or []) if ok else detail}", file=sys.stderr)
        _json(h, 200 if ok else detail.pop("status", 404), {"ok": ok, **detail})
    elif path == "/conversation/log":
        # The same conversation, read rather than heard. `?session=` is the
        # v1 form (server-contract.md §10) and wins when both are given; it
        # never asks ABS, so it answers when ABS does not.
        qs = parse_qs(query)
        session = qs.get("session", [""])[0]
        if session:
            ok, detail = threads.log_for_session(session, _bearer(h))
        else:
            ok, detail = threads.log_for_item(qs.get("item", [""])[0], _bearer(h))
        if ok:
            # The live reply's position was read early in building this
            # answer; bring it up to the moment it is sent. The phone is
            # 2s away over the tailnet, and every stale moment here was a
            # moment the follow-along bold spent behind the voice.
            now = time.time()
            for line in detail.get("lines") or []:
                if line.get("live") and line.get("elapsed") is not None \
                        and not line.get("paused") and line.get("server_time"):
                    line["elapsed"] = round(line["elapsed"] + now - line["server_time"], 3)
                    line["server_time"] = round(now, 3)
            _json(h, 200, {"ok": ok, **detail})
        else:
            _json(h, detail.pop("status", 404), {"ok": ok, **detail})
    elif path == "/targets":
        # Everything a message can be pointed at — running sessions and
        # the directories a fresh one can open in — so the app renders a
        # list instead of working one out from the library.
        ok, detail = sessions.targets(_bearer(h))
        _json(h, 200 if ok else detail.pop("status", 403), {"ok": ok, **detail})
    elif path == "/harnesses":
        # The four harnesses and what each needs — is it installed, is it
        # signed in — so the app can offer the buttons that would fix it.
        ok, detail = harnesses.agents(_bearer(h))
        _json(h, 200 if ok else detail.pop("status", 403), {"ok": ok, **detail})
    elif path == "/harnesses/screen":
        # The install or sign-in window, as the desk sees it.
        ok, detail = harnesses.screen((parse_qs(query).get("pane") or [""])[0], _bearer(h))
        _json(h, 200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
    elif path == "/conversations":
        # What the assistant button can be pointed at: live sessions and
        # recent conversations, by title. Gated like /conversation.
        # (/sessions is taken: the amux list the popup reads.)
        user, err = auth.gate(_bearer(h))
        if not user:
            _json(h, err.get("status", 401), {"ok": False, "error": "not allowed"})
        else:
            _json(h, 200, {"ok": True, "sessions": sessions.sessions_index()})
    elif path == "/sessions/state":
        ok, detail = sessions.session_states(_bearer(h))
        _json(h, 200 if ok else detail.pop("status", 403), {"ok": ok, **detail})
    elif path == "/draft":
        # What the app's reply box was left holding for this session.
        ok, detail = drafts.draft_read((parse_qs(query).get("session") or [""])[0], _bearer(h))
        _json(h, 200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
    elif path == "/speech/now":
        # The app's speech bar: /speech's live bit, named — the session's
        # title and library item — and gated by the caller's ABS bearer.
        ok, detail = speech.speech_now(_bearer(h), speech.current_state())
        # Polled every few seconds, so not every request: a refusal, and
        # the first answer each device gets, are what tell "the bar is
        # asking and being turned away" from "the bar never asked".
        who = h.client_address[0]
        if not ok or who not in _SPEECH_NOW_SEEN:
            _SPEECH_NOW_SEEN.add(who)
            print(f"speech/now: {who} -> {'ok' if ok else detail}", file=sys.stderr)
        _json(h, 200 if ok else detail.pop("status", 403), {"ok": ok, **detail})
    else:
        return False
    return True


def _base_url(h: BaseHTTPRequestHandler) -> str:
    """The address this request came in on, as the device should use it from
    now on: `Host` as the client sent it (so a tailnet name stays a name), and
    https when a TLS-terminating proxy in front says so (the Cloudflare link)
    — a device token must travel over https there (§9)."""
    proto = (h.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    proto = proto if proto in ("http", "https") else "http"
    host = (h.headers.get("Host") or "").strip()
    if not host:
        addr = h.server.server_address if getattr(h, "server", None) else ("", 0)
        host = f"{addr[0]}:{addr[1]}"
    return f"{proto}://{host}"


def _pair(h: BaseHTTPRequestHandler) -> None:
    """`POST /pair {"code", "device"}` — a pairing code for a device token (§9).

    No credential: the code is the credential, minted at the desk by someone
    with a shell. Wrong, used and expired codes all get the same 403, so the
    answer does not say which. Each failure counts against the source address,
    and past `devices.MAX_FAILURES` in `devices.FAIL_WINDOW_S` the answer is
    429 before the code is even looked at. A failure never burns the code —
    see devices.py for why.
    """
    import socket

    ip = h.client_address[0] if h.client_address else ""
    if devices.rate_limited(ip):
        print(f"pair: rate-limited {ip}", file=sys.stderr)
        _json(h, 429, {"ok": False, "code": "rate_limited",
                       "error": "too many pairing attempts; try again in a few minutes"})
        return
    body = _read_json(h) or {}
    got = devices.redeem(str(body.get("code") or ""), str(body.get("device") or ""), ip)
    if not got:
        print(f"pair: refused a code from {ip}", file=sys.stderr)
        _json(h, 403, {"ok": False, "code": "bad_pairing_code",
                       "error": "invalid or expired pairing code"})
        return
    print(f"pair: paired {got['device_id']} ({got['name']!r}) from {ip}", file=sys.stderr)
    _json(h, 200, {"ok": True, "token": got["token"], "device_id": got["device_id"],
                   "server": {"name": socket.gethostname(), "base": _base_url(h)}})


def _post(h: BaseHTTPRequestHandler, path: str) -> bool:
    if path == "/pair":
        _pair(h)
    elif path == "/share":
        # "Play with agent-media" from the app's share sheet: media-share's
        # /share, with the caller's ABS bearer instead of a token of its own.
        body = _read_json(h) or {}
        ok, detail = share.share_from_app(str(body.get("text") or ""),
                                          str(body.get("channel") or ""), _bearer(h))
        _json(h, 200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
    elif path == "/speech/ctl":
        # The app's speech bar buttons. The caller's ABS bearer, like
        # /reply, and only the listener's verbs (_APP_SPEECH_ACTIONS).
        ok, detail = auth.may_control_speech(_bearer(h))
        if not ok:
            _json(h, detail.pop("status", 403), {"ok": False, **detail})
            return True
        body = _read_json(h) or {}
        action = str(body.get("action") or "")
        if action not in speech._APP_SPEECH_ACTIONS:
            _json(h, 400, {"ok": False, "error": "unknown action"})
            return True
        # `arg` is the turn index for prev/replay, kept by the app the way
        # the popup keeps hist_idx: 1 is the latest reply.
        # `replay-id` carries a history row id instead, which is not an
        # index and is not clamped.
        try:
            arg = int(body.get("arg") or 1)
        except (TypeError, ValueError):
            arg = 1
        arg = max(1, arg) if action == "replay-id" else max(1, min(999, arg))
        out = speech.run_ctl(action, arg)
        print(f"speech/ctl: {action} -> {out.strip()[:120]!r}", file=sys.stderr)
        _json(h, 200, {"ok": True, "out": out})
    elif path == "/rename":
        # ⋮ → Rename, from the app. The name outlives the next turn and
        # reaches the terminal too (see book_tracks.rename).
        body = _read_json(h) or {}
        ok, detail = threads.rename_conversation(
            str(body.get("item") or ""), str(body.get("session") or ""),
            str(body.get("title") or ""), _bearer(h))
        _json(h, 200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
    elif path == "/reply":
        # Reply to a conversation from inside the Audiobookshelf player.
        # Deliberately NOT gated by the host's token: the credential here is
        # the caller's own ABS bearer, verified with ABS, so the phone carries
        # no secret of ours. See send.py and the proposal.
        # `session` is the v1 form (server-contract.md §10); `item` stays
        # until the ABS exit, and `session` wins when both are sent.
        body = _read_json(h) or {}
        ok, detail = send.reply(
            str(body.get("item") or ""), str(body.get("text") or ""), _bearer(h),
            quote=str(body.get("quote") or ""),
            mode=str(body.get("mode") or "continue"),
            session=str(body.get("session") or ""))
        status = detail.pop("status", 400)
        if not ok:
            # Which thread too: a refusal that names only the reason
            # cannot be told apart from the next one, and "no such item"
            # is a question about WHICH item was asked for.
            named = (f"session {str(body.get('session'))[:8]}" if body.get("session")
                     else f"item {str(body.get('item') or '')!r}")
            print(f"reply: refused {status} ({detail.get('error')}) "
                  f"for {named} from {h.client_address[0]}", file=sys.stderr)
        _json(h, 200 if ok else status, {"ok": ok, **detail})
    elif path == "/ask":
        # A fresh session from the phone: the assistant button, or "new
        # chat" in the app. Gated like /reply — the ABS bearer is the
        # credential — and it lands in the scratch tmux session.
        body = _read_json(h) or {}
        ok, detail = routing.ask_routed(
            str(body.get("text") or ""), _bearer(h),
            target=str(body.get("target") or ""),
            player_item=str(body.get("player_item") or ""),
            player_session=str(body.get("player_session") or ""),
            sticky=str(body.get("sticky") or ""),
            parse=body.get("parse", True) is not False,
            dry=body.get("dry") is True,
            agent=str(body.get("agent") or ""),
            project=str(body.get("project") or ""),
            cwd=str(body.get("cwd") or ""))
        status = detail.pop("status", 400)
        if not ok:
            print(f"ask: refused {status} ({detail.get('error')}) "
                  f"from {h.client_address[0]}", file=sys.stderr)
        _json(h, 200 if ok else status, {"ok": ok, **detail})
    elif path in ("/session/resume", "/session/close"):
        # Managing the session behind a conversation from the app: bring
        # it back in a tmux window, or close the pane it runs in. Gated
        # like /reply.
        body = _read_json(h) or {}
        fn = send.session_resume if path.endswith("resume") else send.session_close
        ok, detail = fn(str(body.get("session") or ""), _bearer(h))
        _json(h, 200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
    elif path == "/session/archive":
        # Archive or un-archive a thread: a flag kept here, by session, now
        # that the ABS tag it used to be is going. It ends nothing — "End &
        # archive" in the app also sends /session/close. Gated like it.
        body = _read_json(h) or {}
        ok, detail = archive.session_archive(str(body.get("session") or ""),
                                             body.get("archived"), _bearer(h))
        _json(h, 200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
    elif path == "/session/answer":
        # Answering the dialog a session is stopped on — a permission
        # prompt, Codex's hooks review. A number, never text, and only
        # while that very question is still up (see send.answer).
        body = _read_json(h) or {}
        try:
            choice = int(body.get("choice"))
        except (TypeError, ValueError):
            choice = 0
        ok, detail = send.answer(str(body.get("session") or ""), choice,
                                 str(body.get("key") or ""), _bearer(h))
        if not ok:
            print(f"answer: refused ({detail.get('error')}) for "
                  f"{str(body.get('session'))[:8]}", file=sys.stderr, flush=True)
        _json(h, 200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
    elif path in ("/harnesses/run", "/harnesses/keys", "/harnesses/close"):
        # Getting an agent onto this host and signing into it, from the
        # app: a command in a background tmux window, its screen read and
        # typed into. Gated like /reply, and only the windows this opened
        # can be reached — see harnesses.py.
        body = _read_json(h) or {}
        bearer = _bearer(h)
        if path.endswith("run"):
            ok, detail = harnesses.run(str(body.get("agent") or ""),
                                       str(body.get("action") or ""), bearer)
        elif path.endswith("keys"):
            ok, detail = harnesses.keys(str(body.get("pane") or ""),
                                        str(body.get("text") or ""),
                                        str(body.get("key") or ""), bearer)
        else:
            ok, detail = harnesses.close(str(body.get("pane") or ""), bearer)
        if not ok:
            print(f"harnesses: refused ({detail.get('error')}) on {path} "
                  f"from {h.client_address[0]}", file=sys.stderr)
        _json(h, 200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
    elif path == "/draft":
        # Half a reply, held for next time the conversation is opened.
        # Gated like /reply: it is the same box, before the send.
        body = _read_json(h) or {}
        ok, detail = drafts.draft_write(
            str(body.get("session") or ""), str(body.get("text") or ""),
            body.get("at"), _bearer(h))
        _json(h, 200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
    elif path == "/focus":
        # The "opened in %23" link: pull the attached tmux client to a pane.
        body = _read_json(h) or {}
        bearer = _bearer(h)
        allowed = ((_TOKEN_OK is not None and _TOKEN_OK(h))
                   or auth.may_reply(auth.identity(bearer)[0])[0])
        if not allowed:
            _json(h, 401, {"error": "unauthorized"})
            return True
        ok, detail = send.focus(str(body.get("pane") or ""))
        _json(h, 200 if ok else 400, {"ok": ok, "detail": detail})
    else:
        return False
    return True


class Handler(BaseHTTPRequestHandler):
    """The app's routes and nothing else: the server without a canvas."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass

    def _not_found(self) -> None:
        _send(self, 404, b"not found\n", "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        if not dispatch(self, "GET", self.path.partition("?")[0]):
            self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        # Reject oversized bodies before reading a byte (#139).
        try:
            clen = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            clen = 0
        if clen > MAX_BODY:
            _send(self, 413, b"request body too large\n", "text/plain")
            return
        if not dispatch(self, "POST", self.path.split("?", 1)[0]):
            self._not_found()

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not dispatch(self, "OPTIONS", self.path.split("?", 1)[0]):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
