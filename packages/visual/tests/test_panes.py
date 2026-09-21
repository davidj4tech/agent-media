"""Reaching a session in the pane it runs in — tmux's, or herdr's."""

from agent_media_visual import panes


def _record(monkeypatch):
    """Capture every command the module would run, answering each with "ok"."""
    calls = []

    def fake_run(argv, timeout=10):
        calls.append(argv)
        return "ok"

    monkeypatch.setattr(panes, "_run", fake_run)
    return calls


# --- addresses ----------------------------------------------------------------

def test_a_tmux_pane_keeps_its_bare_id():
    assert not panes.is_herdr("%562")
    assert panes.herdr_pane("%562") == "%562"


def test_a_herdr_pane_carries_its_multiplexer():
    assert panes.is_herdr("herdr:w6:p1")
    assert panes.herdr_pane("herdr:w6:p1") == "w6:p1"


def test_a_process_is_addressed_by_the_pane_it_inherited():
    assert panes.addr_of_env({b"TMUX_PANE": b"%562"}) == "%562"
    assert panes.addr_of_env({b"HERDR_PANE_ID": b"w6:p1"}) == "herdr:w6:p1"
    assert panes.addr_of_env({b"HOME": b"/home/ryer"}) == ""


def test_tmux_wins_when_a_process_carries_both():
    # A tmux client running inside a herdr pane: the inner multiplexer is the
    # one whose send-keys reaches the agent.
    env = {b"TMUX_PANE": b"%562", b"HERDR_PANE_ID": b"w6:p1"}
    assert panes.addr_of_env(env) == "%562"


# --- the three verbs ----------------------------------------------------------

def test_capture_asks_the_right_multiplexer(monkeypatch):
    calls = _record(monkeypatch)
    panes.capture("%562", lines=40)
    panes.capture("herdr:w6:p1", lines=40)
    assert calls[0][:2] == ["tmux", "capture-pane"] and "-e" in calls[0]
    assert calls[1][:3] == ["herdr", "pane", "read"]
    assert calls[1][3] == "w6:p1" and "ansi" in calls[1]


def test_capture_can_ask_for_plain_text(monkeypatch):
    calls = _record(monkeypatch)
    panes.capture("%562", ansi=False)
    panes.capture("herdr:w6:p1", ansi=False)
    assert "-e" not in calls[0]
    assert "text" in calls[1]


def test_an_empty_address_captures_nothing(monkeypatch):
    calls = _record(monkeypatch)
    assert panes.capture("") == ""
    assert not panes.alive("")
    assert calls == []


def test_send_types_then_presses_enter(monkeypatch):
    _record(monkeypatch)   # alive() says yes
    ran = []
    monkeypatch.setattr(panes.subprocess, "run",
                        lambda argv, **kw: ran.append(argv))
    monkeypatch.setattr(panes.time, "sleep", lambda _s: None)

    assert panes.send("herdr:w6:p1", "hello") == ""
    assert ran[0] == ["herdr", "pane", "send-text", "w6:p1", "hello"]
    assert ran[1] == ["herdr", "pane", "send-keys", "w6:p1", "enter"]

    ran.clear()
    assert panes.send("%562", "hello") == ""
    assert ran[0] == ["tmux", "send-keys", "-t", "%562", "-l", "hello"]
    assert ran[1] == ["tmux", "send-keys", "-t", "%562", "Enter"]


def test_nothing_is_typed_into_a_pane_that_is_gone(monkeypatch):
    monkeypatch.setattr(panes, "_run", lambda argv, timeout=10: "")
    typed = []
    monkeypatch.setattr(panes.subprocess, "run",
                        lambda argv, **kw: typed.append(argv))
    assert "gone" in panes.send("herdr:w6:p1", "hello")
    assert typed == []


def test_liveness_asks_each_multiplexer_its_own_way(monkeypatch):
    calls = _record(monkeypatch)
    assert panes.alive("%562")
    assert panes.alive("herdr:w6:p1")
    assert calls[0][:2] == ["tmux", "display-message"]
    assert calls[1] == ["herdr", "pane", "get", "w6:p1"]


# --- what the pane is called --------------------------------------------------

def test_a_herdr_pane_label_comes_out_of_its_json(monkeypatch):
    monkeypatch.setattr(panes, "_run", lambda argv, timeout=10: (
        '{"result": {"pane": {"pane_id": "w6:p1", "label": "Drones"}}}'))
    assert panes.label("herdr:w6:p1") == "Drones"


def test_an_unnamed_pane_is_called_what_the_agent_called_it(monkeypatch):
    # Claude Code writes the conversation title into the terminal title, and
    # that is the name the picker should show.
    monkeypatch.setattr(panes, "_run", lambda argv, timeout=10: (
        '{"result": {"pane": {"terminal_title": "\u2733 Drones",'
        ' "terminal_title_stripped": "Drones"}}}'))
    assert panes.label("herdr:w6:p1") == "Drones"


def test_a_herdr_pane_knows_its_own_directory(monkeypatch):
    monkeypatch.setattr(panes, "_run", lambda argv, timeout=10: (
        '{"result": {"pane": {"cwd": "/home/ryer", '
        '"foreground_cwd": "/home/ryer/projects/agent-media"}}}'))
    assert panes.cwd("herdr:w6:p1") == "/home/ryer/projects/agent-media"


def test_herdrs_own_agent_detection_is_the_answer(monkeypatch):
    calls = []

    def fake_run(argv, timeout=10):
        calls.append(argv)
        return '{"result": {"pane": {"agent": "claude"}}}'

    monkeypatch.setattr(panes, "_run", fake_run)
    assert panes.process_name("herdr:w6:p1") == "claude"
    # and it did not go walking processes to find out
    assert all("process-info" not in a for a in calls)


def test_unreadable_json_is_no_label(monkeypatch):
    monkeypatch.setattr(panes, "_run", lambda argv, timeout=10: "not json")
    assert panes.label("herdr:w6:p1") == ""


def test_the_agent_is_the_deepest_foreground_process(monkeypatch):
    # herdr said nothing about an agent, so the processes are walked instead.
    monkeypatch.setattr(panes, "_run", lambda argv, timeout=10: (
        '{"result": {"process_info": {"foreground_processes": ['
        '{"name": "zsh"}, {"name": "claude"}]}}}'))
    assert panes.process_name("herdr:w6:p1") == "claude"


def test_a_bare_shell_is_reported_as_itself(monkeypatch):
    monkeypatch.setattr(panes, "_run", lambda argv, timeout=10: (
        '{"result": {"process_info": {"foreground_processes": ['
        '{"name": "zsh"}]}}}'))
    assert panes.process_name("herdr:w6:p1") == "zsh"


def test_where_names_the_grouping_each_multiplexer_uses(monkeypatch):
    monkeypatch.setattr(panes, "_run", lambda argv, timeout=10: "p-agent-media")
    assert panes.where("%562") == {"source": "tmux", "session": "p-agent-media"}
    assert panes.where("herdr:w6:p1") == {"source": "herdr", "session": "w6"}


# --- hermes: an agent whose pane does not say its name ------------------------

def test_a_hermes_pane_is_recognised_by_the_process_in_it(monkeypatch):
    from agent_media_visual import canvas
    from agent_media_core import harnesses

    monkeypatch.setattr(harnesses, "_argv", lambda pid: [
        "/h/.hermes/hermes-agent/venv/bin/python3",
        "/h/.hermes/hermes-agent/venv/bin/hermes", "--tui"])
    assert canvas._agent_by_argv("123") == "hermes"
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["/usr/bin/zsh"])
    assert canvas._agent_by_argv("123") == ""
    assert canvas._agent_by_argv("") == ""


def test_hermes_is_working_while_it_says_so():
    from agent_media_visual import canvas

    ready = " ─ ready │ opus 4.8 │ 30.5k t ─\n meridian ❯ Ask me anything…"
    busy = " ─ ヽ(>∀<☆)☆ formulating…   · 2s\n meridian ❯ Ctrl+C to interrupt…"
    assert canvas._classify_agent(ready, "hermes") == "input"
    assert canvas._classify_agent(busy, "hermes") == "working"
    # Not painted yet is not "waiting for you".
    assert canvas._classify_agent("", "hermes") is None
