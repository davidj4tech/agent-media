"""Codex request_user_input through the same question contract as Claude.

Codex 0.163's overlay uses Question i/n, numbered single choices, and a
notes composer for free text. A digit commits AND advances: never follow it
with Enter. Sources: codex-rs/tui/src/bottom_pane/request_user_input/{mod,render}.rs.
The rollout supplies all questions when available; the screen always proves
which request is still live. Otherwise offer the visible question one at a time.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import asks, panes

_PROGRESS = re.compile(r"^\s*Question (\d+)/(\d+)(?:\s|$)")
_FOOTER = re.compile(r"(?:to submit (?:answer|all)|to navigate questions|to interrupt|to add notes)")
_OTHER = "None of the above"


def parse(cap: str) -> dict | None:
    lines = cap.splitlines()
    heads = [(i, _PROGRESS.match(ln)) for i, ln in enumerate(lines) if _PROGRESS.match(ln)]
    if not heads:
        return None
    start, match = heads[-1]
    index, total = int(match[1]) - 1, int(match[2])
    if not 0 <= index < total:
        return None
    prompt, options = [], []
    cursor = None
    notes = None
    hidden = False
    for ln in lines[start + 1:]:
        s = ln.strip()
        if re.search(r"option \d+/\d+", s):
            hidden = True
        if _FOOTER.search(s):
            break
        if re.search(r"option \d+/\d+", s):
            continue
        m = re.match(r"^\s*›\s+(?!\d+\.)(.*)$", ln)
        if m:
            notes = m[1]
            continue
        m = re.match(r"^\s*([›>]?)\s*(\d+)\.\s+(.*)$", ln)
        if m:
            n = int(m[2])
            parts = re.split(r"\s{2,}", m[3].strip(), maxsplit=1)
            options.append({"n": n, "label": parts[0], "detail": parts[1] if len(parts) > 1 else ""})
            if m[1]:
                cursor = n
        elif s and not asks._RULE.match(ln):
            if notes is not None:
                notes = (notes + " " + s).strip()
            elif options:
                options[-1]["detail"] = (options[-1]["detail"] + " " + s).strip()
            else:
                prompt.append(s)
    numbers = [o["n"] for o in options]
    return {"question": " ".join(prompt), "options": options,
            "index": index, "total": total, "cursor": cursor, "notes": notes,
            "partial": hidden or numbers != list(range(1, len(numbers) + 1))}


def _pending(session: str) -> dict | None:
    from agent_media_core import harnesses, pending_asks

    kept = pending_asks.read(session)
    if kept and kept.get("harness") == "codex":
        return {"questions": kept["questions"], "id": kept.get("tool_use_id") or "", "source": "hook"}

    found = harnesses.transcript(session)
    if not found or found[0] != "codex":
        return None
    pending = None
    try:
        # Bound polling cost on long conversations. An unfinished line is
        # ignored until the next capture; no writes to the harness store.
        with found[1].open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2 * 1024 * 1024))
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                p = record.get("payload") or {}
                if p.get("type") == "function_call" and p.get("name", "").split(".")[-1] == "request_user_input":
                    try:
                        args = json.loads(p.get("arguments") or "{}")
                    except (ValueError, TypeError):
                        continue
                    pending = {"questions": args.get("questions") or [], "id": p.get("call_id") or ""}
                elif pending and p.get("type") == "function_call_output" and p.get("call_id") == pending["id"]:
                    pending = None
    except OSError:
        return None
    return pending


def _shape(q: dict) -> dict:
    opts = [{"n": i + 1, "label": o["label"],
             "description": o.get("description") or o.get("detail") or "",
             "detail": o.get("description") or o.get("detail") or ""}
            for i, o in enumerate(q.get("options") or [])]
    return {"question": q["question"], "header": q.get("header") or "",
            "multiSelect": False, "free_text": q.get("isOther", q.get("is_other", True)),
            "options": opts}


def approval(cap: str, session: str = "") -> dict | None:
    scr = parse(cap)
    if scr is None:
        return None
    shown = [o for o in scr["options"] if o["label"] != _OTHER]
    one = {"question": scr["question"], "options": shown,
           "is_other": any(o["label"] == _OTHER for o in scr["options"]) or not shown}
    qs, at, tool_id, source = [_shape(one)], 0, "", "screen"
    pending = _pending(session) if session else None
    if pending and len(pending["questions"]) == scr["total"]:
        candidates = [_shape(q) for q in pending["questions"]]
        current = candidates[scr["index"]]
        labels = {o["n"]: o["label"] for o in current["options"]}
        if (asks._same(current["question"], scr["question"])
                and all(asks._same(o["label"], labels.get(o["n"], "")) for o in shown)):
            qs, at, tool_id, source = candidates, scr["index"], pending["id"], pending.get("source", "rollout")
    seed = json.dumps([tool_id, qs], sort_keys=True)
    return {"question": qs[at]["question"], "options": scr["options"],
            "key": hashlib.sha1(seed.encode()).hexdigest()[:12], "agent": "codex",
            "kind": "question", "multiSelect": False, "free_text": qs[at]["free_text"],
            "questions": qs, "current": at, "review": False,
            "partial": scr["partial"] or not scr["question"],
            "tool_use_id": tool_id, "source": source,
            "screen_index": scr["index"]}


class Screen(asks.Screen):
    def read(self):
        return parse(panes.strip_ansi(self._capture(self.pane)))


def drive(screen, questions, want, start_index=0):
    for offset, (q, w) in enumerate(zip(questions, want)):
        index = start_index + offset
        scr = screen.read()
        # Full-rollout answers can begin while a later tab is on screen.
        while scr and scr["index"] > index:
            before = scr["index"]
            screen.key("Left")
            scr = asks._wait(screen, lambda s: s and s["index"] == before - 1,
                             "could not return to the question")
        if not scr or scr["index"] != index or not asks._same(scr["question"], q["question"]):
            raise asks.Stuck("the question changed")
        if scr["notes"] not in (None, "Add notes", "Type your answer (optional)"):
            raise asks.Stuck("notes are already being edited at the desk")
        if not w["other"] and scr["notes"] is not None:
            raise asks.Stuck("the notes composer is open: finish editing it at the desk")
        if w["other"]:
            if not q["free_text"]:
                raise asks.Stuck("this question does not accept free text")
            if scr["options"]:
                other = next((o["n"] for o in scr["options"] if o["label"] == _OTHER), None)
                if other is None or scr["cursor"] is None:
                    raise asks.Stuck("the free-text option is not visible")
                # Arrows select without committing. A digit would submit.
                while scr["cursor"] != other:
                    before = scr["cursor"]
                    screen.key("Down")
                    scr = asks._wait(screen, lambda s: s and s["index"] == index and s["cursor"] != before,
                                     "could not select free text")
                if scr["notes"] is None:
                    screen.key("Tab")
                    scr = asks._wait(screen, lambda s: s and s["index"] == index and s["notes"] == "Add notes",
                                     "the notes composer did not open")
            screen.type(w["other"])
            asks._wait(screen, lambda s: s and s["index"] == index and s["notes"] is not None
                       and asks._same(s["notes"], w["other"]), "the typed answer did not appear")
            screen.key("Enter")
        else:
            # Codex commits the digit, including advancement/submission.
            screen.key(str(w["selected"][0]))
        asks._wait(screen, lambda s: not s or s["index"] != index
                   or not asks._same(s["question"], q["question"]), "the answer did not advance")


def answer(session, pane, dialog, choice, answers):
    from . import sessions

    qs = dialog["questions"]
    if dialog["partial"]:
        return False, {"error": "only part of this question is visible", "status": 409, "approval": dialog}
    if answers is None:
        # A numbered tap answers just the visible question.
        qs = [qs[dialog["current"]]]
        answers = [{"question_index": 0, "selected": [choice]}]
        start = dialog["screen_index"]
    else:
        start = 0 if dialog["source"] != "screen" else dialog["screen_index"]
    try:
        want = asks.normalise(qs, answers)
        for q, w in zip(qs, want):
            if w["other"] and not q["free_text"]:
                raise asks.Refused("this question does not accept free text")
        drive(Screen(pane, sessions._capture_pane), qs, want, start)
    except asks.Refused as e:
        return False, {"error": str(e), "status": e.status, "approval": dialog}
    except asks.Stuck as e:
        return False, {"error": str(e), "status": 504,
                       "approval": sessions.approval_for(pane, "codex", session)}
    now = sessions.approval_for(pane, "codex", session)
    return True, {"session": session, "pane": pane, "answers": asks.as_text(qs, want),
                  "waiting": bool(now), "approval": now}
