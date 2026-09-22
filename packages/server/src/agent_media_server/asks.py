"""Claude Code's AskUserQuestion, read off a pane and answered from the phone.

A permission prompt is one numbered list and a number answers it. A question
is not always: it can be multi-select (checkboxes), offer a free-text row,
and ask several questions at once, one tab each, with a review page before it
is sent. This module reads that dialog (`parse`), shapes it as the thread's
`approval` (`approval`), and turns a structured answer into the keys that
give it (`drive`, called by send._answer_pane), checking the screen after
every step.

What the dialog looks like, measured on Claude Code 2.1.278 in a throwaway
pane (22 Sep 2026; captures in tests/fixtures/asks/):

    ────────────────────────────────────────
    ←  ☐ Colour  ☒ Pets  ✔ Submit  →          the tabs: ☐ open, ☒ answered
                                              (single single-select: " ☐ Size",
    Which pets?                                no arrows and no Submit tab)
    ❯ 1. [ ] Cat                              multi-select: checkboxes
      independent                             its description, indented 2
      2. [✔] Dog                              single-select: "2. Blue ✔",
      loyal                                    descriptions indented 5
      4. [ ] Type something                   the free-text row, always last
         Submit                               multi-select only: "next"
    ────────────────────────────────────────
      5. Chat about this
    Enter to select · Tab/Arrow keys to navigate · Esc to cancel

    Review your answers                       after the last tab
     ● Which pets?
       → Cat, Fish
    ❯ 1. Submit answers
      2. Cancel

The keys, as measured: a digit on a single-select picks that option and moves
to the next tab (or, with one question, sends it); on a multi-select it
toggles that box without moving the cursor. The digit of the free-text row
moves the cursor onto it, where typing fills it (and ticks it) — and where a
digit is text, not a toggle. Tab moves to the next tab, except from the
free-text row, where it moves to the Submit row; Enter there moves to the next
tab. Left goes back a tab. On the review page "1" sends. Narrow panes wrap a
checkbox across two lines ("[✔ Speech and" / "]  multi-line fixes").

The whole question — every tab, every option — is on screen one tab at a
time, and Claude Code writes the tool call to its transcript only after it is
answered. The PreToolUse hook keeps the tool input (agent_media_core.
pending_asks); when its question is the one on screen it is used as the
content, and the screen is the proof it is still up and says what is ticked.
"""

from __future__ import annotations

import hashlib
import re
import time

from . import panes

_RULE = re.compile(r"^\s*[─━▔▁_=]{6,}\s*$")
_OPTION = re.compile(r"^(\s*)([❯›>↓↑]?)\s*(\d+)\.\s+(.*)$")
_CHAT = re.compile(r"^\s*[❯›>]?\s*(\d+)\.\s+Chat about this\s*$")
_SUBMIT_ROW = re.compile(r"^\s*([❯›>]?)\s*Submit\s*$")
_BOX = re.compile(r"^\[([ ✔])(\]|\s)\s*(.*)$")
_TAB = re.compile(r"([☐☒✔])\s+(.+?)(?=\s{2,}[☐☒✔→]|\s*→|\s*$)")
_FREE_LABELS = ("Type something", "Type something.")
_CURSOR = ("❯", "›", ">")


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _tabs(line: str) -> list[dict] | None:
    """The tab row's headers, or None if `line` is not the tab row."""
    s = line.strip()
    if not s or _OPTION.match(line) or ("☐" not in s and "☒" not in s):
        return None
    found = _TAB.findall(s.lstrip("← "))
    if not found:
        return None
    tabs = [{"header": h.strip(), "answered": mark == "☒"}
            for mark, h in found if not (mark == "✔" and h.strip() == "Submit")]
    return tabs or None


def parse(cap: str) -> dict | None:
    """The AskUserQuestion dialog on an ANSI-stripped screen, or None.

    `{"review": False, "tabs": [{"header", "answered"}], "submit_tab": bool,
    "question": str, "multiSelect": bool, "options": [{"n", "label",
    "detail", "checked"}], "free": {"n", "text", "checked"} | None,
    "chat_n": int | None, "cursor": n | "submit" | None, "partial": bool}`,
    or on the review page `{"review": True, "tabs", "reviewed": [{"question",
    "answer"}], "unanswered": bool}`.
    """
    lines = [ln.rstrip() for ln in (cap or "").splitlines()]
    review_at = max((i for i, ln in enumerate(lines)
                     if ln.strip() == "Review your answers"), default=None)
    if review_at is None:
        # A short pane scrolls the title off (22 Sep 2026, 34x14): the page
        # is still known by its closing prompt, read from the first answer
        # that is left on screen.
        ready = max((i for i, ln in enumerate(lines)
                     if ln.strip().startswith(("Ready to submit your answers", "You have not answered all"))),
                    default=None)
        if ready is not None:
            first = next((i for i in range(ready) if lines[i].strip().startswith("● ")), ready)
            # The title goes back where it was, just above that answer.
            lines.insert(first, "Review your answers")
            review_at = first
    if review_at is not None and any(re.match(r"^\s*[❯›>]?\s*1\.\s+Submit answers", ln)
                                     for ln in lines[review_at:]):
        top = max((j for j in range(review_at) if _RULE.match(lines[j])), default=-1)
        tabs = next((t for t in (_tabs(ln) for ln in lines[top + 1:review_at]) if t), [])
        reviewed: list[dict] = []
        for ln in lines[review_at + 1:]:
            s = ln.strip()
            if s.startswith("● "):
                reviewed.append({"question": s[2:].strip(), "answer": ""})
            elif s.startswith("→") and reviewed:
                reviewed[-1]["answer"] = s[1:].strip()
            elif reviewed and s and not reviewed[-1]["answer"] and not s.startswith("Ready"):
                reviewed[-1]["question"] += " " + s
        return {"review": True, "tabs": tabs, "reviewed": reviewed,
                "unanswered": any("not answered all" in ln for ln in lines[review_at:])}
    chat_at = max((i for i, ln in enumerate(lines) if _CHAT.match(ln)), default=None)
    if chat_at is None:
        return None
    bottom = max((j for j in range(chat_at) if _RULE.match(lines[j])), default=None)
    if bottom is None:
        return None
    top = max((j for j in range(bottom) if _RULE.match(lines[j])), default=-1)
    region = lines[top + 1:bottom]
    tabs: list[dict] = []
    submit_tab = False
    question: list[str] = []
    rows: list[dict] = []
    cursor = None
    boxed = False
    open_bracket = False
    for ln in region:
        m = _OPTION.match(ln)
        if m:
            indent, mark, n, rest = m.groups()
            label, checked = rest.strip(), False
            b = _BOX.match(label)
            if b:
                boxed = True
                checked = b.group(1) == "✔"
                open_bracket = b.group(2) != "]"
                label = b.group(3).strip()
            else:
                open_bracket = False
                if label.endswith(" ✔"):
                    checked, label = True, label[:-2].rstrip()
            rows.append({"n": int(n), "label": label, "detail": "", "checked": checked,
                         "indent": len(indent) + (2 if mark else 0)})
            if mark in _CURSOR:
                cursor = int(n)
            continue
        s = _SUBMIT_ROW.match(ln)
        if s and rows:
            if s.group(1):
                cursor = "submit"
            open_bracket = False
            continue
        if not rows:
            t = _tabs(ln)
            if t is not None and not tabs:
                tabs = t
                submit_tab = "✔ Submit" in ln
            elif ln.strip():
                question.append(ln.strip())
            continue
        text = ln.strip()
        if not text:
            continue
        row = rows[-1]
        if open_bracket and text.startswith("]"):
            row["label"] = (row["label"] + " " + text[1:].strip()).strip()
            open_bracket = False
        elif boxed and len(ln) - len(ln.lstrip()) > row["indent"] + 2 and not row["detail"]:
            # A multi-select label the width wrapped (its descriptions sit at
            # the option's own indent, its wrapped words further in).
            row["label"] = (row["label"] + " " + text).strip()
        else:
            row["detail"] = (row["detail"] + " " + text).strip()
    if not rows:
        return None
    chat_n = int(_CHAT.match(lines[chat_at]).group(1))
    # The free-text row is the one just before "Chat about this" — by number,
    # since once something is typed its label is the words. A long list
    # scrolls it off the screen.
    free = rows.pop() if rows[-1]["n"] == chat_n - 1 else None
    for r in rows + ([free] if free else []):
        r.pop("indent", None)
    numbers = [r["n"] for r in rows] + ([free["n"]] if free else [])
    return {"review": False, "tabs": tabs, "submit_tab": submit_tab,
            "question": _norm(" ".join(question)), "multiSelect": boxed,
            "options": rows,
            "free": {"n": free["n"],
                     "text": "" if free["label"] in _FREE_LABELS else free["label"],
                     "checked": free["checked"]} if free else None,
            "chat_n": chat_n,
            "cursor": cursor,
            "partial": numbers != list(range(1, chat_n))}


def _hook_ask(session: str) -> dict | None:
    if not session:
        return None
    try:
        from agent_media_core import pending_asks
    except Exception:  # noqa: BLE001 — an older core: the screen alone
        return None
    return pending_asks.read(session)


def _label_eq(a: str, b: str) -> bool:
    """Two option labels the same, allowing for a wrap that cut one short."""
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def _match(scr: dict, hook: dict | None) -> int | None:
    """Which of the hook's questions is on screen (the review page: 0), or
    None when the hook's question is not the one up.

    The screen's question text is often only a fragment: on a phone-width
    pane the question wraps and the dialog scrolls, so what is left is its
    middle or its end, and a match on the opening words failed and sent
    David to the desk with the whole question saved (2026-09-22). So, in
    order: the words (either containing the other); the options (labels
    are short and survive the wrap); and, with one question pending and
    nothing on screen to compare, that one.
    """
    if not hook:
        return None
    hq = hook.get("questions") or []
    qs = [_norm(q.get("question") or "") for q in hq]
    if scr["review"]:
        seen = [_norm(r["question"]) for r in scr["reviewed"]]
        return 0 if seen and all(any(s in q or q in s for q in qs) for s in seen) else None
    cur = scr["question"]
    for i, q in enumerate(qs):
        if cur and (q == cur or q.startswith(cur) or cur.startswith(q) or cur in q or q in cur):
            return i
    shown = [_norm(o.get("label") or "") for o in scr.get("options") or []]
    if shown:
        for i, q in enumerate(hq):
            labels = [_norm(o.get("label") or "") for o in q.get("options") or []]
            if len(labels) == len(shown) and all(a == b or a.startswith(b) or b.startswith(a)
                                                 for a, b in zip(labels, shown)):
                return i
        # A short pane scrolls the dialog, so only some options are left,
        # and the "question" read is the tail of an option's description:
        # a two-question ask on a 46x20 pane showed option 2 alone, and the
        # phone got one question to answer for both (2026-09-22). Each
        # option keeps its number, so match those against the saved labels;
        # when exactly one question fits, it is that one.
        fits = []
        numbered = [(o.get("n"), _norm(o.get("label") or "")) for o in scr.get("options") or []]
        for i, q in enumerate(hq if all(isinstance(n, int) for n, _ in numbered) else []):
            labels = [_norm(o.get("label") or "") for o in q.get("options") or []]
            if all(1 <= n <= len(labels) and _label_eq(labels[n - 1], lab) for n, lab in numbered):
                fits.append(i)
        if len(fits) == 1:
            return fits[0]
    if len(hq) == 1 and not cur and not shown:
        # Nothing on screen to compare with (the dialog scrolled to its
        # footer): the one pending question is the best reading. With
        # anything visible, a mismatch wins — the hook's file can be stale.
        return 0
    return None


def approval(cap: str, session: str = "", agent: str = "claude") -> dict | None:
    """The thread's `approval` for a question dialog, or None if `cap` is not one.

    The v0 fields (`question`, `options` numbered as on screen, `partial`,
    `key`, `agent`) stay, so a client that answers by number still can; the
    question fields (`kind`, `multiSelect`, `free_text`, `questions`,
    `tool_use_id`) are what a structured answer is built from.
    """
    scr = parse(cap)
    if scr is None:
        return None
    hook = _hook_ask(session)
    at = _match(scr, hook)
    origin = "hook" if at is not None else "screen"
    if at is not None:
        qs = []
        for i, q in enumerate(hook["questions"]):
            opts = [{"n": j + 1, "label": o["label"], "description": o.get("description") or "",
                     "detail": o.get("description") or "", "checked": False}
                    for j, o in enumerate(q.get("options") or [])]
            if not scr["review"] and i == at and len(scr["options"]) == len(opts):
                for o, s in zip(opts, scr["options"]):
                    o["checked"] = s["checked"]
            qs.append({"question": q["question"], "header": q.get("header") or "",
                       "multiSelect": bool(q.get("multiSelect")), "free_text": True,
                       "options": opts})
        partial = False
        seed = "ask|" + str(hook.get("tool_use_id") or "") + "|" + "|".join(q["question"] for q in qs)
        tool_use_id = str(hook.get("tool_use_id") or "")
        current = qs[at]
    elif scr["review"]:
        # The review page without the hook's copy: the questions' words are
        # there, their options are not.
        qs = []
        partial = True
        seed = "review|" + "|".join(r["question"] for r in scr["reviewed"])
        tool_use_id = ""
        current = {"question": "", "options": []}
        at = 0
    else:
        opts = [{"n": o["n"], "label": o["label"], "description": o["detail"],
                 "detail": o["detail"], "checked": o["checked"]} for o in scr["options"]]
        one = {"question": scr["question"], "header": "", "multiSelect": scr["multiSelect"],
               "free_text": True, "options": opts}
        tabs = scr["tabs"]
        if len(tabs) > 1:
            # Several questions and only one of them on screen: the others'
            # words are on their own tabs, and which tab this is shows only
            # in colour. The one on screen is all that can be drawn, and a
            # number (this tab's) is all that can answer it.
            qs, at = [one], 0
            partial = True
        else:
            if tabs:
                one["header"] = tabs[0]["header"]
            qs, at = [one], 0
            partial = scr["partial"]
        seed = "screen|" + "|".join(t["header"] for t in tabs) + "|" + scr["question"] + "|" + \
            "|".join(f"{o['n']}.{o['label']}" for o in scr["options"])
        tool_use_id = ""
        current = qs[at]
        if partial:
            # Not all of it can be drawn, so none of it is offered as a card:
            # the app says "answer it at the desk" (and a number still works).
            qs = []
    if scr["review"]:
        v0 = [{"n": 1, "label": "Submit answers", "detail": ""},
              {"n": 2, "label": "Cancel", "detail": ""}]
        question = "Review your answers"
    else:
        v0 = [{"n": o["n"], "label": o["label"], "detail": o["detail"], "checked": o["checked"]}
              for o in scr["options"]]
        if scr["free"]:
            v0.append({"n": scr["free"]["n"], "label": _free(scr)["text"] or "Type something",
                       "detail": "", "checked": _free(scr)["checked"]})
        if scr["chat_n"]:
            v0.append({"n": scr["chat_n"], "label": "Chat about this", "detail": ""})
        question = current["question"] or scr["question"]
    return {"question": question,
            # With the hook's copy every question is known, however much of
            # the dialog a short pane scrolled away.
            "partial": partial or (origin == "screen" and scr.get("partial", False)),
            "options": v0,
            "key": hashlib.sha1(seed.encode()).hexdigest()[:12],
            "agent": agent,
            "kind": "question",
            "multiSelect": any(q["multiSelect"] for q in qs),
            "free_text": True,
            "questions": qs,
            "current": at,
            "review": scr["review"],
            "tool_use_id": tool_use_id,
            "source": origin}


# --- a structured answer ---------------------------------------------------------


class Refused(Exception):
    """An answer that does not fit the question: `status`, and the message."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalise(questions: list[dict], answers) -> list[dict]:
    """`answers`, in either shape, as `[{"selected": [n, …], "other": str}]`,
    one per question, checked against `questions` (each with numbered
    `options` and `multiSelect`).

    The shapes (server-contract.md §6.4): a list, `[{"question_index",
    "selected": [n, …], "other_text"?}]`, or the headless one, a dict
    `{question: label | [labels] | free text}` — a string that is not a
    label is the free text.
    """
    if not questions:
        raise Refused("nothing is being asked", 409)
    want: list[dict | None] = [None] * len(questions)
    if isinstance(answers, list):
        for a in answers:
            if not isinstance(a, dict):
                raise Refused("each answer is {question_index, selected, other_text?}")
            try:
                i = int(a.get("question_index", 0))
            except (TypeError, ValueError):
                raise Refused("question_index must be a number")
            if not 0 <= i < len(questions):
                raise Refused(f"no such question {i}")
            sel = a.get("selected") or []
            if not isinstance(sel, list):
                sel = [sel]
            try:
                sel = [int(n) for n in sel]
            except (TypeError, ValueError):
                raise Refused("selected holds option numbers")
            want[i] = {"selected": sel, "other": _norm(str(a.get("other_text") or ""))}
    elif isinstance(answers, dict):
        by_text = {_norm(q["question"]): i for i, q in enumerate(questions)}
        for q, a in answers.items():
            i = by_text.get(_norm(str(q)))
            if i is None:
                raise Refused(f"no such question {q!r}")
            labels = {o["label"]: o["n"] for o in questions[i]["options"]}
            got = a if isinstance(a, list) else [a]
            sel, other = [], []
            for x in got:
                x = str(x if x is not None else "").strip()
                if not x:
                    continue
                if x in labels:
                    sel.append(labels[x])
                else:
                    other.append(x)
            # One line: the free-text row is typed into, and a newline
            # there is Enter.
            want[i] = {"selected": sel, "other": _norm(", ".join(other))}
    else:
        raise Refused("answers needed: [{question_index, selected, other_text?}]")
    for i, (q, w) in enumerate(zip(questions, want)):
        name = q["question"] or q.get("header") or f"question {i + 1}"
        if w is None:
            raise Refused(f"no answer for {name!r}")
        known = {o["n"] for o in q["options"]}
        bad = [n for n in w["selected"] if n not in known]
        if bad:
            raise Refused(f"no option {bad[0]} in {name!r}")
        w["selected"] = sorted(set(w["selected"]), key=w["selected"].index)
        if not w["selected"] and not w["other"]:
            raise Refused(f"no answer for {name!r}")
        if not q["multiSelect"] and len(w["selected"]) + (1 if w["other"] else 0) > 1:
            raise Refused(f"{name!r} takes one answer")
    return want  # type: ignore[return-value]


def as_text(questions: list[dict], want: list[dict]) -> dict:
    """`{question: "label, label, other"}` — what the agent is handed."""
    out = {}
    for q, w in zip(questions, want):
        labels = {o["n"]: o["label"] for o in q["options"]}
        parts = [labels[n] for n in w["selected"]] + ([w["other"]] if w["other"] else [])
        out[q["question"]] = ", ".join(parts)
    return out


def as_labels(questions: list[dict], want: list[dict]) -> dict:
    """The headless driver's `answers`: `{question: label | [labels] | text}`."""
    out = {}
    for q, w in zip(questions, want):
        labels = {o["n"]: o["label"] for o in q["options"]}
        got = [labels[n] for n in w["selected"]] + ([w["other"]] if w["other"] else [])
        out[q["question"]] = got if q["multiSelect"] else got[0]
    return out


# --- the keys that give it ---------------------------------------------------------

#: tmux key names → herdr's.
_HERDR_KEYS = {"Enter": "enter", "Tab": "tab", "Left": "left", "Right": "right",
               "Up": "up", "Down": "down", "BSpace": "backspace", "Escape": "escape"}
#: How long one step may take to show on screen, and how often to look.
STEP_TIMEOUT_S = 2.0
STEP_POLL_S = 0.15


class Screen:
    """The pane a question is on: its keys and its reading. The tests swap it."""

    def __init__(self, pane: str, capture):
        self.pane = pane
        self._capture = capture

    def read(self) -> dict | None:
        return parse(panes.strip_ansi(self._capture(self.pane)))

    def key(self, name: str) -> None:
        if panes.is_herdr(self.pane):
            panes._run(["herdr", "pane", "send-keys", panes.herdr_pane(self.pane),
                        _HERDR_KEYS.get(name, name)])
        else:
            panes._tmux(["send-keys", "-t", self.pane, name])

    def type(self, text: str) -> None:
        if panes.is_herdr(self.pane):
            panes._run(["herdr", "pane", "send-text", panes.herdr_pane(self.pane), text])
        else:
            panes._tmux(["send-keys", "-t", self.pane, "-l", text])

    def sleep(self, s: float) -> None:
        time.sleep(s)


class Stuck(Exception):
    """The screen did not do what a key should have made it do."""


def _wait(screen: Screen, ok, what: str):
    deadline = time.monotonic() + STEP_TIMEOUT_S
    while True:
        screen.sleep(STEP_POLL_S)
        scr = screen.read()
        if ok(scr):
            return scr
        if time.monotonic() >= deadline:
            raise Stuck(what)


def _at(scr: dict | None, questions: list[dict], i: int) -> bool:
    return bool(scr) and not scr["review"] and \
        _match(scr, {"questions": questions}) == i


def _free(scr: dict | None) -> dict:
    return (scr or {}).get("free") or {"n": 0, "text": "", "checked": False}


def _clear(screen: Screen, scr: dict) -> dict:
    """Empty the free-text row the cursor is on."""
    old = _free(scr)["text"]
    for _ in old:
        screen.key("BSpace")
    if old:
        scr = _wait(screen, lambda s: bool(s) and not s["review"] and not _free(s)["text"],
                    "the free-text row would not clear")
    return scr


def drive(screen: Screen, questions: list[dict], want: list[dict], *,
          tabbed: bool) -> None:
    """Give `want` to the dialog on `screen`, one verified step at a time.

    `tabbed` is whether the dialog has a Submit tab (anything but a single
    single-select question, which a digit sends at once). Raises `Stuck`
    naming the step the screen did not follow.
    """
    n = len(questions)
    scr = screen.read()
    # Start from the first tab: someone at the desk may have moved on.
    for _ in range(n + 1):
        if _at(scr, questions, 0):
            break
        screen.key("Left")
        scr = _wait(screen, lambda s: bool(s) and (s["review"] or _match(s, {"questions": questions}) is not None),
                    "could not get back to the first question")
    if not _at(scr, questions, 0):
        raise Stuck("could not get back to the first question")
    for i, (q, w) in enumerate(zip(questions, want)):
        last = i == n - 1

        def moved_on(s, i=i, last=last):
            if not s:
                return not tabbed and last
            if s["review"]:
                return tabbed and last
            return not last and _match(s, {"questions": questions}) == i + 1

        free_n = len(q["options"]) + 1
        if not q["multiSelect"]:
            if w["other"]:
                screen.key(str(free_n))
                scr = _wait(screen, lambda s: _at(s, questions, i) and s["cursor"] == free_n,
                            f"the free-text row of {q['question']!r} would not take the cursor")
                scr = _clear(screen, scr)
                screen.type(w["other"])
                _wait(screen, lambda s: _at(s, questions, i) and _free(s)["text"] == w["other"],
                      f"the words did not land in {q['question']!r}")
                screen.key("Enter")
            else:
                screen.key(str(w["selected"][0]))
            scr = _wait(screen, moved_on, f"{q['question']!r} did not take its answer")
            continue
        # Multi-select. Digits toggle — unless the cursor is on the free-text
        # row, where they are typed — so get off it first.
        if scr["cursor"] == free_n:
            screen.key("Up")
            scr = _wait(screen, lambda s: _at(s, questions, i) and s["cursor"] != free_n,
                        "could not leave the free-text row")
        wanted = set(w["selected"])
        for o in scr["options"]:
            if o["checked"] != (o["n"] in wanted):
                screen.key(str(o["n"]))
                screen.sleep(0.1)
        scr = _wait(screen, lambda s: _at(s, questions, i) and
                    all(o["checked"] == (o["n"] in wanted) for o in s["options"]),
                    f"the boxes of {q['question']!r} would not tick")
        if w["other"]:
            cur = scr["cursor"] if isinstance(scr["cursor"], int) else 1
            for _ in range(max(0, free_n - cur)):
                screen.key("Down")
                screen.sleep(0.05)
            scr = _wait(screen, lambda s: _at(s, questions, i) and s["cursor"] == free_n,
                        f"the free-text row of {q['question']!r} would not take the cursor")
            scr = _clear(screen, scr)
            screen.type(w["other"])
            scr = _wait(screen, lambda s: _at(s, questions, i) and _free(s)["text"] == w["other"]
                        and _free(s)["checked"],
                        f"the words did not land in {q['question']!r}")
            # Tab from the free-text row goes to the Submit row; Enter there
            # moves on.
            screen.key("Tab")
            _wait(screen, lambda s: _at(s, questions, i) and s["cursor"] == "submit",
                  "could not reach the Submit row")
            screen.key("Enter")
        else:
            if _free(scr)["checked"]:
                screen.key(str(free_n))
                scr = _wait(screen, lambda s: _at(s, questions, i) and not _free(s)["checked"],
                            f"the free-text row of {q['question']!r} would not clear")
            screen.key("Tab")
        scr = _wait(screen, moved_on, f"{q['question']!r} did not take its answer")
    if tabbed:
        if not scr or not scr["review"]:
            raise Stuck("the review page did not come up")
        screen.key("1")
        _wait(screen, lambda s: s is None or not s["review"], "the answers were not sent")
