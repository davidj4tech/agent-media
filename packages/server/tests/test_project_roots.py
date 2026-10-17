"""Project roots: the folders a new project may go in (project_roots.py, §6.24).

All fakes, as test_layout_desk.py: no tmux, no panes, a tmp HOME.
"""

import os

import pytest

from agent_media_server import auth, auth_abs, presets, project_roots, send, sessions


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_LAYOUT", "default")
    h = tmp_path / "home"
    (h / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("MEDIA_PROJECTS_DIR", raising=False)
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200)
                        if b == "tok" else (None, 401))
    return h.resolve()


@pytest.fixture
def asker(monkeypatch):
    opened = []
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": None)
    monkeypatch.setattr(send, "_agent_unready", lambda agent: "")
    monkeypatch.setattr(send, "_settle", lambda p, timeout=5.0: None)
    monkeypatch.setattr(send, "_ensure_submitted", lambda p, t, timeout=3.0, agent="claude": True)
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    monkeypatch.setattr(sessions, "session_of_pane", lambda p, timeout=10.0, agent="claude": "")
    monkeypatch.setattr(send, "open_window",
                        lambda s, cwd, *, resume, host="", flags=(), agent="claude":
                        opened.append(cwd) or ("%9", ""))
    return opened


def test_the_default_is_projects_dir_else_home_projects(home, monkeypatch):
    assert project_roots.listing() == [
        {"path": str(home / "projects"), "label": "~/projects", "default": True}]
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(home / "code"))
    assert project_roots.listing()[0]["label"] == "~/code"
    assert not project_roots.store_path().exists()


def test_roots_are_added_made_default_and_removed(home):
    (home / "work").mkdir()
    ok, d = project_roots.add("~/work", "tok")
    assert ok and [r["label"] for r in d["roots"]] == ["~/projects", "~/work"]
    assert [r["default"] for r in d["roots"]] == [True, False]
    ok, d = project_roots.make_default(str(home / "work"), "tok")
    assert ok and d["roots"][0] == {"path": str(home / "work"), "label": "~/work", "default": True}
    ok, d = project_roots.remove("~/projects", "tok")
    assert ok and [r["label"] for r in d["roots"]] == ["~/work"]
    ok, d = project_roots.remove("~/work", "tok")
    assert not ok and d["status"] == 409
    assert oct(os.stat(project_roots.store_path()).st_mode & 0o777) == "0o600"


def test_a_root_must_be_an_existing_folder_under_home_and_new(home, tmp_path):
    (home / "file").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside)
    (home / "link").symlink_to(home / "projects")
    for bad in ("", "~/nope", "~/file", str(outside), "~/escape", "/etc", "~/../outside"):
        ok, d = project_roots.add(bad, "tok")
        assert not ok and d["status"] == 400, bad
    for dup in ("~/projects", "~/link", str(home / "projects") + "/"):
        ok, d = project_roots.add(dup, "tok")
        assert not ok and d["status"] == 409, dup
    ok, d = project_roots.remove("~/elsewhere", "tok")
    assert not ok and d["status"] == 404
    ok, d = project_roots.make_default("/tmp", "tok")
    assert not ok and d["status"] == 404


def test_the_routes_are_gated_through_may_admin(home, monkeypatch):
    (home / "work").mkdir()
    for call in (project_roots.add, project_roots.remove, project_roots.make_default):
        ok, d = call("~/work", "bad")
        assert not ok and d["status"] == 401
    asked = []
    monkeypatch.setattr(auth, "may_admin", lambda b: asked.append(b) or (None, {"status": 403}))
    ok, d = project_roots.add("~/work", "tok")
    assert not ok and d["status"] == 403 and asked == ["tok"]
    assert project_roots.get("tok")[0] and not project_roots.get("bad")[0]


def test_presets_lists_the_roots(home):
    ok, d = presets.listing("tok")
    assert ok and d["project_roots"] == project_roots.listing()


def test_a_new_project_goes_in_the_root_named_or_the_first(home, asker):
    (home / "work").mkdir()
    assert project_roots.add("~/work", "tok")[0]
    ok, d = send.ask("hi", "tok", new_project="a")
    assert ok and d["cwd"] == str(home / "projects" / "a") and asker == [d["cwd"]]
    ok, d = send.ask("hi", "tok", new_project="b", root=str(home / "work"))
    assert ok and d["cwd"] == str(home / "work" / "b")
    ok, d = send.ask("hi", "tok", new_project="c", root="~/work")
    assert ok and d["cwd"] == str(home / "work" / "c")
    assert project_roots.make_default("~/work", "tok")[0]
    ok, d = send.ask("hi", "tok", new_project="d")
    assert ok and d["cwd"] == str(home / "work" / "d")


def test_a_root_not_listed_is_refused_before_a_folder_is_made(home, asker, tmp_path):
    (home / "other").mkdir()
    for bad in (str(home / "other"), "/tmp", str(tmp_path)):
        ok, d = send.ask("hi", "tok", new_project="x", root=bad)
        assert not ok and d["status"] == 400, bad
    assert not (home / "other" / "x").exists() and asker == []


def test_ask_routed_passes_the_root(home, asker):
    from agent_media_server import routing

    (home / "work").mkdir()
    assert project_roots.add("~/work", "tok")[0]
    ok, d = routing.ask_routed("hi", "tok", new_project="r", root=str(home / "work"))
    assert ok and d["cwd"] == str(home / "work" / "r")
