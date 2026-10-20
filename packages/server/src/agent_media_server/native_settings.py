"""Native Codex/Grok settings, between turns, never as an agent prompt.

Codex's /model opens a picker; an inline '/model id' does not select one.
The final 's session' action changes only this conversation (0.163.0).
Only send keys after seeing the expected picker, and verify the footer.
"""

from __future__ import annotations

import re
import time

from . import panes


def screen(pane: str) -> str:
    return panes.capture(pane, lines=18, ansi=False)


def plan_on(pane: str) -> bool:
    return bool(re.search(r"\bPlan mode\b", "\n".join(screen(pane).splitlines()[-6:]), re.I))


def _key(pane: str, key: str) -> None:
    panes._tmux(["send-keys", "-t", pane, key])
    time.sleep(0.2)


def _pick(pane: str, label: str) -> bool:
    """Walk the visible picker, inspecting its highlighted row each time.

    Bounded even when a CLI version changes its labels or cursor. No Enter
    is sent without an exact row match, so a stale catalogue cannot pick a
    neighbouring model. The picker may scroll as the cursor moves.
    """
    for _ in range(80):
        view = screen(pane)
        selected = next((line for line in view.splitlines()
                         if re.match(r"\s*[›❯>]\s*", line)), "")
        row = re.sub(r"^\s*[›❯>]\s*(?:\d+[.)]\s*)?", "", selected)
        if row == label or re.match(re.escape(label) + r"(?:\s+\(current\)|\s{2,})", row):
            return True
        if not selected:
            return False
        _key(pane, "Down")
    return False


def _codex_model(pane: str, model: str, label: str) -> tuple[bool, dict]:
    err = panes.send(pane, "/model")
    if err:
        return False, {"error": err, "status": 502}
    time.sleep(0.5)
    view = screen(pane)
    if "Select Model" not in view:
        return False, {"error": "Codex did not open its model picker", "status": 502}
    if "All models" in view and "Select Model and Effort" not in view:
        if not _pick(pane, "All models"):
            _key(pane, "Escape")
            return False, {"error": "Codex's full model list is unavailable", "status": 502}
        _key(pane, "Enter")
    if not _pick(pane, label):
        _key(pane, "Escape")
        return False, {"error": f"Codex did not offer {label} in its picker", "status": 502}
    # Some models have a single effort and expose the session action here;
    # others open a reasoning picker first. Keep its selected effort.
    if "s session" not in screen(pane):
        _key(pane, "Enter")
    view = screen(pane)
    if "s session" not in view or "Select " not in view:
        _key(pane, "Escape")
        return False, {"error": "Codex has no session-only model selection here", "status": 502}
    _key(pane, "s")
    for _ in range(10):
        view = screen(pane)
        footer = "\n".join(view.splitlines()[-6:]).lower()
        if "Select Model" not in view and "Select Reasoning" not in view and (
                model.lower() in footer or label.lower() in footer):
            return True, {"told": True}
        time.sleep(0.2)
    return False, {"error": f"Codex did not confirm {label} in its footer", "status": 502}


def configure(agent: str, pane: str, *, model: str | None, mode: str | None,
              offered: list) -> tuple[bool, dict]:
    if panes.is_herdr(pane):
        return False, {"error": "these controls need a tmux pane", "status": 400}
    if mode is not None and agent != "codex":
        return False, {"error": "plan switching is not supported for this harness yet", "status": 400}
    entry = next((m for m in offered if m["id"] == model), None)
    if model is not None and entry is None:
        return False, {"error": "model is not offered by this harness", "status": 400}
    if model is not None:
        if agent == "codex":
            ok, detail = _codex_model(pane, model, entry["label"])
            if not ok:
                return ok, detail
        else:
            err = panes.send(pane, f"/model {model}")
            if err:
                return False, {"error": err, "status": 502}
            for _ in range(10):
                time.sleep(0.2)
                footer = "\n".join(screen(pane).splitlines()[-6:]).lower()
                if model.lower() in footer or entry["label"].lower() in footer:
                    break
            else:
                return False, {"error": "Grok did not confirm the model change", "status": 502}
    if mode is not None:
        want = mode == "plan"
        for _ in range(2):
            if plan_on(pane) == want:
                break
            _key(pane, "BTab")
        if plan_on(pane) != want:
            return False, {"error": "Codex did not confirm the plan mode change", "status": 502}
    return True, {"told": True}
