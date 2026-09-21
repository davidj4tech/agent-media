"""Installing an agent, and signing into it, from the app."""

import pytest

from agent_media_core import harnesses
from agent_media_server import auth_abs, panes, send, sessions
from agent_media_server import harnesses as agents


@pytest.fixture(autouse=True)
def _here(tmp_path, monkeypatch):
    """A registry of our own, an open gate, and a tmux that says yes."""
    monkeypatch.setenv("MEDIA_AGENT_SETUP_DIR", str(tmp_path / "setup"))
    monkeypatch.setattr(auth_abs, "may_control_speech", lambda bearer: (True, {}))
    monkeypatch.setattr(send, "ask_target", lambda: ("scratch", "/home/ryer", []))
    monkeypatch.setattr(send, "ensure_host", lambda host, cwd: True)


def _tmux(monkeypatch, answer="%7", record=None):
    def fake(argv, timeout=10):
        if record is not None:
            record.append(argv)
        if argv[0] == "new-window":
            return answer
        if argv[0] == "display":
            return "%7"
        return ""
    monkeypatch.setattr(panes, "_tmux", fake)


# --- what is here ------------------------------------------------------------

def test_a_missing_agent_offers_install_and_not_login(monkeypatch):
    monkeypatch.setattr(harnesses, "program", lambda name: "" if name == "codex" else "/x/" + name)
    monkeypatch.setattr(harnesses, "auth_state", lambda name, **k: ("in", "david@example"))
    monkeypatch.setattr(harnesses, "version_of", lambda name, **k: "1.2.3")
    ok, detail = agents.agents("bearer")
    rows = {r["name"]: r for r in detail["agents"]}
    assert ok
    assert rows["codex"]["present"] is False
    assert rows["codex"]["actions"] == ["install"]
    assert rows["codex"]["installed_action"] == "install"
    # Installed: the same button is an update, and signing in is on offer.
    assert rows["claude"]["installed_action"] == "update"
    assert rows["claude"]["actions"] == ["install", "login"]


def test_pi_is_honest_about_having_no_sign_in(monkeypatch):
    monkeypatch.setattr(harnesses, "program", lambda name: "/x/" + name)
    monkeypatch.setattr(harnesses, "auth_state", lambda name, **k: ("unknown", ""))
    monkeypatch.setattr(harnesses, "version_of", lambda name, **k: "")
    _ok, detail = agents.agents("bearer")
    rows = {r["name"]: r for r in detail["agents"]}
    assert "login" not in rows["pi"]["actions"]        # API keys, not a login
    assert rows["pi"]["auth"] == "unknown"
    # Hermes present: it updates itself, and its wizard is the sign-in.
    assert rows["hermes"]["actions"] == ["install", "login"]


def test_hermes_cannot_be_conjured_from_nothing(monkeypatch):
    # Its install is a checkout Nous's own installer makes; only once it is
    # here is there a command (its own updater) to offer.
    monkeypatch.setattr(harnesses, "program", lambda name: "")
    assert harnesses.install_argv("hermes") == []
    monkeypatch.setattr(harnesses, "program", lambda name: "/x/" + name)
    assert harnesses.install_argv("hermes") == ["/x/hermes", "update", "--yes"]


def test_the_gate_is_the_app_bearer(monkeypatch):
    monkeypatch.setattr(auth_abs, "may_control_speech",
                        lambda bearer: (False, {"error": "not allowed", "status": 403}))
    assert agents.agents("")[0] is False
    assert agents.run("claude", "install", "")[0] is False
    assert agents.keys("%7", "hi", "", "")[0] is False


# --- running it --------------------------------------------------------------

def test_install_opens_a_window_running_the_recipe(monkeypatch):
    calls = []
    _tmux(monkeypatch, record=calls)
    monkeypatch.setattr(harnesses, "install_argv", lambda name: ["/x/npm", "install", "-g", "p"])
    ok, detail = agents.run("pi", "install", "bearer")
    assert ok and detail["pane"] == "%7"
    cmd = calls[0][-1]
    assert "/x/npm install -g p" in cmd
    # The window outlives the command, and what follows it is not a shell.
    assert "[finished: %s]" in cmd and "exec sleep" in cmd


def test_login_of_a_missing_agent_is_refused(monkeypatch):
    _tmux(monkeypatch)
    monkeypatch.setattr(harnesses, "login_argv", lambda name: [])
    monkeypatch.setattr(harnesses, "installed", lambda name: False)
    ok, detail = agents.run("codex", "login", "bearer")
    assert ok is False and detail["status"] == 409
    assert "not installed" in detail["error"]


def test_only_agents_and_actions_we_know(monkeypatch):
    _tmux(monkeypatch)
    assert agents.run("rm -rf", "install", "bearer")[1]["status"] == 400
    assert agents.run("claude", "shell", "bearer")[1]["status"] == 400


# --- watching and answering it ------------------------------------------------

def _opened(monkeypatch):
    _tmux(monkeypatch)
    monkeypatch.setattr(harnesses, "login_argv", lambda name: ["/x/codex", "login"])
    ok, detail = agents.run("codex", "login", "bearer")
    assert ok
    return detail["pane"]


def test_screen_reads_the_window_and_sees_it_finish(monkeypatch):
    pane = _opened(monkeypatch)
    monkeypatch.setattr(sessions, "_capture_pane",
                        lambda p: "Open https://auth.example/code\n\n[finished: 0]\n\n")
    ok, detail = agents.screen(pane, "bearer")
    assert ok and detail["done"] is True and detail["exit"] == 0
    assert detail["lines"][0].startswith("Open https://")
    assert detail["agent"] == "codex" and detail["action"] == "login"


def test_a_running_window_is_not_done(monkeypatch):
    pane = _opened(monkeypatch)
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: "Waiting for sign-in...")
    ok, detail = agents.screen(pane, "bearer")
    assert ok and detail["done"] is False and detail["exit"] is None


def test_a_pane_we_did_not_open_is_refused(monkeypatch):
    _opened(monkeypatch)
    for pane in ("%99", "%; kill", "", "$0"):
        assert agents.screen(pane, "bearer")[1]["status"] == 404
        assert agents.keys(pane, "y", "", "bearer")[1]["status"] == 404
        assert agents.close(pane, "bearer")[1]["status"] == 404


def test_typing_a_code_back_types_one_line(monkeypatch):
    pane = _opened(monkeypatch)
    sent = []
    monkeypatch.setattr(panes, "_tmux",
                        lambda argv, timeout=10: sent.append(argv) or "%7")
    ok, _ = agents.keys(pane, "ABC-123\nrm -rf /", "Enter", "bearer")
    assert ok
    typed = [a for a in sent if a[0] == "send-keys"]
    assert typed == [["send-keys", "-t", pane, "-l", "ABC-123"],
                     ["send-keys", "-t", pane, "Enter"]]


def test_only_keys_we_press(monkeypatch):
    pane = _opened(monkeypatch)
    assert agents.keys(pane, "", "C-z; ls", "bearer")[1]["status"] == 400
    assert agents.keys(pane, "", "", "bearer")[1]["status"] == 400


def test_a_window_that_went_away_is_gone_not_refused(monkeypatch):
    pane = _opened(monkeypatch)
    monkeypatch.setattr(panes, "_tmux", lambda argv, timeout=10: "")
    ok, detail = agents.screen(pane, "bearer")
    assert ok is False and detail["status"] == 410
    # And it is forgotten, so the id cannot be reused against us later.
    assert agents.screen(pane, "bearer")[1]["status"] == 404


def test_close_kills_the_window_once(monkeypatch):
    pane = _opened(monkeypatch)
    killed = []
    monkeypatch.setattr(panes, "_tmux",
                        lambda argv, timeout=10: killed.append(argv) or "%7")
    assert agents.close(pane, "bearer")[0] is True
    assert ["kill-pane", "-t", pane] in killed
    assert agents.close(pane, "bearer")[1]["status"] == 404
