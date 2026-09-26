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


def test_only_skills_and_our_own_commands_are_offered(monkeypatch):
    monkeypatch.setattr(slash_menu, "ask_claude", lambda cwd, timeout=60.0: (
        ["model", "resume", "speak", "deploy", "__secret", "mcp__x__y", "anthropic-skills:pdf"],
        ["speak", "anthropic-skills:pdf"], "2.1.1"))
    monkeypatch.setattr(slash_menu, "descriptions", lambda cwd=None: {"deploy": "Ship it"})
    monkeypatch.setattr(slash_menu, "bundle_commands", lambda names: {})
    names = [c["name"] for c in slash_menu.build("/proj")]
    # A skill, a plugin's skill, and a command of this project's own.
    assert names == ["anthropic-skills:pdf", "deploy", "speak"]
    # Claude Code's own: nothing the phone cannot see or cannot drive.
    assert "model" not in names and "resume" not in names
    assert "__secret" not in names and "mcp__x__y" not in names


def test_a_skills_own_description_is_what_it_shows(monkeypatch):
    monkeypatch.setattr(slash_menu, "ask_claude",
                        lambda cwd, timeout=60.0: (["speak"], ["speak"], "2.1.1"))
    monkeypatch.setattr(slash_menu, "descriptions", lambda cwd=None: {"speak": "Out loud"})
    monkeypatch.setattr(slash_menu, "groups", lambda cwd=None: {"speak": "Yours"})
    monkeypatch.setattr(slash_menu, "bundle_commands", lambda names: {})
    assert slash_menu.build("/proj") == [
        {"name": "speak", "description": "Out loud", "aliases": [], "group": "Yours"}]


def test_the_sheet_groups_by_where_a_command_lives(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(slash_menu.Path, "home", staticmethod(lambda: home))
    _skill(home, "speak", "Say it")
    _skill(tmp_path / "proj", "deploy", "Ship it")
    # A skill pack linked in from elsewhere: grouped by the pack's name.
    pack = tmp_path / "share" / "cloudflare-skills" / "skills" / "wrangler"
    pack.mkdir(parents=True)
    (pack / "SKILL.md").write_text("---\ndescription: Deploy\n---\n")
    (home / ".claude" / "skills" / "wrangler").symlink_to(pack)
    found = slash_menu.groups(tmp_path / "proj")
    assert found == {"speak": "Yours", "wrangler": "Cloudflare", "deploy": "This project"}
    # No file: a plugin's prefix, else Claude Code's own.
    assert slash_menu.group_of("anthropic-skills:pdf", found) == "Anthropic"
    assert slash_menu.group_of("code-review", found) == "Claude Code"


def test_an_older_cache_is_rebuilt_for_the_groups(monkeypatch):
    calls = []
    monkeypatch.setattr(slash_menu, "build", lambda cwd: calls.append(cwd) or [{"name": "a"}])
    monkeypatch.setattr(slash_menu, "claude_version", lambda: "2.1.1")
    path = slash_menu._cache_path("/proj")
    path.parent.mkdir(parents=True, exist_ok=True)
    import json, time
    path.write_text(json.dumps({"at": time.time(), "version": "2.1.1", "commands": [{"name": "old"}]}))
    assert slash_menu.menu("/proj") == [{"name": "a"}] and calls == ["/proj"]


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
                          "skills": ["speak"], "claude_code_version": "2.1.9"}),
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
    assert slash_menu.ask_claude("/proj") == (["model"], ["speak"], "2.1.9")
    assert stopped == [True]                       # never gets as far as a reply


def test_a_plugin_skill_takes_its_words_from_the_bundle(monkeypatch):
    """A plugin's skill has no file on disk here, but the bundle describes it."""
    monkeypatch.setattr(slash_menu, "ask_claude", lambda cwd, timeout=60.0: (
        ["anthropic-skills:pdf"], ["anthropic-skills:pdf"], "2.1.1"))
    monkeypatch.setattr(slash_menu, "descriptions", lambda cwd=None: {})
    monkeypatch.setattr(slash_menu, "bundle_commands", lambda names: {
        "anthropic-skills:pdf": {"description": "Work with PDF files", "aliases": []}})
    assert slash_menu.build("/proj")[0]["description"] == "Work with PDF files"


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


def test_claude_is_found_where_a_service_has_no_path(tmp_path, monkeypatch):
    monkeypatch.delenv("MEDIA_CLAUDE_BIN", raising=False)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    guess = tmp_path / "bin" / "claude"
    guess.parent.mkdir(parents=True)
    guess.write_text("#!/bin/sh\n")
    monkeypatch.setattr(slash_menu, "_CLAUDE_GUESSES", (str(guess),))
    assert slash_menu.claude_bin() == str(guess)


def test_a_named_claude_that_is_not_there_is_not_used(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_CLAUDE_BIN", str(tmp_path / "nope"))
    assert slash_menu.claude_bin() == ""
