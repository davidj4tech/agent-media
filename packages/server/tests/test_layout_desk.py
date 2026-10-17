"""Where app chats open under each layout (agent_media_core/layout.py).

The rest of the suite runs pinned to David's layout (conftest.py); this file
flips to `default` and checks the decisions send.py and sessions.py make from
it. All fakes: no tmux, no amux, no panes.
"""

import json
import os

import pytest

from agent_media_server import auth_abs, driver, new_project, panes, presets, send, sessions


@pytest.fixture
def default_desk(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_LAYOUT", "default")
    # An amux registration that must NOT be consulted.
    d = tmp_path / "amux" / "sessions"
    d.mkdir(parents=True)
    (d / "scratch.env").write_text('CC_DIR="/home/d/scratch"\nCC_FLAGS="--yolo"\n')
    monkeypatch.setenv("CC_HOME", str(tmp_path / "amux"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


@pytest.fixture
def asker(monkeypatch):
    opened = []
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": None)
    # The harness is asked whether it could answer at all; here it always
    # could (test_ask_refuses_a_signed_out_agent covers the check itself).
    monkeypatch.setattr(send, "_agent_unready", lambda agent: "")
    monkeypatch.setattr(send, "_settle", lambda p, timeout=5.0: None)
    monkeypatch.setattr(send, "_ensure_submitted", lambda p, t, timeout=3.0, agent="claude": True)
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(sessions, "session_of_pane", lambda p, timeout=10.0, agent="claude": "")
    monkeypatch.setattr(send, "open_window",
                        lambda s, cwd, *, resume, host="", flags=(), agent="claude":
                        opened.append((cwd, host, list(flags))) or ("%9", ""))
    return opened


@pytest.mark.skipif(os.name == "nt", reason="the desk's tmux layout: not on Windows, whose chats are headless")
def test_default_ask_target_is_home_in_sasonica_without_amux(default_desk):
    assert send.ask_target() == ("sasonica", str(default_desk / "home"), [])


@pytest.mark.skipif(os.name == "nt", reason="the desk's tmux layout: not on Windows, whose chats are headless")
def test_default_fresh_chat_opens_a_window_in_sasonica(default_desk, asker):
    ok, detail = send.ask("hi", "tok")
    assert ok and detail["tmux"] == "sasonica"
    assert asker == [(str(default_desk / "home"), "sasonica", [])]


def test_default_chat_in_a_place_opens_in_sasonica_at_that_folder(default_desk, asker, monkeypatch):
    place = default_desk / "projects" / "runlet"
    place.mkdir(parents=True)
    monkeypatch.setattr(sessions, "places", lambda limit=6: [
        {"name": "runlet", "path": str(place), "at": 1.0}])
    ok, detail = send.ask("hi", "tok", cwd=str(place))
    assert ok and asker == [(str(place), "sasonica", [])]


def test_default_project_is_the_folder_basename(default_desk, asker, monkeypatch):
    place = default_desk / "projects" / "runlet"
    place.mkdir(parents=True)
    monkeypatch.setattr(sessions, "places", lambda limit=6: [
        {"name": "runlet", "path": str(place), "at": 1.0}])
    assert sessions.project_target("runlet") == ("sasonica", str(place))
    assert sessions.project_target("p-runlet") == ("", "")
    ok, detail = send.ask("hi", "tok", project="runlet")
    assert ok and asker == [(str(place), "sasonica", [])]


def test_davids_project_is_still_the_series(tmp_path, monkeypatch):
    # The same world read in David's layout: a series named for its tmux session.
    monkeypatch.setenv("MEDIA_LAYOUT", "projects-per-tmux-session")
    d = tmp_path / "book-tracks"
    d.mkdir()
    (d / "s1.json").write_text(json.dumps({"session": "s1", "folder": "/c/p-runlet/Thing"}))
    monkeypatch.setattr(sessions, "_manifest_dir", lambda: d)
    here = tmp_path / "runlet"
    here.mkdir()
    monkeypatch.setattr(sessions, "transcript_cwd", lambda s: str(here))
    assert sessions.project_target("p-runlet") == ("p-runlet", str(here))
    assert sessions.project_target("runlet") == ("", "")


def test_default_revived_window_goes_to_a_held_sasonica(default_desk, monkeypatch):
    held, calls = [], []

    def fake_tmux(argv, timeout=10):
        calls.append(argv)
        return "%4" if argv[0] == "new-window" else ""

    monkeypatch.setattr(panes, "_tmux", fake_tmux)
    monkeypatch.setattr(send, "ensure_host", lambda h, c: held.append((h, c)) or True)
    monkeypatch.setattr(send, "pane_ready", lambda p, agent="claude": True)
    monkeypatch.setattr(send, "_claude_bin", lambda name="claude": "claude")
    monkeypatch.setattr(send, "attached_session", lambda: pytest.fail("default holds its own"))
    pane, err = send.open_window("", "/x", resume=False)
    assert (pane, err) == ("%4", "")
    assert held == [("sasonica", "/x")]
    nw = next(c for c in calls if c[0] == "new-window")
    assert nw[nw.index("-t") + 1] == "sasonica:"


def test_davids_revived_window_goes_to_the_attached_session(monkeypatch):
    monkeypatch.setenv("MEDIA_LAYOUT", "projects-per-tmux-session")
    calls = []

    def fake_tmux(argv, timeout=10):
        calls.append(argv)
        return "%4" if argv[0] == "new-window" else ""

    monkeypatch.setattr(panes, "_tmux", fake_tmux)
    monkeypatch.setattr(send, "ensure_host", lambda h, c: pytest.fail("nothing to hold"))
    monkeypatch.setattr(send, "pane_ready", lambda p, agent="claude": True)
    monkeypatch.setattr(send, "_claude_bin", lambda name="claude": "claude")
    monkeypatch.setattr(send, "attached_session", lambda: "p-agent-media")
    assert send.open_window("", "/x", resume=False) == ("%4", "")
    nw = next(c for c in calls if c[0] == "new-window")
    assert nw[nw.index("-t") + 1] == "p-agent-media:"


class _Headless:
    kind = driver.HEADLESS
    agents = ("claude",)

    def __init__(self):
        self.started = []

    def start(self, **k):
        self.started.append(k)
        return True, {"session": "h-1"}


def test_default_headless_chat_is_filed_under_its_folder(default_desk, monkeypatch):
    hl = _Headless()
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(driver, "for_new", lambda agent="claude": hl)
    place = default_desk / "projects" / "runlet"
    place.mkdir(parents=True)
    monkeypatch.setattr(sessions, "places", lambda limit=6: [
        {"name": "runlet", "path": str(place), "at": 1.0}])
    assert send.ask("hi", "tok", cwd=str(place))[0]
    assert hl.started[-1]["host"] == "runlet" and hl.started[-1]["cwd"] == str(place)
    assert send.ask("hi", "tok")[0]
    assert hl.started[-1]["host"] == "sasonica"          # home names nobody


def test_davids_headless_chat_keeps_the_tmux_name(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LAYOUT", "projects-per-tmux-session")
    monkeypatch.setenv("CC_HOME", str(tmp_path / "none"))
    hl = _Headless()
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(driver, "for_new", lambda agent="claude": hl)
    assert send.ask("hi", "tok")[0]
    assert hl.started[-1]["host"] == "amux-scratch"


@pytest.mark.skipif(os.name == "nt", reason="the desk's tmux layout: not on Windows, whose chats are headless")
def test_davids_project_from_claude_history_when_nothing_is_shelved(tmp_path, monkeypatch):
    # The shelf is empty since the Conversations library came out: Claude's
    # own transcripts name the places, and a project's own directory beats a
    # worktree inside it.
    monkeypatch.setenv("MEDIA_LAYOUT", "projects-per-tmux-session")
    d = tmp_path / "book-tracks"
    d.mkdir()
    monkeypatch.setattr(sessions, "_manifest_dir", lambda: d)
    home = tmp_path / "home"
    proj = home / "projects" / "runlet"
    tree = proj / ".claude" / "worktrees" / "spike"
    tree.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "claude-projects"
    cwds = {"a": str(proj), "b": str(tree)}
    for i, sid in enumerate(cwds):
        (root / sid).mkdir(parents=True)
        f = root / sid / f"{sid}.jsonl"
        f.write_text("{}\n")
        os.utime(f, (100 + i, 100 + i))
    from agent_media_server import moves
    monkeypatch.setattr(moves, "claude_root", lambda: root)
    monkeypatch.setattr(sessions, "transcript_cwd", lambda s: cwds.get(s, ""))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    assert sessions.project_target("p-runlet") == ("p-runlet", str(proj))
    # The worktree sits under a hidden directory: out of the list, until a
    # session is live there.
    assert [p["path"] for p in sessions.places()] == [str(proj)]
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"b": "%1"})
    assert [p["path"] for p in sessions.places()] == [str(tree), str(proj)]


def test_a_new_project_is_made_initialised_and_chatted_in(default_desk, asker, monkeypatch):
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(default_desk / "projects"))
    (default_desk / "projects").mkdir(exist_ok=True)
    ok, detail = send.ask("hi", "tok", new_project="drones")
    made = default_desk / "projects" / "drones"
    assert ok and asker == [(str(made), "sasonica", [])]
    assert (made / ".git").is_dir() or not __import__("shutil").which("git")


def test_a_new_project_refuses_a_name_already_there_or_not_a_name(default_desk, asker, monkeypatch):
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(default_desk / "projects"))
    (default_desk / "projects").mkdir(exist_ok=True)
    (default_desk / "projects" / "runlet").mkdir(parents=True)
    ok, detail = send.ask("hi", "tok", new_project="runlet")
    assert not ok and detail["status"] == 409
    for bad in ("../etc", "a/b", ".hidden", " ", "x" * 65, "repo.git"):
        ok, detail = send.ask("hi", "tok", new_project=bad)
        assert not ok and detail["status"] == 400, bad
    assert asker == [] and sorted(p.name for p in (default_desk / "projects").iterdir()) == ["runlet"]


def test_a_new_project_is_not_made_for_someone_signed_out(default_desk, asker, monkeypatch):
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(default_desk / "projects"))
    (default_desk / "projects").mkdir(exist_ok=True)
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: (None, 401))
    ok, _ = send.ask("hi", "bad", new_project="drones")
    assert not ok and not (default_desk / "projects" / "drones").exists()


needs_git = pytest.mark.skipif(not __import__("shutil").which("git"), reason="no git")


def _git(*args):
    import subprocess

    return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


@needs_git
def test_a_new_project_clones_a_repo_and_keeps_its_agents_md(default_desk, asker, monkeypatch):
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(default_desk / "projects"))
    (default_desk / "projects").mkdir(exist_ok=True)
    src = default_desk / "src"
    src.mkdir()
    _git("init", "-q", str(src))
    (src / "AGENTS.md").write_text("theirs\n")
    _git("-C", str(src), "add", ".")
    _git("-C", str(src), "commit", "-qm", "x")
    # A local path is not a URL a clone may be given; a fake ssh one gets through
    # the check, so the clone itself is redirected to the local repo.
    ok, detail = send.ask("hi", "tok", new_project="c", repo=str(src))
    assert not ok and detail["status"] == 400
    real = new_project._git
    monkeypatch.setattr(new_project, "_git", lambda *a, timeout=20: real(
        *[str(src) if x == "git@example.com:me/c.git" else x for x in a], timeout=timeout))
    ok, detail = send.ask("hi", "tok", new_project="c", repo="git@example.com:me/c.git", agents_md=True)
    made = default_desk / "projects" / "c"
    assert ok and detail["cwd"] == str(made)
    assert (made / "AGENTS.md").read_text() == "theirs\n"
    assert os.readlink(made / "CLAUDE.md") == "AGENTS.md"


def test_a_failed_clone_leaves_no_folder(default_desk, asker, monkeypatch):
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(default_desk / "projects"))
    (default_desk / "projects").mkdir(exist_ok=True)
    def fail(*a, timeout=20):
        (default_desk / "projects" / "c").mkdir()
        return False, "fatal: repository not found"
    monkeypatch.setattr(new_project, "_git", fail)
    ok, detail = send.ask("hi", "tok", new_project="c", repo="https://example.com/c.git")
    assert not ok and detail["status"] == 502 and "not found" in detail["error"]
    assert not (default_desk / "projects" / "c").exists() and asker == []


@needs_git
def test_a_new_project_gets_its_presets_agents_md(default_desk, asker, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(default_desk / "projects"))
    (default_desk / "projects").mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert presets.put({"name": "Web app", "agent": "claude", "agents_md": "# {name}: web\n"}, "tok")[0]
    ok, _ = send.ask("hi", "tok", new_project="shop", agents_md=True, preset="nope")
    assert not ok and not (default_desk / "projects" / "shop").exists()
    ok, _ = send.ask("hi", "tok", new_project="shop", agents_md=True, preset="Web app")
    assert ok and (default_desk / "projects" / "shop" / "AGENTS.md").read_text() == "# shop: web\n"


def test_a_new_project_can_have_its_first_chat_draft_agents_md(default_desk, asker, monkeypatch):
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(default_desk / "projects"))
    (default_desk / "projects").mkdir(exist_ok=True)
    typed = []
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: typed.append(t) or "")
    ok, _ = send.ask("", "tok", new_project="empty")
    assert not ok and not (default_desk / "projects" / "empty").exists()
    ok, _ = send.ask("", "tok", new_project="drones", agents_md="draft")
    made = default_desk / "projects" / "drones"
    assert ok and typed == [new_project.DRAFT_PROMPT]
    assert not (made / "AGENTS.md").exists() and os.readlink(made / "CLAUDE.md") == "AGENTS.md"
    ok, _ = send.ask("then add a README", "tok", new_project="rotors", agents_md="draft")
    assert ok and typed[-1] == f"{new_project.DRAFT_PROMPT} then add a README"


def test_presets_are_added_replaced_and_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    assert presets.put({"name": "A", "agent": "codex", "prompt": "go"}, "tok")[0]
    assert presets.put({"name": "A", "agent": "pi", "plan": True}, "tok")[0]
    assert presets.put({"name": "B"}, "tok")[0]
    assert not presets.put({"name": "C", "agent": "emacs"}, "tok")[0]
    assert not presets.put({"name": ""}, "tok")[0]
    ok, d = presets.listing("tok")
    assert [(p["name"], p["agent"], p["plan"]) for p in d["presets"]] == [("A", "pi", True), ("B", "", False)]
    assert "{name}" in d["agents_md_default"]
    assert presets.remove("A", "tok")[0] and not presets.remove("A", "tok")[0]


@needs_git
def test_a_worktree_is_cut_inside_its_project_and_chatted_in(default_desk, asker, monkeypatch):
    place = default_desk / "projects" / "runlet"
    place.mkdir(parents=True)
    _git("init", "-q", str(place))
    (place / "f").write_text("x")
    _git("-C", str(place), "add", ".")
    _git("-C", str(place), "commit", "-qm", "x")
    monkeypatch.setattr(sessions, "places", lambda limit=6: [
        {"name": "runlet", "path": str(place), "at": 1.0}])
    ok, detail = send.ask("hi", "tok", worktree={"from": str(default_desk), "branch": "x"})
    assert not ok and detail["status"] == 404
    ok, detail = send.ask("hi", "tok", worktree={"from": str(place), "branch": "bad..name"})
    assert not ok and detail["status"] == 400
    ok, detail = send.ask("hi", "tok", worktree={"from": str(place), "branch": "fix-x"})
    tree = place / ".worktrees" / "fix-x"
    assert ok and detail["cwd"] == str(tree) and detail["branch"] == "fix-x"
    assert (tree / "f").exists() and asker == [(str(tree), "sasonica", [])]
    assert "/.worktrees/" in (place / ".git" / "info" / "exclude").read_text()
    assert _git("-C", str(place), "status", "--porcelain") == ""
    ok, detail = send.ask("hi", "tok", worktree={"from": str(place), "branch": "fix-x"})
    assert not ok and detail["status"] == 409


def test_new_project_and_worktree_are_one_each(default_desk, asker):
    ok, detail = send.ask("hi", "tok", new_project="a", worktree={"from": "/x", "branch": "b"})
    assert not ok and detail["status"] == 400
