"""The layout setting: detection, precedence, and what each layout answers.

Everything here is files in tmp_path: HOME, CC_HOME, CLAUDE_CONFIG_DIR and
MEDIA_CONFIG all point into it, so nothing reads the real desk.
"""

import argparse
import json

import pytest

from agent_media_core import layout, session_feed, setup


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """An empty host: no amux, no hooks, no config. MEDIA_LAYOUT unpinned."""
    monkeypatch.delenv("MEDIA_LAYOUT", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CC_HOME", str(home / ".amux"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("MEDIA_CONFIG", str(home / "config.toml"))
    for k in ("MEDIA_ASK_SESSION", "MEDIA_ASK_TMUX", "MEDIA_ASK_CWD", "MEDIA_ASK_FLAGS",
              "MEDIA_REPLY_TMUX"):
        monkeypatch.delenv(k, raising=False)
    return home


def _amux(home, body='CC_DIR="/home/d/scratch"\nCC_FLAGS="--yolo"\n'):
    d = home / ".amux" / "sessions"
    d.mkdir(parents=True)
    (d / "scratch.env").write_text(body)


def _move_hook(home):
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command",
                    "command": '[ -z "${TMUX_PANE:-}" ] || ~/.local/bin/tmux-organise-panes --pane "$TMUX_PANE"'}]}]}}))


# --- detection ---------------------------------------------------------------


def test_a_fresh_host_is_default(desk):
    name, why = layout.detect()
    assert name == layout.DEFAULT and "no amux" in why
    assert layout.current().source == "detected"


def test_amux_and_the_move_hook_together_are_davids(desk):
    _amux(desk)
    _move_hook(desk)
    name, why = layout.detect()
    assert name == layout.PROJECTS and "tmux-organise-panes" in why


def test_the_tmux_resume_hook_counts_as_the_move_hook(desk):
    _amux(desk)
    (desk / ".config" / "tmux").mkdir(parents=True)
    (desk / ".config" / "tmux" / "tmux.conf.local").write_text(
        "set-hook -g after-new-session 'run-shell ~/.local/bin/tmux-claude-resume'\n")
    assert layout.detect()[0] == layout.PROJECTS


def test_either_mark_alone_is_a_coincidence(desk):
    _amux(desk)
    assert layout.detect()[0] == layout.DEFAULT
    (desk / ".amux" / "sessions" / "scratch.env").unlink()
    (desk / ".amux" / "sessions").rmdir()
    _move_hook(desk)
    assert layout.detect()[0] == layout.DEFAULT


def test_a_broken_settings_file_reads_as_no_hook(desk):
    _amux(desk)
    (desk / ".claude").mkdir()
    (desk / ".claude" / "settings.json").write_text("{not json")
    assert layout.detect()[0] == layout.DEFAULT


# --- precedence --------------------------------------------------------------


def test_config_beats_detection_and_env_beats_config(desk, monkeypatch):
    _amux(desk)
    _move_hook(desk)
    (desk / "config.toml").write_text('layout = "default"\n\n[host]\nroles = ["render"]\n')
    now = layout.current()
    assert (now.name, now.source) == (layout.DEFAULT, "config")
    monkeypatch.setenv("MEDIA_LAYOUT", "projects-per-tmux-session")
    now = layout.current()
    assert (now.name, now.source) == (layout.PROJECTS, "env")


def test_a_nonsense_value_falls_through(desk, monkeypatch):
    (desk / "config.toml").write_text('layout = "tiles"\n')
    monkeypatch.setenv("MEDIA_LAYOUT", "bogus")
    assert layout.current().source == "detected"


def test_a_malformed_config_is_detection(desk):
    (desk / "config.toml").write_text("layout = \n[[[")
    assert layout.current().source == "detected"


# --- the answers -------------------------------------------------------------


def test_davids_fresh_target_is_the_amux_registration(desk, monkeypatch):
    monkeypatch.setenv("MEDIA_LAYOUT", layout.PROJECTS)
    _amux(desk)
    assert layout.fresh_target() == ("amux-scratch", "/home/d/scratch", "--yolo")
    assert layout.uses_amux() and layout.expects_move_hook()
    assert layout.revive_host() == ""
    assert layout.place_host("/x/projects/runlet") == "runlet"
    assert layout.project_host("p-runlet") == "p-runlet"
    assert layout.workspace_for("p-runlet", "/x/projects/runlet") == "p-runlet"


def test_default_fresh_target_ignores_amux(desk, monkeypatch):
    monkeypatch.setenv("MEDIA_LAYOUT", layout.DEFAULT)
    _amux(desk)          # there, but not consulted
    assert layout.fresh_target() == ("sasonica", str(desk), "")
    assert not layout.uses_amux() and not layout.expects_move_hook()
    assert layout.revive_host() == "sasonica"
    assert layout.place_host("/x/projects/runlet") == "sasonica"
    assert layout.project_host("runlet") == "sasonica"
    assert layout.holds_client()


def test_default_overrides_still_apply(desk, monkeypatch):
    monkeypatch.setenv("MEDIA_LAYOUT", layout.DEFAULT)
    monkeypatch.setenv("MEDIA_ASK_TMUX", "chats")
    monkeypatch.setenv("MEDIA_ASK_CWD", "/srv/work")
    monkeypatch.setenv("MEDIA_REPLY_TMUX", "back")
    assert layout.fresh_target() == ("chats", "/srv/work", "")
    assert layout.place_host("/a/b") == "chats"
    assert layout.revive_host() == "back"


def test_default_workspace_is_the_folder(desk, monkeypatch):
    monkeypatch.setenv("MEDIA_LAYOUT", layout.DEFAULT)
    assert layout.workspace_for("sasonica", "/x/projects/runlet") == "runlet"
    assert layout.workspace_for("sasonica", str(desk)) == "sasonica"      # home names nobody
    assert layout.workspace_for("my-own", "/x/projects/runlet") == "my-own"
    assert layout.project_of_path("/x/projects/runlet/") == "runlet"


def test_encoded_label_is_p_dash_only_on_davids_desk(desk, monkeypatch):
    monkeypatch.setenv("MEDIA_LAYOUT", layout.PROJECTS)
    assert layout.encoded_project_label("agent-media") == "p-agent-media"
    monkeypatch.setenv("MEDIA_LAYOUT", layout.DEFAULT)
    assert layout.encoded_project_label("agent-media") == "agent-media"
    assert layout.encoded_project_label(" ") == ""


def test_default_files_a_sasonica_chat_under_its_folder(desk, monkeypatch):
    from agent_media_core import harnesses

    monkeypatch.setenv("MEDIA_LAYOUT", layout.DEFAULT)
    monkeypatch.setattr(harnesses, "cwd_of", lambda s: "/x/projects/runlet")
    ts = [session_feed.Turn(at=1.0, text="hi", workspace="sasonica")]
    assert session_feed.workspace_for("s-1", ts) == "runlet"
    monkeypatch.setenv("MEDIA_LAYOUT", layout.PROJECTS)
    assert session_feed.workspace_for("s-1", ts) == "sasonica"


# --- writing it --------------------------------------------------------------


def test_write_setting_goes_above_the_first_table(desk):
    p = desk / "config.toml"
    p.write_text('# header\n\n[host]\nroles = ["render"]\n')
    assert layout.write_setting("default", p) is True
    from agent_media_core import config
    data = config.load(p)
    assert data["layout"] == "default" and data["host"]["roles"] == ["render"]
    # Never over one already there, unless asked.
    assert layout.write_setting("projects-per-tmux-session", p) is False
    assert config.load(p)["layout"] == "default"
    assert layout.write_setting("projects-per-tmux-session", p, replace=True) is True
    assert config.load(p)["layout"] == layout.PROJECTS
    with pytest.raises(ValueError):
        layout.write_setting("tiles", p)


def test_write_setting_creates_the_file(desk):
    p = desk / "new" / "config.toml"
    assert layout.write_setting("default", p)
    assert layout.current(p).name == "default"


def test_init_writes_the_detected_layout_on_a_fresh_host(desk):
    p = desk / "config.toml"
    args = argparse.Namespace(config=str(p), roles="render", force=False, dry_run=False)
    assert setup.cmd_init(args) == 0
    now = layout.current(p)
    assert (now.name, now.source) == (layout.DEFAULT, "config")


def test_init_writes_davids_layout_where_it_is_detected(desk):
    _amux(desk)
    _move_hook(desk)
    p = desk / "config.toml"
    args = argparse.Namespace(config=str(p), roles="render", force=False, dry_run=False)
    assert setup.cmd_init(args) == 0
    assert layout.current(p).name == layout.PROJECTS


def test_init_adds_the_layout_to_an_existing_config_and_nothing_else(desk):
    p = desk / "config.toml"
    p.write_text('[host]\nroles = ["origin"]\n')
    args = argparse.Namespace(config=str(p), roles=None, force=False, dry_run=False,
                              layout="default")
    assert setup.cmd_init(args) == 0
    text = p.read_text()
    assert 'roles = ["origin"]' in text and layout.current(p).name == "default"


def test_media_setup_layout_reports_and_sets(desk, capsys):
    p = desk / "config.toml"
    assert setup.cmd_layout(argparse.Namespace(config=str(p), set=None)) == 0
    out = capsys.readouterr().out
    assert "layout: default" in out and "detected" in out
    assert setup.cmd_layout(argparse.Namespace(config=str(p), set="projects-per-tmux-session")) == 0
    out = capsys.readouterr().out
    assert "layout: projects-per-tmux-session" in out and "detection would say default" in out


def test_selfcheck_reports_the_layout(desk, monkeypatch):
    from agent_media_core import cli

    monkeypatch.setenv("MEDIA_LAYOUT", layout.DEFAULT)
    facts = cli._layout_facts()
    assert facts == {"layout": "default", "layout_why": "env: MEDIA_LAYOUT=default"}
