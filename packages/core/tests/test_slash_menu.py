"""The slash menu: what the reply box offers when a message starts with `/`."""

import json

import pytest

from agent_media_core import slash_menu


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(slash_menu, "state_dir", lambda: tmp_path / "state" / "agent-media")
    return tmp_path


def _skill(root, name, description):
    p = root / ".claude" / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n")


def test_a_skills_own_words_describe_it(tmp_path, monkeypatch):
    monkeypatch.setattr(slash_menu.Path, "home", staticmethod(lambda: tmp_path / "home"))
    _skill(tmp_path / "home", "speak", "Say something out loud")
    _skill(tmp_path / "proj", "deploy", "Ship it")
    assert slash_menu.descriptions(tmp_path / "proj") == {
        "speak": "Say something out loud", "deploy": "Ship it"}


def test_the_project_wins_a_name_the_user_also_has(tmp_path, monkeypatch):
    monkeypatch.setattr(slash_menu.Path, "home", staticmethod(lambda: tmp_path / "home"))
    _skill(tmp_path / "home", "run", "The user's run")
    _skill(tmp_path / "proj", "run", "The project's run")
    assert slash_menu.descriptions(tmp_path / "proj")["run"] == "The project's run"


def test_the_menu_is_what_claude_code_says_plus_the_terminal_only_ones(monkeypatch):
    monkeypatch.setattr(slash_menu, "ask_claude",
                        lambda cwd, timeout=60.0: (["model", "speak", "__secret",
                                                    "mcp__x__y", "resume"], "2.1.1"))
    monkeypatch.setattr(slash_menu, "descriptions", lambda cwd=None: {"speak": "Out loud"})
    monkeypatch.setattr(slash_menu, "bundle_commands", lambda names: {})
    menu = slash_menu.build("/proj")
    names = [c["name"] for c in menu]
    assert "__secret" not in names and "mcp__x__y" not in names   # internal
    assert names == sorted(names)
    by_name = {c["name"]: c for c in menu}
    assert by_name["speak"]["description"] == "Out loud"
    assert by_name["model"]["description"] == slash_menu.BUILTIN_DESCRIPTIONS["model"]
    assert by_name["resume"]["terminal"] is False   # claude named it, so not ours
    assert by_name["help"]["terminal"] is True      # only the terminal has it


def test_the_menu_is_cached_until_the_version_changes(monkeypatch):
    calls = []
    monkeypatch.setattr(slash_menu, "build", lambda cwd: calls.append(cwd) or [{"name": "a"}])
    monkeypatch.setattr(slash_menu, "claude_version", lambda: "2.1.1")
    assert slash_menu.menu("/proj") == [{"name": "a"}]
    assert slash_menu.menu("/proj") == [{"name": "a"}]
    assert len(calls) == 1
    monkeypatch.setattr(slash_menu, "claude_version", lambda: "2.1.2")
    slash_menu.menu("/proj")
    assert len(calls) == 2


def test_a_menu_that_cannot_be_built_is_not_cached(monkeypatch):
    monkeypatch.setattr(slash_menu, "build", lambda cwd: [])
    monkeypatch.setattr(slash_menu, "claude_version", lambda: "2.1.1")
    assert slash_menu.menu("/proj") == []
    assert not (slash_menu.state_dir() / "slash-menu").exists()


def test_the_startup_event_is_read_and_the_run_stopped(monkeypatch):
    events = [json.dumps({"type": "hook", "subtype": "hook_started"}),
              "not json\n",
              json.dumps({"subtype": "init", "slash_commands": ["model"],
                          "claude_code_version": "2.1.9"}),
              json.dumps({"type": "assistant"})]
    stopped = []

    class _Proc:
        def __init__(self):
            self.stdout = iter(events)
            self.stdout = type("S", (), {"readline": lambda _s: next(iter_lines, "")})()
        def terminate(self):
            stopped.append(True)
        def wait(self, timeout=None):
            return 0

    iter_lines = iter(e if e.endswith("\n") else e + "\n" for e in events)
    monkeypatch.setattr(slash_menu.subprocess, "Popen", lambda *a, **k: _Proc())
    assert slash_menu.ask_claude("/proj") == (["model"], "2.1.9")
    assert stopped == [True]                       # never gets as far as a reply


def test_the_bundles_own_words_and_aliases_beat_the_kept_ones(monkeypatch):
    monkeypatch.setattr(slash_menu, "ask_claude", lambda cwd, timeout=60.0: (["model"], "2.1.1"))
    monkeypatch.setattr(slash_menu, "descriptions", lambda cwd=None: {})
    monkeypatch.setattr(slash_menu, "bundle_commands", lambda names: {
        "model": {"description": "Set the AI model for Claude Code", "aliases": []},
        "exit": {"description": "", "aliases": ["quit"]}})
    by_name = {c["name"]: c for c in slash_menu.build("/proj")}
    assert by_name["model"]["description"] == "Set the AI model for Claude Code"
    # /exit builds its description at runtime, so the kept line still shows —
    # but the alias is real and comes through.
    assert by_name["exit"]["aliases"] == ["quit"]
    assert by_name["exit"]["description"] == dict(slash_menu.TERMINAL_ONLY)["exit"]


def test_a_command_record_is_read_whichever_way_round_it_is(tmp_path, monkeypatch):
    bundle = tmp_path / "claude.exe"
    bundle.write_bytes(
        b'x={type:"local",name:"config",aliases:["settings"],description:"Open settings"};'
        b'y={aliases:["quit"],name:"exit",immediate:!0};'
        b'z={name:"OCaml",aliases:["ml"]};')
    monkeypatch.setattr(slash_menu, "_bundle_path", lambda: bundle)
    found = slash_menu.bundle_commands({"config", "exit"})
    assert found["config"] == {"description": "Open settings", "aliases": ["settings"]}
    assert found["exit"]["aliases"] == ["quit"]
    assert "OCaml" not in found          # only names the init event vouched for


def test_no_bundle_is_no_trouble(monkeypatch):
    monkeypatch.setattr(slash_menu, "_bundle_path", lambda: None)
    assert slash_menu.bundle_commands({"exit"}) == {}
