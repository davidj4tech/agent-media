"""Editing a project's document from the reader: its chat box and its menu.

The reader's box (and a paragraph's long-press menu) starts a fresh session
in the document's project, as a note's does in the notes tree (org_chat.py).
The first message names the document, the heading the paragraph sits under
and the paragraph itself, quoted whole, then what to do with it: a line
number moves once the file is edited, the words do not.

  POST /doc/ask {"path", "line"?, "action"?, "text"?, "agent"?}
       → /ask's answer (+ "path", "line")
  GET  /doc/read also answers "chats": [{session, title, at}], newest first

`action` is one of ACTIONS: the menu's Shorten, Expand, Plainer and Delete
need no words; Rewrite and Ask take `text`. With no `line` the message is
about the whole document. An edit is committed on its own (only that file),
so it can be undone by reverting it. server-contract.md §6.10.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from agent_media_core import docs

from . import doc_read, org_chat, send

#: The menu's actions: what each asks of the agent about the paragraph.
ACTIONS = {
    "ask": "",
    "rewrite": "Rewrite this paragraph:",
    "shorten": "Shorten this paragraph, keeping what it says.",
    "expand": "Expand this paragraph: say more, in the document's own voice.",
    "plainer": "Say this paragraph more plainly.",
    "delete": "Delete this paragraph.",
}
#: The actions that are complete without words of yours.
WORDLESS = {"shorten", "expand", "plainer", "delete"}
#: How much of a paragraph is quoted in the first message.
QUOTE_MAX = 1200

_MD_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_ORG_HEADING = re.compile(r"^\*+\s+(.+?)\s*$")
_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _heading(line: str, org: bool) -> str | None:
    m = (_ORG_HEADING if org else _MD_HEADING).match(line)
    return m.group(1) if m else None


def block_at(text: str, line: int, org: bool = False) -> tuple[str, str]:
    """`(paragraph, heading above it)` for the block starting on `line`
    (0-based, the reader's `data-line`): a heading is its own line, a list
    item runs to the next item, a fence to its close, anything else to a
    blank line or a heading."""
    lines = text.splitlines()
    if not 0 <= line < len(lines):
        return "", ""
    first = lines[line]
    under = ""
    for prev in reversed(lines[:line]):
        h = _heading(prev, org)
        if h:
            under = h
            break
    if _heading(first, org) is not None:
        return first.strip(), under
    out = [first]
    if _FENCE.match(first):
        for nxt in lines[line + 1:]:
            out.append(nxt)
            if _FENCE.match(nxt):
                break
        return "\n".join(out), under
    item = _ITEM.match(first)
    for nxt in lines[line + 1:]:
        if not nxt.strip() or _heading(nxt, org) is not None or _FENCE.match(nxt):
            break
        if item and (m := _ITEM.match(nxt)) and len(m.group(1)) <= len(item.group(1)):
            break
        if not item and _ITEM.match(nxt):
            break
        out.append(nxt)
    return "\n".join(out), under


def _flat(s: str, cap: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= cap else s[:cap - 1] + "…"


def prompt(doc: dict, line: int | None, action: str, text: str) -> str:
    """The first message, on one line (/ask flattens it anyway)."""
    p = Path(doc["path"])
    where = f'my document "{doc["title"]}" ({p})'
    para = ""
    if line is not None:
        para, under = block_at(doc["text"], line, doc["fmt"] == "org")
        if under:
            where += f', under "{_flat(under, 120)}"'
        if para:
            where += f', the paragraph at line {line + 1}: "{_flat(para, QUOTE_MAX)}"'
    what = " ".join(x for x in (ACTIONS.get(action, ""), (text or "").strip()) if x)
    if not para and action in ACTIONS and action != "ask":
        what = what.replace("this paragraph", "this document")
    commit = (f"If this asks for a change, edit {p.name} in place, change nothing else, "
              f"and commit just that file on its own (git commit -- {p.name}) "
              "so the edit can be undone.")
    return f"About {where}: {what} {commit}"


def ask(raw: str, line: int | None, action: str, text: str, bearer: str, *,
        agent: str = "") -> tuple[bool, dict]:
    """Start a session about the document at `raw` (the paragraph on `line`)."""
    action = (action or "ask").strip().lower()
    if action not in ACTIONS:
        return False, {"error": f"unknown action {action!r}", "status": 400}
    if not (text or "").strip() and action not in WORDLESS:
        return False, {"error": "empty message", "status": 400}
    ok, doc = doc_read.read(raw, bearer)
    if not ok:
        return False, doc
    p = Path(doc["path"])
    top = docs.project_root(p)
    cwd = str(top.resolve() if top else p.parent)
    ok, detail = send.ask(prompt(doc, line, action, text), bearer, agent=agent,
                          cwd=cwd, cwd_trusted=True)
    if ok and detail.get("session"):
        label = (text or "").strip() or action.capitalize()
        org_chat._remember(doc["path"], "", {
            "session": detail["session"], "title": " ".join(label.split())[:80],
            "at": int(time.time())})
    return ok, {**detail, "path": doc["path"], "line": line}


def chats(path: str) -> list[dict]:
    """The chats started about this document, newest first."""
    return org_chat.chats(path, "")
