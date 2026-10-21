"""`/session/settings` — a thread's model and plan mode, from the reply box.

The reply box's bottom row carries a model chip and a plan chip. `GET
/session/settings?session=` says what the thread runs now and whether it can
be changed from the phone; `POST /session/settings {session, model?, plan?}`
changes it; `/ask` takes the same two for a new chat (`send.ask`).

Codex exposes both chips through its native model picker and collaboration
mode. Grok exposes its model chip; their sheets come from their own account
catalogues (native_models.py), and native_settings.py verifies the changes.
Hermes and Gemini currently hide the chips. opencode has a model chip too: a new
chat's sheet is its free models (`new_sheet`, opencode_models.py) and `/ask`
starts it with `-m`; a running one in a pane is switched through its own
picker (the opencode section below), a headless one sends the new model with
its next prompt (sessiond_opencode.py). No plan mode for it. So has pi: the
sheet is what `pi --list-models` lists for the chat's harness profile
(pi_models.py), `/ask` starts it with `--provider`/`--model`, and a running
one is told `/model <provider/model>` (the pi section below). pi is always a
pane — sessiond has no pi adapter — and has no plan mode.

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
import logging
import os
import re
import threading
import time

from . import auth, driver, sessions

log = logging.getLogger(__name__)

#: The chip's sheet, in order: alias (what `--model` and `/model` take) and the
#: label kept for when Claude Code cannot be asked for its own (`models()`).
MODELS = (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("fable", "Fable"))
_ALIASES = {a for a, _ in MODELS}

#: Claude Code's list for a few minutes, so a sheet opened twice does not ask twice.
_MODELS_TTL_S = 300
_models_memo: tuple[float, list] = (0.0, [])

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
    """A new chat's model and mode as `ask` passes them on: Claude's, an alias
    from the sheet; opencode's, a `provider/model` id (opencode_models.py) and
    no plan mode. Anything else is dropped, not guessed."""
    if agent == "opencode":
        from agent_media_core import opencode_models

        return opencode_models.allowed(model), ""
    if agent == "pi":
        from agent_media_core import pi_models

        return pi_models.allowed(model), ""
    if agent in ("codex", "grok"):
        from agent_media_core import native_models

        return native_models.allowed(model), ("plan" if mode == "plan" and agent == "codex" else "")
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
        if v.get("agent") == "antigravity":
            mid = str(v.get("model") or v.get("init_model") or "")
            return {"agent": "antigravity", "live": bool(v.get("live")),
                    "model": mid, "model_id": mid, "plan": v.get("mode") == "plan",
                    "driver": driver.HEADLESS, "can": {"model": False, "plan": False}}
        if v.get("agent") == "opencode":
            # Its model rides on each prompt (sessiond_opencode.py): the one
            # chosen, else what its last reply ran on. No plan mode.
            from agent_media_core import opencode_models

            model = str(v.get("model") or "") or opencode_models.current(session)
            return {"agent": "opencode", "live": bool(v.get("live")), "model": model,
                    "model_id": model, "plan": False, "driver": driver.HEADLESS,
                    "can": {"model": True, "plan": False}}
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
    from agent_media_core import harnesses

    # A shared pane can show a shell or another agent. The thread's own
    # transcript identifies its harness even when pane detection falls back
    # to Claude.
    agent = harnesses.harness_of(session) or (
        sessions._agent_of_pane(pane) if pane else sessions.agent_of(session))
    if agent == "codex":
        from . import codex_control

        daemon = codex_control.state(session)
        if daemon is not None:
            return {"agent": agent, "live": True, "model": daemon["model"],
                    "model_id": daemon["model"], "plan": daemon["plan"], "driver": "codex",
                    "can": {"model": True, "plan": daemon["mode_known"]}}
    if agent == "opencode":
        return _opencode_state(session, pane)
    if agent == "pi":
        return _pi_state(session, pane)
    if agent in ("codex", "grok"):
        from agent_media_core import native_models
        from . import native_settings, panes

        model, at = native_models.current(session)
        with _LOCK:
            chosen = _CHOSEN.get(session)
        if chosen and chosen[1] > at:
            model = chosen[0]
        capable = bool(pane) and not panes.is_herdr(pane)
        return {"agent": agent, "live": bool(pane), "model": model, "model_id": model,
                "plan": bool(pane) and native_settings.plan_on(pane) if agent == "codex" else False,
                "driver": driver.PANE, "can": {"model": capable, "plan": capable and agent == "codex"}}
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


def _offered() -> list:
    """Claude Code's own `/model` list (claude_models.py), or [] without one."""
    global _models_memo
    at, memo = _models_memo
    if memo and time.time() - at < _MODELS_TTL_S:
        return memo
    from agent_media_core import claude_models

    try:
        offered = claude_models.models()
    except Exception as e:  # noqa: BLE001 — the names still make a sheet
        log.warning("session-settings: no model list from claude (%s)", e)
        offered = []
    if offered:
        _models_memo = (time.time(), offered)
    return offered


def models() -> list:
    """The sheet: `[{id, label, note}]`, one per alias, in MODELS' order.

    The label and note are Claude Code's own, from its `/model` list —
    "Haiku 4.5", "Fastest for quick answers" — so they follow its releases
    without an edit here. An alias Claude Code lists by full id only (Fable)
    takes its first, newest entry. Without an answer the sheet is the bare
    names.
    """
    offered = _offered()

    def entry(alias: str) -> dict:
        for m in offered:
            if m.get("value") == alias:
                return m
        return next((m for m in offered if f"-{alias}-" in str(m.get("value") or "")), {})

    return [{"id": alias, "label": str(entry(alias).get("displayName") or label),
             "note": str(entry(alias).get("description") or "")}
            for alias, label in MODELS]


def default_note() -> str:
    """What the host's default is, in Claude Code's words ("Opus 5.5 · Best
    for everyday, complex tasks"), for a new chat's "Default" row; or ""."""
    return next((str(m.get("description") or "") for m in _offered()
                 if m.get("value") == "default"), "")


def _answer(session: str, **extra) -> dict:
    st = state(session)
    sheet = ([] if st["agent"] == "antigravity"
             else _native_sheet(st["agent"], session) if st["agent"] in ("codex", "grok")
             else _opencode_sheet(st["model"]) if st["agent"] == "opencode"
             else _pi_sheet(session, st["model"]) if st["agent"] == "pi" else models())
    return {"session": session, "models": sheet, **st, **extra}


def _native_sheet(agent: str, session: str) -> list:
    from agent_media_core import harnesses, native_models

    return native_models.models(agent, harnesses.profile_of(session))


# --- opencode ----------------------------------------------------------------
#
# A running opencode's model is changed the way it is at the desk: `/models`
# opens its picker, the model's name typed into the picker's search, Enter.
# Its footer then says `Build · <name>`, which is checked; opencode keeps the
# choice for the session's next turn (measured 28 Sep 2026, 1.18.32: the
# next message went to the picked model). Between turns only, as Claude's.


def _opencode_sheet(current: str) -> list:
    """A running opencode thread's sheet: the free models, and the one it is
    on if that is not one of them (a gateway's, a paid one)."""
    from agent_media_core import opencode_models

    offered, _ = opencode_models.models()
    if current and not any(m["id"] == current for m in offered):
        offered = [{"id": current, "label": current.split("/", 1)[-1], "note": ""}, *offered]
    return offered


def _opencode_state(session: str, pane: str) -> dict:
    from agent_media_core import opencode_models

    from . import panes

    model = opencode_models.current(session)
    with _LOCK:
        chosen = _CHOSEN.get(session)
    if chosen and chosen[1] > _opencode_at(session):
        model = chosen[0]
    herdr = bool(pane) and panes.is_herdr(pane)
    return {"agent": "opencode", "live": bool(pane), "model": model, "model_id": model,
            "plan": False, "driver": driver.PANE,
            "can": {"model": bool(pane) and not herdr, "plan": False}}


def _opencode_at(session: str) -> float:
    """When the session's last message was written, in seconds."""
    from agent_media_core import harnesses

    rows = harnesses.opencode_rows("select max(time_created) from message where session_id = ?",
                                   (session,))
    return (rows[0][0] or 0) / 1000.0 if rows else 0.0


def _opencode_footer(screen: str) -> str:
    """What the composer's footer names after `Build · ` — the model and its
    provider — or "". Only the composer's line: a reply's header above it
    (`▣  Build · Big Pickle · 5.2s`) names the model that reply ran on."""
    for line in reversed(screen.splitlines()):
        m = re.match(r"\s*┃\s+\S+ · (.+?)\s*$", line)
        if m:
            return m.group(1).split("  ")[0] + " "
    return ""


def _configure_opencode(session: str, pane: str, model: str) -> tuple[bool, dict]:
    from agent_media_core import opencode_models

    from . import panes

    if panes.is_herdr(pane):
        return False, {"error": "an opencode model is not switchable in a herdr pane yet",
                       "status": 400}
    label = opencode_models.label_of(model, _opencode_sheet(""))
    if not label:
        return False, {"error": f"{model} is not one of this host's free models", "status": 400}
    err = panes.send(pane, "/models")
    if err:
        return False, {"error": err, "status": 502}
    time.sleep(1.2)
    try:
        panes._type_tmux(pane, label)
        time.sleep(1.0)
        panes._tmux(["send-keys", "-t", pane, "Enter"])
    except Exception as e:  # noqa: BLE001
        return False, {"error": f"send: {e}", "status": 502}
    time.sleep(1.0)
    if not _opencode_footer(panes.capture(pane, lines=12, ansi=False)).startswith(label + " "):
        panes._tmux(["send-keys", "-t", pane, "Escape"])
        return False, {"error": f"opencode did not take {label}", "status": 502}
    with _LOCK:
        _CHOSEN[session] = (model, time.time())
    return True, {"told": True}


# --- pi ----------------------------------------------------------------------
#
# A running pi is told `/model <provider/model>`: an exact id switches it
# there and then ("Model: <id>" above the composer, `(provider) model` in
# the footer, a `model_change` in its session file); anything else opens its
# picker on that search, which Escape closes. pi offers the id as an
# autocompletion as it is typed, and the first Enter takes that, so a
# composer still holding the command gets a second one. Measured 15 Oct
# 2026, pi 1.2.0, in a scratch window on the venice profile. Between turns
# only, as Claude's.


#: How many times to look for the footer to name the model, 0.6 s apart.
_PI_POLLS = 10


def _pi_sheet(session: str, current: str) -> list:
    """A running pi thread's sheet: its profile's models, and the one it is
    on if that is not among them."""
    from agent_media_core import harnesses, pi_models

    offered, _ = pi_models.models(harnesses.profile_of(session))
    if current and not any(m["id"] == current for m in offered):
        provider, _, name = current.partition("/")
        offered = [{"id": current, "label": name, "note": provider}, *offered]
    return offered


def _pi_state(session: str, pane: str) -> dict:
    from agent_media_core import pi_models

    from . import panes

    model, at = pi_models.current(session)
    with _LOCK:
        chosen = _CHOSEN.get(session)
    if chosen and chosen[1] > at:
        model = chosen[0]
    herdr = bool(pane) and panes.is_herdr(pane)
    return {"agent": "pi", "live": bool(pane), "model": model, "model_id": model,
            "plan": False, "driver": driver.PANE,
            "can": {"model": bool(pane) and not herdr, "plan": False}}


def _pi_footer(screen: str) -> str:
    """The `provider/model` pi's footer names — `(venice) kimi-k3`, on its
    last line or one of the two above it (an extension's status) — or ""."""
    for line in [ln for ln in screen.splitlines() if ln.strip()][-3:][::-1]:
        m = re.search(r"\(([^()\s]+)\) (\S+)", line)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return ""


def _configure_pi(session: str, pane: str, model: str) -> tuple[bool, dict]:
    from . import panes

    if panes.is_herdr(pane):
        return False, {"error": "a pi model is not switchable in a herdr pane yet",
                       "status": 400}
    command = f"/model {model}"
    err = panes.send(pane, command)
    if err:
        return False, {"error": err, "status": 502}
    time.sleep(1.2)
    screen = panes.capture(pane, lines=12, ansi=False)
    if _pi_footer(screen) != model and any(line.strip() == command
                                           for line in screen.splitlines()):
        # The autocompletion took the Enter; the command is still there.
        panes._tmux(["send-keys", "-t", pane, "Enter"])
    # An id it has not cached sends it to refresh its catalogs first, a
    # second or two ("Refreshing model catalogs…").
    for _ in range(_PI_POLLS):
        time.sleep(0.6)
        screen = panes.capture(pane, lines=12, ansi=False)
        if _pi_footer(screen) == model:
            break
    if _pi_footer(screen) != model:
        panes._tmux(["send-keys", "-t", pane, "Escape"])
        return False, {"error": f"pi did not take {model}", "status": 502}
    with _LOCK:
        _CHOSEN[session] = (model, time.time())
    return True, {"told": True}


def new_sheet(agent: str = "claude", profile: str = "") -> dict:
    """A new chat's sheet: Claude Code's aliases, opencode's free models, or
    the models pi lists for `profile` (a harness profile, "" its default
    one), with what its "Default" runs; {} for an agent with no chip."""
    if agent in ("codex", "grok"):
        from agent_media_core import harness_profiles, native_models

        if profile and not harness_profiles.get(agent, profile):
            return {"models": []}
        return {"models": native_models.models(agent, profile),
                "can": {"model": True, "plan": agent == "codex"}}
    if agent == "opencode":
        from agent_media_core import opencode_models

        offered, default = opencode_models.models()
        return {"models": offered, "default": opencode_models.default_note(offered, default)}
    if agent == "pi":
        from agent_media_core import harness_profiles, pi_models

        if profile and not harness_profiles.get("pi", profile):
            return {"models": []}
        offered, default = pi_models.models(profile)
        return {"models": offered, "default": pi_models.default_note(offered, default)}
    if agent != "claude":
        return {"models": []}
    return {"models": models(), "default": default_note()}


def get(session: str, bearer: str, agent: str = "claude",
        profile: str = "") -> tuple[bool, dict]:
    """A thread's settings; with no session, only the sheet (a new chat's,
    for `agent` in harness profile `profile`)."""
    session = (session or "").strip()
    if not session:
        user, err = auth.gate(bearer)
        return ((True, new_sheet((agent or "claude").strip().lower(), (profile or "").strip()))
                if user else (False, err))
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
    agent = sessions.agent_of(session)
    if model is not None and agent in ("codex", "grok"):
        if not isinstance(model, str) or model not in {m["id"] for m in _native_sheet(agent, session)}:
            return False, {"error": "model must be one of this harness's offered models", "status": 400}
    elif model is not None and agent in ("opencode", "pi"):
        from agent_media_core import opencode_models, pi_models

        model = (opencode_models if agent == "opencode" else pi_models).allowed(str(model))
        if not model:
            return False, {"error": "model must be a provider/model id", "status": 400}
    elif model is not None:
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
    if agent == "codex":
        from . import codex_control

        if codex_control.state(session) is not None:
            ok, detail = codex_control.configure(session, model=model, mode=mode)
        else:
            ok, detail = driver.for_session(session).configure(session, model=model, mode=mode)
    else:
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
    agent = sessions._agent_of_pane(pane)
    if agent not in ("claude", "opencode", "pi", "codex", "grok"):
        return False, {"error": "this harness has no settings controls here yet",
                       "status": 400}
    st = sessions.activity_of(session, pane).get("state") or "waiting"
    if st != "waiting":
        why = "a question" if st == "approval" else "the turn"
        return False, {"error": f"wait for {why} to finish, then change it", "status": 409,
                       "state": st}
    if agent in ("codex", "grok"):
        from . import native_settings

        ok, detail = native_settings.configure(agent, pane, model=model, mode=mode,
                                               offered=_native_sheet(agent, session))
        if ok and model is not None:
            with _LOCK:
                _CHOSEN[session] = (model, time.time())
        return ok, detail
    if agent == "opencode":
        if mode:
            return False, {"error": "opencode has no plan mode here", "status": 400}
        return _configure_opencode(session, pane, model) if model else (True, {"told": True})
    if agent == "pi":
        if mode:
            return False, {"error": "pi has no plan mode here", "status": 400}
        return _configure_pi(session, pane, model) if model else (True, {"told": True})
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
