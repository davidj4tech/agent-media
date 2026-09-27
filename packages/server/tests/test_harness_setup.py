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
    # Signed in, so signing out is too — and never on the one that is not here.
    assert rows["claude"]["installed_action"] == "update"
    assert rows["claude"]["actions"] == ["install", "login", "logout"]
    assert "logout" not in rows["codex"]["actions"]


def test_pi_signs_in_inside_its_own_prompt(monkeypatch):
    monkeypatch.setattr(harnesses, "program", lambda name: "/x/" + name)
    monkeypatch.setattr(harnesses, "auth_state", lambda name, **k: ("unknown", ""))
    monkeypatch.setattr(harnesses, "version_of", lambda name, **k: "")
    _ok, detail = agents.agents("bearer")
    rows = {r["name"]: r for r in detail["agents"]}
    assert "login" in rows["pi"]["actions"]            # its /login, typed into it
    assert "logout" not in rows["pi"]["actions"]
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


# --- signing out --------------------------------------------------------------

def test_signed_out_agents_are_not_offered_a_sign_out(monkeypatch):
    """Nothing to take away, and on pi and Hermes nothing anyone could see."""
    monkeypatch.setattr(harnesses, "program", lambda name: "/x/" + name)
    monkeypatch.setattr(harnesses, "auth_state",
                        lambda name, **k: (("out" if name == "claude" else "unknown"), ""))
    monkeypatch.setattr(harnesses, "version_of", lambda name, **k: "1.2.3")
    rows = {r["name"]: r for r in agents.agents("bearer")[1]["agents"]}
    assert "logout" not in rows["claude"]["actions"]
    assert "logout" not in rows["pi"]["actions"]


def test_sign_out_runs_the_command_and_re_reads_the_state(monkeypatch):
    ran = []

    class Done:
        returncode = 0
        stdout = "Signed out.\n"
        stderr = ""

    def fake_run(argv, **kw):
        ran.append(argv)
        return Done()

    monkeypatch.setattr(harnesses, "logout_argv", lambda name: ["/x/codex", "logout"])
    monkeypatch.setattr(harnesses, "auth_state", lambda name, **k: ("out", ""))
    monkeypatch.setattr("subprocess.run", fake_run)
    ok, detail = agents.sign_out("codex", "bearer")
    assert ok and ran == [["/x/codex", "logout"]]
    # No window: the answer is the command's own, with the state after it.
    assert detail["exit"] == 0 and detail["lines"] == ["Signed out."]
    assert detail["auth"] == "out" and "pane" not in detail


def test_sign_out_of_something_with_no_recipe_is_refused(monkeypatch):
    monkeypatch.setattr(harnesses, "logout_argv", lambda name: [])
    assert agents.sign_out("pi", "bearer")[1]["status"] == 409
    assert agents.sign_out("rm -rf", "bearer")[1]["status"] == 400


# --- is anything out of date --------------------------------------------------

def _no_cache():
    agents._LATEST.clear()


def test_updates_compares_the_registry_with_what_is_here(monkeypatch):
    _no_cache()
    monkeypatch.setattr(harnesses, "installed", lambda name: name in ("claude", "codex"))
    monkeypatch.setattr(harnesses, "version_of",
                        lambda name, **k: "2.0.0 (x)" if name == "claude" else "codex-cli 0.155.1")
    monkeypatch.setattr(harnesses, "latest_of",
                        lambda name, **k: "2.0.0" if name == "claude" else "0.156.0")
    ok, detail = agents.updates("bearer")
    rows = {r["name"]: r for r in detail["updates"]}
    assert ok and set(rows) == {"claude", "codex"}
    assert rows["claude"]["behind"] is False
    assert rows["codex"]["behind"] is True and rows["codex"]["latest"] == "0.156.0"


def test_a_lookup_that_says_nothing_is_not_up_to_date(monkeypatch):
    """"Could not ask" must never render as "current": that hides an update."""
    _no_cache()
    monkeypatch.setattr(harnesses, "installed", lambda name: name == "codex")
    monkeypatch.setattr(harnesses, "version_of", lambda name, **k: "codex-cli 0.155.1")
    monkeypatch.setattr(harnesses, "latest_of", lambda name, **k: "")
    rows = agents.updates("bearer")[1]["updates"]
    assert rows[0]["behind"] is None


def test_the_one_that_is_not_a_package_is_asked_about_itself(monkeypatch):
    _no_cache()
    asked = []
    monkeypatch.setattr(harnesses, "installed", lambda name: name == "hermes")
    monkeypatch.setattr(harnesses, "version_of", lambda name, **k: "Hermes 0.17.0")
    monkeypatch.setattr(harnesses, "latest_of",
                        lambda name, **k: pytest.fail("hermes is not on npm"))
    monkeypatch.setattr(harnesses, "update_check",
                        lambda name, **k: asked.append(name) or (True, "Update available."))
    rows = agents.updates("bearer")[1]["updates"]
    assert asked == ["hermes"]
    assert rows[0]["behind"] is True and rows[0]["latest"] == ""
    assert "Update available" in rows[0]["line"]


def test_the_network_is_asked_once_an_hour_unless_told_otherwise(monkeypatch):
    _no_cache()
    calls = []
    monkeypatch.setattr(harnesses, "installed", lambda name: name == "codex")
    monkeypatch.setattr(harnesses, "version_of", lambda name, **k: "codex-cli 0.155.1")
    monkeypatch.setattr(harnesses, "latest_of",
                        lambda name, **k: calls.append(name) or "0.156.0")
    agents.updates("bearer")
    agents.updates("bearer")
    assert calls == ["codex"]
    agents.updates("bearer", refresh=True)
    assert calls == ["codex", "codex"]


# --- this machine's wiring (roadmap item 3) -------------------------------------

def test_wiring_is_media_setup_status(monkeypatch, tmp_path):
    exe = tmp_path / "media-setup"
    exe.write_text("#!/bin/sh\necho '{\"rows\": [{\"name\": \"mail\", \"state\": \"missing\"}]}'\n")
    exe.chmod(0o755)
    monkeypatch.setattr(agents, "_media_setup", lambda: str(exe))
    ok, detail = agents.wiring("bearer")
    assert ok and detail["rows"] == [{"name": "mail", "state": "missing"}]


def test_wiring_says_when_media_setup_is_not_here(monkeypatch):
    monkeypatch.setattr(agents, "_media_setup", lambda: "")
    ok, detail = agents.wiring("bearer")
    assert not ok and detail["status"] == 409


def test_wire_runs_the_profile_in_a_window_it_can_watch(monkeypatch):
    seen = []
    _tmux(monkeypatch, record=seen)
    monkeypatch.setattr(agents, "_media_setup", lambda: "/venv/bin/media-setup")
    ok, detail = agents.wire("mail", "bearer")
    assert ok and detail["pane"] == "%7"
    assert detail["cmd"] == "/venv/bin/media-setup profile --only mail"
    assert "media-setup profile --only mail" in seen[0][-1]
    # The window is one of ours, so the page can read it like an install's.
    assert agents._row("%7")["agent"] == "setup"
    ok, detail = agents.wire("", "bearer")
    assert ok and detail["cmd"] == "/venv/bin/media-setup profile"


def test_wire_refuses_a_row_that_is_not_a_name(monkeypatch):
    monkeypatch.setattr(agents, "_media_setup", lambda: "/venv/bin/media-setup")
    ok, detail = agents.wire("mail; rm -rf ~", "bearer")
    assert not ok and detail["status"] == 400


def test_setup_is_gated(monkeypatch):
    monkeypatch.setattr(auth_abs, "may_control_speech",
                        lambda bearer: (False, {"error": "not allowed", "status": 403}))
    assert agents.wiring("")[0] is False
    assert agents.wire("mail", "")[0] is False


# --- pi: a sign-in that exists only inside its prompt ----------------------------

def test_pi_login_starts_pi_and_types_login_once_it_is_up(monkeypatch):
    from agent_media_core import harnesses as core
    monkeypatch.setattr(core, "program", lambda name: "/x/" + name)
    sent = []
    screens = iter(["", "pi v0.87 · / commands · ! bash"])

    def fake(argv, timeout=10):
        sent.append(argv)
        if argv[0] == "new-window":
            return "%7"
        if argv[0] == "capture-pane":
            return next(screens, "pi v0.87 · / commands")
        if argv[0] == "display":
            return "%7"
        return ""
    monkeypatch.setattr(panes, "_tmux", fake)
    started = []
    monkeypatch.setattr("threading.Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: started.append((target, args))})())
    ok, detail = agents.run("pi", "login", "bearer")
    assert ok and detail["cmd"] == "/x/pi --no-session"
    target, args = started[0]
    target(*args, wait=2, step=0.01)
    typed = [a for a in sent if a[0] == "send-keys"]
    assert typed == [["send-keys", "-t", "%7", "-l", "/login"], ["send-keys", "-t", "%7", "Enter"]]


def test_pi_auth_reads_the_check(monkeypatch):
    from agent_media_core import harnesses as core
    assert core._pi_auth('{"status":"ready","provider":"anthropic","authType":"api_key"}', "anthropic") == ("in", "anthropic (API key)")
    assert core._pi_auth('{"status":"not_ready","reason":"provider_not_found"}', "meridian")[0] == "unknown"
    assert core._pi_auth('{"status":"not_ready","reason":"no_credentials"}', "openai") == ("out", "openai: no credentials")
    assert core._pi_auth("not json", "x") == ("unknown", "")


# --- harness profiles: another login for an agent -----------------------------

@pytest.fixture()
def profiles(tmp_path, monkeypatch):
    from agent_media_core import harness_profiles as hp
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    hp._CACHE = (-1.0, [])
    return hp


def test_a_profile_is_its_own_row_with_its_own_sign_in(monkeypatch, profiles):
    monkeypatch.setattr(harnesses, "program", lambda name: "/x/" + name)
    monkeypatch.setattr(harnesses, "version_of", lambda name, **k: "1.0")
    seen = []

    def auth(name, env=None, **k):
        seen.append((name, dict(env or {})))
        return ("in", "work@example") if env else ("out", "")
    monkeypatch.setattr(harnesses, "auth_state", auth)
    ok, detail = agents.add_profile("codex", "work", "", "bearer")
    assert ok and detail["dir"].endswith("harness-profiles/codex-work")
    _ok, detail = agents.agents("bearer")
    codex = [r for r in detail["agents"] if r["name"] == "codex"]
    assert [r["profile"] for r in codex] == ["", "work"]
    assert codex[1]["auth"] == "in" and codex[1]["account"] == "work@example"
    assert "install" in codex[0]["actions"] and "install" not in codex[1]["actions"]
    assert ("codex", {"CODEX_HOME": profiles.get("codex", "work").dir}) in seen, "asked in its own directory"
    hermes = [r for r in detail["agents"] if r["name"] == "hermes"]
    assert hermes[0]["profiles"] is False, "Hermes keeps its own profiles"


def test_sign_in_runs_in_the_profiles_directory(monkeypatch, profiles):
    monkeypatch.setattr(harnesses, "program", lambda name: "/x/" + name)
    seen = []
    _tmux(monkeypatch, record=seen)
    agents.add_profile("codex", "work", "", "bearer")
    ok, detail = agents.run("codex", "login", "bearer", profile="work")
    assert ok
    assert detail["cmd"].startswith("env CODEX_HOME=") and detail["cmd"].endswith("/x/codex login --device-auth")
    ok, detail = agents.run("codex", "login", "bearer", profile="nope")
    assert not ok and detail["status"] == 404


def test_profiles_can_be_removed_and_hermes_cannot_have_one(monkeypatch, profiles):
    assert agents.add_profile("hermes", "x", "", "bearer")[1]["status"] == 400
    agents.add_profile("pi", "home", "", "bearer")
    ok, detail = agents.remove_profile("pi", "home", True, "bearer")
    assert ok and detail["deleted"]
    assert agents.remove_profile("pi", "home", False, "bearer")[1]["status"] == 404


def test_a_chat_in_a_profile_opens_with_its_directory(monkeypatch, profiles):
    from agent_media_server import send
    p = profiles.create("codex", "work")
    seen = []
    _tmux(monkeypatch, record=seen)
    monkeypatch.setattr(harnesses, "program", lambda name: "/x/" + name)
    monkeypatch.setattr(send, "pane_ready", lambda pane, agent: True)
    pane, err = send.open_window("", "/tmp", resume=False, host="scratch", agent="codex", profile="work")
    assert not err
    cmd = seen[-1][-1]
    assert f"CODEX_HOME={p.dir}" in cmd and "/x/codex" in cmd
    # Resumed: the profile it was found in.
    monkeypatch.setattr(harnesses, "profile_of", lambda session: "work")
    monkeypatch.setattr(harnesses, "running", lambda: [])
    import agent_media_core.claude_sessions as cs
    monkeypatch.setattr(cs, "running", lambda: [])
    send.open_window("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "/tmp", resume=True, host="scratch", agent="codex")
    assert f"CODEX_HOME={p.dir}" in seen[-1][-1]
