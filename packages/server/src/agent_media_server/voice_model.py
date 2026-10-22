"""Voice mode's quick model: switch a thread to a faster model while the
hands-free loop is on, and back when it ends (David, 22 Oct 2026).

What and why is in agent_media_core/voice_mode.py (`VOICE_MODELS`,
`SUBAGENT_MODELS`, the note's subagent line). This is the switching:

* **In** — a `/reply` with `voice: true` (`on_reply`), before its words go in:
  the thread's model is read (session_settings.state), recorded
  (`voice_mode.record_switch`) and changed through its driver's `configure`,
  the same road the reply box's model chip takes — sessiond's `set_model`
  control request for a headless thread (session-only, kept for its resume),
  `/model <alias>` typed into a pane.
* **Out** — a reply without the flag, or the mark gone stale (`sweep`, run
  by every reply and by the idle reaper's pass): the recorded model is put
  back, unless someone changed the model meanwhile (then theirs stands).

Only between turns: a thread that is working, holding a question or has a
message queued is left as it is — the switch in waits for the next voice
reply, the switch out for the next reply or sweep. `/model` in Claude Code's
TUI runs at once even mid-turn (`immediate`), so this is checked here, not
left to the TUI.

Claude Code's TUI also saves a typed `/model` as the user's default for new
sessions ("Set model to Sonnet 5.5 and saved as your default for new
sessions", measured 23 Oct 2026, 2.1.308). Voice mode must not change what
the next desk session starts on, so the settings file's `model` is put back
as it was after each switch (`_keep_default`); the running session keeps the
model it was switched to (measured: rewriting the file does not move a live
session).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from agent_media_core import voice_mode

from . import driver, sessions

log = logging.getLogger(__name__)

_LOCK = threading.Lock()

#: How long to look for Claude Code's answer to a typed `/model`.
_CONFIRM_S = 6.0
#: How long to wait for its settings write after the answer.
_SAVE_S = 3.0
_POLL_S = 0.25

_ANSWER = re.compile(r"(Set model to|Kept model as)\b")


def _settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "settings.json"


_ABSENT = object()


def _default_model():
    try:
        data = json.loads(_settings_path().read_text())
    except (OSError, ValueError):
        return _ABSENT
    return data.get("model", _ABSENT) if isinstance(data, dict) else _ABSENT


def _restore_default(before) -> None:
    """Put the settings file's `model` back to `before` (absent = no key),
    leaving every other key as it is now."""
    path = _settings_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("model", _ABSENT) == before:
        return
    if before is _ABSENT:
        data.pop("model", None)
    else:
        data["model"] = before
    tmp = path.with_name(path.name + ".voice-model.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, path.stat().st_mode & 0o777)
        tmp.replace(path)
    except OSError as e:
        log.warning("voice-model: could not put the default model back (%s)", e)
        tmp.unlink(missing_ok=True)


def _answered(pane: str, model: str) -> bool:
    """Has the pane answered the `/model <model>` it was last typed?"""
    from . import panes

    lines = panes.capture(pane, lines=40, ansi=False).splitlines()
    typed = [i for i, ln in enumerate(lines) if f"/model {model}" in ln]
    if not typed:
        return False
    return any(_ANSWER.search(ln) for ln in lines[typed[-1]:])


def _configure_pane(session: str, model: str) -> bool:
    """`/model <model>` typed in (session_settings.configure_pane, which
    refuses a busy pane), its answer waited for, and the user's default
    model put back as it was."""
    from . import session_settings

    before = _default_model()
    ok, detail = session_settings.configure_pane(session, model=model)
    if not ok:
        log.info("voice-model: %s not switched to %s (%s)", session[:8], model,
                 detail.get("error"))
        return False
    pane = str(detail.get("pane") or sessions.live_sessions().get(session, ""))
    deadline = time.monotonic() + _CONFIRM_S
    answered = False
    while pane and time.monotonic() < deadline:
        if _answered(pane, model):
            answered = True
            break
        time.sleep(_POLL_S)
    if not answered:
        # Its answer is printed once the default is saved; without one seen,
        # give the write a moment so it is not put back before it lands.
        deadline = time.monotonic() + _SAVE_S
        while time.monotonic() < deadline and _default_model() == before:
            time.sleep(_POLL_S)
    _restore_default(before)
    return True


def _idle(session: str) -> tuple[bool, str]:
    """(may switch now, which driver). A parked headless thread may: the
    model is kept for its resume."""
    if driver.owned_headless(session):
        v = driver.headless_driver().view(session)
        if not v.get("live"):
            return True, driver.HEADLESS
        return (v.get("state") == "waiting" and not v.get("queued")), driver.HEADLESS
    pane = sessions.live_sessions().get(session, "")
    if not pane:
        return False, driver.PANE
    st = sessions.activity_of(session, pane).get("state")
    return st == "waiting", driver.PANE


def _current(session: str) -> tuple[str, str, str]:
    """(agent, the model to put back later, the alias it runs now). For a
    headless thread the model kept on it ("" = its default); for a pane the
    alias its last reply ran on, else the full id, else "default"."""
    from . import session_settings

    st = session_settings.state(session)
    agent = str(st.get("agent") or "")
    if st.get("driver") == driver.HEADLESS:
        kept = str(driver.headless_driver()._get(session).get("model") or "")
        return agent, kept, str(st.get("model") or "")
    alias = str(st.get("model") or "")
    return agent, alias or str(st.get("model_id") or "") or "default", alias


def _set(session: str, how: str, model: str) -> bool:
    if how == driver.PANE:
        return _configure_pane(session, model)
    ok, detail = driver.headless_driver().configure(session, model=model)
    if not ok:
        log.info("voice-model: %s not switched to %s (%s)", session[:8], model or "default",
                 detail.get("error"))
    return ok


def enter(session: str) -> bool:
    """Switch `session` to its harness's voice model, if it has one and is
    between turns. True when it was switched now."""
    with _LOCK:
        if voice_mode.switched(session):
            return False
        try:
            agent, prev, now = _current(session)
        except Exception as e:  # noqa: BLE001 — the reply still goes in
            log.warning("voice-model: could not read %s's model (%s)", session[:8], e)
            return False
        to = voice_mode.voice_model(agent)
        if not to or now == to:
            return False
        ok, how = _idle(session)
        if not ok:
            return False
        if not _set(session, how, to):
            return False
        voice_mode.record_switch(session, agent, prev, to)
        log.info("voice-model: %s %s -> %s", session[:8], prev or "default", to)
        return True


def leave(session: str) -> bool:
    """Put back what voice mode switched `session` from, between turns.
    True when the record is gone (put back, or someone changed the model
    meanwhile and theirs stands)."""
    with _LOCK:
        rec = voice_mode.switched(session)
        if not rec:
            return True
        try:
            _agent, _prev, now = _current(session)
        except Exception as e:  # noqa: BLE001
            log.warning("voice-model: could not read %s's model (%s)", session[:8], e)
            return False
        if now and now != rec.get("to"):
            voice_mode.clear_switch(session)
            return True
        if not (sessions.live_sessions().get(session) or driver.owned_headless(session)):
            # Closed: a pane's resume starts on the user's default anyway.
            voice_mode.clear_switch(session)
            return True
        ok, how = _idle(session)
        if not ok:
            return False
        prev = str(rec.get("prev") or "")
        if not _set(session, how, prev if how == driver.HEADLESS else (prev or "default")):
            return False
        voice_mode.clear_switch(session)
        log.info("voice-model: %s back to %s", session[:8], prev or "default")
        return True


def sweep() -> None:
    """Put back every switch whose voice mode has ended (a reply without the
    flag that found the thread busy, or a loop left to go stale)."""
    for rec in voice_mode.all_switched():
        session = str(rec.get("session") or "")
        if session and not voice_mode.active(session):
            try:
                leave(session)
            except Exception as e:  # noqa: BLE001 — the next sweep tries again
                log.warning("voice-model: could not put %s back (%s)", session[:8], e)


def on_reply(session: str, voice: bool) -> None:
    """A reply is about to go into `session` (send.reply): switch it in for
    the loop, or back out of it. Never raises — the words still go in."""
    try:
        if voice:
            enter(session)
        else:
            leave(session)
    except Exception as e:  # noqa: BLE001
        log.warning("voice-model: %s (%s)", session[:8], e)
    if voice_mode.all_switched():
        threading.Thread(target=sweep, daemon=True).start()
