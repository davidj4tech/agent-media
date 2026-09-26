"""Where app chats open under each layout (agent_media_core/layout.py).

The rest of the suite runs pinned to David's layout (conftest.py); this file
flips to `default` and checks the decisions send.py and sessions.py make from
it. All fakes: no tmux, no amux, no panes.
"""

import json
import os

import pytest

from agent_media_server import auth_abs, driver, panes, send, sessions


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


def test_default_ask_target_is_home_in_sasonica_without_amux(default_desk):
    assert send.ask_target() == ("sasonica", str(default_desk / "home"), [])


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
    assert [p["path"] for p in sessions.places()] == [str(tree), str(proj)]
