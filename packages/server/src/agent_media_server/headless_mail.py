"""`media session list|send`: reach a headless session from another one.

A headless session (sessiond.py: a phone-started chat, `claude -p` under
media-sessiond) has no tmux pane, and sessiond strips `CLAUDE*` from its
environment, its messaging socket included, so it never registers an inbox:
`ListAgents` does not list it, `SendMessage` cannot reach it, and
agent-mail-deliver, finding no pane in its folder, started a new session
beside it (David, 19 Oct 2026: "that not being able to send to the headless
thread sounds like a gap"). This is the way in, through sessiond's own
`send` — the app's path:

* `list` shows the sessions sessiond holds: id, live, state, folder, title,
  and whether a card is up or work is in flight — `ListAgents` for these.
* `send` puts a message into one, named by id (or its first 8+ characters)
  or by folder: the live session there, the most recently active if several.
  A session mid-turn takes it after the turn (sessiond queues it; nothing is
  interrupted). A session with a question or permission card up is never
  sent to, since a message answers the card the way typing does: it is
  refused `pending` — atomically by a sessiond that knows `keep_pending`,
  and by a look first for one that does not — and with `--wait S` it is
  tried again until the card is answered or S seconds pass.

Nothing here records the words as David's turn (send._record_turn): they
are another session's, marked by the `[From …]` the caller puts on them.

Exit codes for `send`: 0 sent (`queued` when behind a turn), 4 a card is up
(after `--wait`), 5 no such session / no live session in that folder, 1 the
host is down or refused it, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

#: Polling a card while `--wait` lasts.
POLL_S = 2.0
RC_SENT, RC_FAILED, RC_USAGE, RC_PENDING, RC_NONE = 0, 1, 2, 4, 5


def _call(op: str, **kw) -> dict:
    from .driver import headless

    return headless.call(op, **kw)


def _real(path: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(path)).rstrip("/") or "/"
    except (OSError, ValueError):
        return path


def _title(view: dict) -> str:
    sid = str(view.get("session") or "")
    try:
        from . import sessions

        t = sessions._headless_title(sid, view)
        if t:
            return t
    except Exception:  # noqa: BLE001 — a name is a nicety
        pass
    t = " ".join(str(view.get("first_text") or "").split())
    return t if len(t) <= 60 else t[:59] + "…"


def rows() -> tuple[list[dict], bool]:
    """sessiond's sessions, newest activity first, and whether it answered
    (down: the records on disk, none of them live)."""
    from . import sessiond

    r = _call("list", timeout=5.0)
    up = bool(r.get("ok"))
    views = list(r.get("sessions") or []) if up else [
        {**rec, "live": False} for rec in sessiond.records()]
    views.sort(key=lambda v: float(v.get("last_event_at") or 0), reverse=True)
    return views, up


def brief(view: dict) -> dict:
    return {"session": str(view.get("session") or ""), "live": bool(view.get("live")),
            "state": str(view.get("state") or ""), "cwd": str(view.get("cwd") or ""),
            "title": _title(view), "agent": str(view.get("agent") or "claude"),
            "card": bool(view.get("pending")) or view.get("state") == "approval",
            "tasks": int(view.get("tasks") or 0), "queued": len(view.get("queued") or []),
            "last_event_at": view.get("last_event_at")}


def find(target: str, views: list[dict]) -> tuple[dict | None, str]:
    """The session `target` names: an id or 8+ character prefix of one, or a
    folder's live session (the most recently active). `(view, why_not)`."""
    target = (target or "").strip()
    if not target:
        return None, "no session or folder named"
    if target.startswith(("/", "~")):
        want = _real(target)
        here = [v for v in views if v.get("live") and _real(str(v.get("cwd") or "")) == want]
        if not here:
            return None, f"no live headless session in {target}"
        return here[0], ""
    if len(target) < 8:
        return None, "a session id needs at least 8 characters"
    hits = [v for v in views if str(v.get("session") or "").startswith(target)]
    if not hits:
        return None, f"no headless session {target}"
    if len(hits) > 1:
        return None, f"{target} names {len(hits)} sessions; give more of the id"
    return hits[0], ""


def _card_up(session: str) -> bool | None:
    r = _call("get", session=session, timeout=5.0)
    if not r.get("ok"):
        return None
    return bool(r.get("pending")) or r.get("state") == "approval"


def send(session: str, text: str, *, wait: float = 0.0) -> tuple[int, dict]:
    """Send `text` to `session`, never over a card; `(rc, answer)`."""
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        up = _card_up(session)
        if up is None:
            r = _call("get", session=session, timeout=5.0)
            return RC_FAILED, {"error": r.get("error") or "the session host did not answer",
                               "code": r.get("code") or "down"}
        if not up:
            r = _call("send", session=session, text=text, keep_pending=True, timeout=30.0)
            if r.get("ok"):
                return RC_SENT, {"session": session, "queued": bool(r.get("queued")),
                                 "resumed": bool(r.get("resumed")),
                                 "acked": bool(r.get("acked"))}
            if r.get("code") != "pending":
                return RC_FAILED, {"error": r.get("error") or "refused",
                                   "code": r.get("code") or "refused"}
        if time.monotonic() >= deadline:
            return RC_PENDING, {"error": "it has a question or permission waiting for an "
                                         "answer; not sent", "code": "pending",
                                "session": session}
        time.sleep(min(POLL_S, max(0.05, deadline - time.monotonic())))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="media session",
        description="headless sessions (sessiond): list them, or send one a message "
                    "from another session")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="the sessions sessiond holds, newest first")
    ls.add_argument("--all", action="store_true", help="parked ones too (default: live only)")
    ls.add_argument("--cwd", default="", help="only this folder's")
    ls.add_argument("--json", action="store_true")
    sd = sub.add_parser("send", help="a message into one, queued behind a running turn; "
                                     "never over a question or permission card")
    sd.add_argument("target", help="a session id (8+ characters) or a folder (its live session)")
    sd.add_argument("text", nargs="*", help="the message; '-' or nothing reads stdin")
    sd.add_argument("--from", dest="sender", default="",
                    help="who it is from: prefixed as '[From Name]' unless the text has one")
    sd.add_argument("--wait", type=float, default=0.0,
                    help="seconds to wait for a card to be answered (default 0)")
    sd.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:
        return RC_USAGE if e.code else 0
    views, up = rows()
    if a.cmd == "list":
        out = [brief(v) for v in views if a.all or v.get("live")]
        if a.cwd:
            want = _real(a.cwd)
            out = [b for b in out if _real(b["cwd"]) == want]
        if a.json:
            print(json.dumps({"sessiond": up, "sessions": out}))
            return 0
        if not up:
            print("sessiond is not answering: these are its records, none live", file=sys.stderr)
        for b in out:
            flags = [b["state"] or "?"] + (["card up"] if b["card"] else []) + (
                [f"{b['tasks']} task(s)"] if b["tasks"] else [])
            print(f"{b['session']}  {'live' if b['live'] else 'parked'}  "
                  f"{', '.join(flags):<24}  {b['cwd']}  {b['title']}")
        return 0
    text = " ".join(a.text).strip()
    if not text or text == "-":
        text = sys.stdin.read().strip()
    if not text:
        print("media session send: nothing to say", file=sys.stderr)
        return RC_USAGE
    if a.sender and not text.startswith("[From "):
        name = a.sender.strip()
        text = f"[From {name[:1].upper() + name[1:]}] {text}"
    if not up:
        msg = "the session host (media-sessiond) is not running"
        print(json.dumps({"ok": False, "code": "down", "error": msg}) if a.json
              else f"media session send: {msg}", file=sys.stdout if a.json else sys.stderr)
        return RC_FAILED
    view, why = find(a.target, views)
    if view is None:
        print(json.dumps({"ok": False, "code": "none", "error": why}) if a.json
              else f"media session send: {why}", file=sys.stdout if a.json else sys.stderr)
        return RC_NONE
    rc, ans = send(str(view["session"]), text, wait=a.wait)
    if a.json:
        print(json.dumps({"ok": rc == RC_SENT, **ans}))
    elif rc == RC_SENT:
        print(f"sent to {ans['session']}" + (" (queued behind its turn)" if ans["queued"] else "")
              + (" (resumed)" if ans["resumed"] else ""))
    else:
        print(f"media session send: {ans.get('error')}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
