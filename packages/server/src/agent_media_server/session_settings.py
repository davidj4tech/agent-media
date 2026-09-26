"""`/session/settings` — a thread's model and plan mode, from the reply box.

The reply box's bottom row carries a model chip and a plan chip. `GET
/session/settings?session=` says what the thread runs now and whether it can
be changed from the phone; `POST /session/settings {session, model?, plan?}`
changes it; `/ask` takes the same two for a new chat (`send.ask`).

Claude Code only: Codex, pi and Hermes answer `can: {model: false, plan:
false}` and the app hides the chips.

How, per driver (driver/):

* **headless** — sessiond's `configure` op writes the CLI's own control
  requests, `set_model` and `set_permission_mode` (both answered under `-p`
  stream-json, measured 26 Sep 2026 on 2.1.x), and keeps the choice on the
  session so `--resume` after a park starts with `--model` and
  `--permission-mode plan` again.
* **pane** — `/model <alias>` typed in, the way it would be at the desk, and
  shift+tab pressed round the TUI's mode cycle until the footer does (or
  does not) say "plan mode on". Only between turns: a command typed into a
  working pane would queue behind the turn as a message.

What a pane runs is read from its transcript — each assistant message names
its model — since the TUI's footer does not show it. A choice made since the
last reply is remembered here (`_CHOSEN`) until a reply carries it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from . import auth, driver, sessions

#: The chip's sheet, in order: alias (what `--model` and `/model` take) and label.
MODELS = (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("fable", "Fable"))
_ALIASES = {a for a, _ in MODELS}

#: session -> (alias, when) for a pane told `/model` that has not replied since.
_CHOSEN: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()

_PLAN_ON = re.compile(r"plan mode on", re.I)
#: How many shift+tabs to try: default → accept edits → plan → (bypass) → default.
_CYCLE = 5


def alias_of(model_id: str) -> str:
    """`claude-sonnet-5` → `sonnet`; "" for anything not on the sheet."""
    m = (model_id or "").lower()
    for a in _ALIASES:
        if a in m:
            return a
    return ""


def clean_new(agent: str, model: str, mode: str) -> tuple[str, str]:
    """A new chat's model and mode as `ask` passes them on: Claude's only,
    and only an alias from the sheet (anything else is dropped, not guessed)."""
    if agent != "claude":
        return "", ""
    model = (model or "").strip().lower()
    return (model if model in _ALIASES else ""), ("plan" if mode == "plan" else "")


def _last_model(session: str) -> tuple[str, float]:
    """The model named by the transcript's last assistant message, and when."""
    from . import transcript

    path = transcript.transcript_path(session)
    if not path:
        return "", 0.0
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            lines = f.read().splitlines()
    except OSError:
        return "", 0.0
    for raw in reversed(lines):
        if b'"assistant"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        model = str(((obj.get("message") or {}).get("model")) or "")
        if obj.get("type") == "assistant" and model and not model.startswith("<"):
            return model, _when(obj.get("timestamp"))
    return "", 0.0


def _when(stamp) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _pane_plan(pane: str) -> bool:
    from . import panes

    tail = "\n".join(panes.capture(pane, lines=8, ansi=False).rstrip("\n").splitlines()[-4:])
    return bool(_PLAN_ON.search(tail))


def state(session: str) -> dict:
    """`{agent, live, model, model_id, plan, can: {model, plan}}`."""
    drv = driver.for_session(session)
    if drv.kind == driver.HEADLESS:
        v = drv._get(session)
        mid = str(v.get("init_model") or "")
        model = str(v.get("model") or "") or alias_of(mid)
        # What the process last said wins while it runs (approving a plan
        # takes it out of plan mode); the choice kept for its resume otherwise.
        seen = str(v.get("init_mode") or "") if v.get("live") else ""
        plan = seen == "plan" if seen else v.get("mode") == "plan"
        # A parked session takes it too: it starts with it on its next resume.
        return {"agent": "claude", "live": bool(v.get("live")), "model": model,
                "model_id": mid, "plan": plan, "driver": driver.HEADLESS,
                "can": {"model": True, "plan": True}}
    pane = sessions.live_sessions().get(session, "")
    agent = sessions._agent_of_pane(pane) if pane else sessions.agent_of(session)
    if agent != "claude":
        return {"agent": agent, "live": bool(pane), "model": "", "model_id": "",
                "plan": False, "driver": driver.PANE, "can": {"model": False, "plan": False}}
    mid, at = _last_model(session)
    model = alias_of(mid)
    with _LOCK:
        chosen = _CHOSEN.get(session)
    if chosen and chosen[1] > at:
        model = chosen[0]
    from . import panes

    herdr = bool(pane) and panes.is_herdr(pane)
    return {"agent": "claude", "live": bool(pane), "model": model, "model_id": mid,
            "plan": bool(pane) and _pane_plan(pane), "driver": driver.PANE,
            "can": {"model": bool(pane), "plan": bool(pane) and not herdr}}


def _answer(session: str, **extra) -> dict:
    return {"session": session, "models": [{"id": a, "label": l} for a, l in MODELS],
            **state(session), **extra}


def get(session: str, bearer: str) -> tuple[bool, dict]:
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return True, _answer(session)


def post(session: str, body: dict, bearer: str) -> tuple[bool, dict]:
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    model = body.get("model")
    if model is not None:
        model = str(model).strip().lower()
        if model not in _ALIASES:
            return False, {"error": f"model must be one of {', '.join(a for a, _ in MODELS)}",
                           "status": 400}
    plan = body.get("plan")
    if plan is not None and not isinstance(plan, bool):
        return False, {"error": "plan must be true or false", "status": 400}
    if model is None and plan is None:
        return False, {"error": "nothing to change (model or plan)", "status": 400}
    mode = None if plan is None else ("plan" if plan else "")
    ok, detail = driver.for_session(session).configure(session, model=model, mode=mode)
    if not ok:
        return False, detail
    return True, _answer(session, told=bool(detail.get("told")))


def configure_pane(session: str, *, model: str | None = None,
                   mode: str | None = None) -> tuple[bool, dict]:
    """The pane driver's `configure`: typed and pressed, between turns only."""
    from . import panes

    pane = sessions.live_sessions().get(session, "")
    if not pane or not panes.alive(pane):
        return False, {"error": "the session is not running; resume it first", "status": 409}
    if sessions._agent_of_pane(pane) != "claude":
        return False, {"error": "only Claude Code sessions change model here", "status": 400}
    st = sessions.activity_of(session, pane).get("state") or "waiting"
    if st != "waiting":
        why = "a question" if st == "approval" else "the turn"
        return False, {"error": f"wait for {why} to finish, then change it", "status": 409,
                       "state": st}
    told = True
    if model is not None:
        err = panes.send(pane, f"/model {model}")
        if err:
            return False, {"error": err, "status": 502}
        with _LOCK:
            _CHOSEN[session] = (model, time.time())
        time.sleep(0.6)
    if mode is not None:
        if panes.is_herdr(pane):
            return False, {"error": "plan mode is not switchable in a herdr pane yet",
                           "status": 400}
        want = mode == "plan"
        for _ in range(_CYCLE):
            if _pane_plan(pane) == want:
                break
            panes._tmux(["send-keys", "-t", pane, "BTab"])
            time.sleep(0.4)
        told = _pane_plan(pane) == want
        if not told:
            return False, {"error": "pressed shift+tab round the modes but the pane "
                                    "never showed it", "status": 502}
    return True, {"session": session, "told": told, "pane": pane}
