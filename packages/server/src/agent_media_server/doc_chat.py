"""Editing a project's document from the reader: its chat box and its menu.

The reader's box (and a paragraph's long-press menu) starts a fresh session
in the document's project, as a note's does in the notes tree (org_chat.py).
The first message names the document, the heading the paragraph sits under
and the paragraph itself, quoted whole, then what to do with it: a line
number moves once the file is edited, the words do not.

  POST /doc/ask {"path", "line"?, "action"?, "text"?, "notes"?, "agent"?}
       → /ask's answer (+ "path", "line")
  POST /doc/undo {"path", "sha"} → {"path", "undone", "sha"}: the last edit reverted
  GET  /doc/read also answers "chats": [{session, title, at}], newest first,
       and "last_edit": {sha, subject, at} when Undo can take it back

`action` is one of ACTIONS: the menu's Shorten, Expand, Plainer and Delete
need no words; Rewrite and Ask take `text`. With no `line` the message is
about the whole document. `notes` gathers margin notes made while reading
(`[{line, text}]`): one message quoting each paragraph with its note. An edit is committed on its own (only that file),
so it can be undone by reverting it. server-contract.md §6.10.
"""

from __future__ import annotations

import re
import subprocess
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
    "notes": "",
}
#: The actions that are complete without words of yours.
WORDLESS = {"shorten", "expand", "plainer", "delete"}
#: How much of a paragraph is quoted in the first message.
QUOTE_MAX = 1200
#: How many margin notes one message carries.
NOTES_MAX = 30

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


def notes_prompt(doc: dict, notes: list[dict], text: str) -> str:
    """The first message for margin notes: each paragraph quoted, then its note."""
    p = Path(doc["path"])
    parts = []
    for n, note in enumerate(notes, 1):
        para, under = block_at(doc["text"], note["line"], doc["fmt"] == "org")
        where = f'under "{_flat(under, 80)}", ' if under else ""
        parts.append(f'({n}) {where}line {note["line"] + 1}: "{_flat(para, 400)}" — {_flat(note["text"], 400)}')
    lead = (f'My notes on my document "{doc["title"]}" ({p}), made while reading it; '
            "act on each one: ")
    tail = (f" {text.strip()}" if (text or "").strip() else "")
    commit = (f" Edit {p.name} in place, change nothing else, and commit just that file "
              f"on its own (git commit -- {p.name}) so the edits can be undone.")
    return lead + " ".join(parts) + tail + commit


def _notes(raw) -> list[dict] | None:
    """`[{line, text}]` from the body, or None when it is not that."""
    if not isinstance(raw, list) or not raw or len(raw) > NOTES_MAX:
        return None
    out = []
    for n in raw:
        try:
            line, text = max(0, int(n["line"])), str(n["text"]).strip()
        except (TypeError, ValueError, KeyError):
            return None
        if not text:
            return None
        out.append({"line": line, "text": text})
    return sorted(out, key=lambda n: n["line"])


def ask(raw: str, line: int | None, action: str, text: str, bearer: str, *,
        agent: str = "", notes=None) -> tuple[bool, dict]:
    """Start a session about the document at `raw` (the paragraph on `line`)."""
    action = (action or "ask").strip().lower()
    if action not in ACTIONS:
        return False, {"error": f"unknown action {action!r}", "status": 400}
    if action == "notes":
        notes = _notes(notes)
        if notes is None:
            return False, {"error": f"notes: 1 to {NOTES_MAX} of {{line, text}}", "status": 400}
    elif not (text or "").strip() and action not in WORDLESS:
        return False, {"error": "empty message", "status": 400}
    ok, doc = doc_read.read(raw, bearer)
    if not ok:
        return False, doc
    p = Path(doc["path"])
    top = docs.project_root(p)
    cwd = str(top.resolve() if top else p.parent)
    first = notes_prompt(doc, notes, text) if action == "notes" else prompt(doc, line, action, text)
    ok, detail = send.ask(first, bearer, agent=agent, cwd=cwd, cwd_trusted=True)
    if ok and detail.get("session"):
        label = (f"{len(notes)} note{'s' if len(notes) != 1 else ''}" if action == "notes"
                 else (text or "").strip() or action.capitalize())
        org_chat._remember(doc["path"], "", {
            "session": detail["session"], "title": " ".join(label.split())[:80],
            "at": int(time.time())})
    return ok, {**detail, "path": doc["path"], "line": line}


def chats(path: str) -> list[dict]:
    """The chats started about this document, newest first."""
    return org_chat.chats(path, "")


# --- Undo: the last edit taken back ------------------------------------------
#
# An edit asked for here is committed on its own (the first message says
# so), so Undo is a revert of the last commit that touched the file — only
# when that commit touched nothing else and the file has no change since.


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return out.returncode, (out.stdout if out.returncode == 0 else out.stderr).strip()


def last_edit(path: str) -> dict | None:
    """`{sha, subject, at}`: the last commit to `path`, if Undo can take it back."""
    p = Path(path)
    top = docs.project_root(p)
    if not top:
        return None
    rel = str(p.resolve().relative_to(top.resolve()))
    code, out = _git(top, "log", "-1", "--format=%H%x00%s%x00%ct", "--", rel)
    if code or not out:
        return None
    sha, subject, at = out.split("\0")
    code, files = _git(top, "show", "--format=", "--name-only", sha)
    if code or files.splitlines() != [rel]:
        return None                         # it changed other files too: not ours to take back
    if _git(top, "diff", "--quiet", "HEAD", "--", rel)[0]:
        return None                         # changed since: Undo would lose that
    if not _git(top, "rev-parse", "--verify", "-q", f"{sha}^")[1]:
        return None                         # the first commit: nothing to go back to
    return {"sha": sha, "subject": subject, "at": int(at)}


def undo(raw: str, sha: str, bearer: str) -> tuple[bool, dict]:
    """Revert the last edit to the document, if it is still `sha` (the one
    the reader offered): a commit of its own, only that file."""
    ok, doc = doc_read.read(raw, bearer)
    if not ok:
        return False, doc
    p = Path(doc["path"])
    got = last_edit(str(p))
    if not got:
        return False, {"error": "nothing to undo here", "status": 409}
    if not sha or got["sha"] != sha:
        return False, {"error": "the document has changed since; look again", "status": 409,
                       "last_edit": got}
    top = docs.project_root(p).resolve()
    rel = str(p.resolve().relative_to(top))
    code, out = _git(top, "checkout", f"{sha}^", "--", rel)
    if code:
        return False, {"error": f"could not put back the version before ({out})", "status": 500}
    code, err = _git(top, "commit", "-q", "-m", f'Undo "{got["subject"]}"', "--", rel)
    if code:
        return False, {"error": f"could not commit the undo ({err})", "status": 500}
    return True, {"path": str(p), "undone": got["subject"], "sha": _git(top, "rev-parse", "HEAD")[1]}
