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
        {"path": str(home / "projects"), "label": "~/projects", "default": True, "missing": False}]
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(home / "code"))
    assert project_roots.listing()[0]["label"] == "~/code"
    assert not project_roots.store_path().exists()


def test_roots_are_added_made_default_and_removed(home):
    (home / "work").mkdir()
    ok, d = project_roots.add("~/work", "tok")
    assert ok and [r["label"] for r in d["roots"]] == ["~/projects", "~/work"]
    assert [r["default"] for r in d["roots"]] == [True, False]
    ok, d = project_roots.make_default(str(home / "work"), "tok")
    assert ok and d["roots"][0] == {"path": str(home / "work"), "label": "~/work", "default": True, "missing": False}
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
    for bad in ("", "~/file", str(outside), "~/escape", "/etc", "~/../outside"):
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


def test_a_root_whose_folder_is_gone_is_listed_missing_until_it_returns(home):
    work = home / "work"
    work.mkdir()
    assert project_roots.add("~/work", "tok")[0]
    assert [r["missing"] for r in project_roots.listing()] == [False, False]
    work.rmdir()
    ok, d = project_roots.get("tok")
    assert ok and [(r["label"], r["missing"]) for r in d["roots"]] == [
        ("~/projects", False), ("~/work", True)]
    assert presets.listing("tok")[1]["project_roots"][1]["missing"] is True
    (work).write_text("not a folder")
    assert project_roots.listing()[1]["missing"] is True
    work.unlink()
    work.mkdir()
    assert project_roots.listing()[1]["missing"] is False


def test_a_new_project_is_refused_in_a_missing_root_and_the_root_not_remade(home, asker):
    work = home / "work"
    work.mkdir()
    assert project_roots.add("~/work", "tok")[0]
    work.rmdir()
    ok, d = send.ask("hi", "tok", new_project="x", root="~/work")
    assert not ok and d["status"] == 409 and d["error"] == "that folder is gone"
    assert not work.exists() and asker == []
    # The default missing: refused too, no falling back to another root.
    assert project_roots.make_default(str(work), "tok")[0]
    ok, d = send.ask("hi", "tok", new_project="x")
    assert not ok and d["status"] == 409
    assert not work.exists() and not (home / "projects" / "x").exists() and asker == []
    # The present one, named, still works; the default stays the stored one.
    ok, d = send.ask("hi", "tok", new_project="x", root="~/projects")
    assert ok and d["cwd"] == str(home / "projects" / "x")
    assert project_roots.listing()[0] == {"path": str(work), "label": "~/work",
                                          "default": True, "missing": True}


def test_the_built_in_default_is_not_made_when_missing(home, asker):
    (home / "projects").rmdir()
    assert project_roots.listing()[0]["missing"] is True
    ok, d = send.ask("hi", "tok", new_project="x")
    assert not ok and d["status"] == 409 and not (home / "projects").exists()
    from agent_media_server import new_project

    ok, d = new_project.create("x")
    assert not ok and d["status"] == 409 and not (home / "projects").exists()


def test_a_missing_root_can_still_be_removed(home):
    work = home / "work"
    work.mkdir()
    assert project_roots.add("~/work", "tok")[0]
    work.rmdir()
    ok, d = project_roots.remove("~/work", "tok")
    assert ok and [r["label"] for r in d["roots"]] == ["~/projects"]


def test_a_folder_not_there_answers_would_create_and_is_made_only_on_create(home):
    ok, d = project_roots.add("~/work/clients", "tok")
    assert not ok and d["status"] == 404 and d["would_create"] == ["~/work", "~/work/clients"]
    assert not (home / "work").exists()
    ok, d = project_roots.add("~/work/clients", "tok", create=True)
    assert ok and d["made"] == ["~/work", "~/work/clients"]
    assert (home / "work" / "clients").is_dir()
    assert [r["label"] for r in d["roots"]] == ["~/projects", "~/work/clients"]
    # mkdir's own mode: 0777 less the umask.
    umask = os.umask(0)
    os.umask(umask)
    assert os.stat(home / "work").st_mode & 0o777 == 0o777 & ~umask
    # Already there: added as before, nothing made; listed already: 409 either way.
    (home / "code").mkdir()
    ok, d = project_roots.add("~/code", "tok", create=True)
    assert ok and d["made"] == []
    (home / "work" / "clients").rmdir()
    ok, d = project_roots.add("~/work/clients", "tok")
    assert not ok and d["status"] == 409 and "would_create" not in d


def test_create_refuses_a_link_out_of_home_a_file_in_the_way_and_too_deep(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside)
    (home / "file").write_text("x")
    (home / "broken").symlink_to(tmp_path / "nowhere")
    (home / "inside").symlink_to(home / "projects")
    for bad in ("~/escape/new", "~/escape/a/b", "~/file/new", "~/broken/new", str(tmp_path / "elsewhere"),
                "~/nope/../../x", "~/a/b/c/d/e", "~/.local/state/agent-media/x",
                "~/.local/state/agent-media"):
        for create in (False, True):
            ok, d = project_roots.add(bad, "tok", create=create)
            assert not ok and d["status"] == 400 and "would_create" not in d, (bad, create)
    assert not list(outside.iterdir()) and not (home / "a").exists() and not (home / "nope").exists()
    assert not (home / ".local").exists()
    # A link inside home is followed: the folder is made where it points.
    ok, d = project_roots.add("~/inside/sub", "tok", create=True)
    assert ok and d["made"] == ["~/projects/sub"] and (home / "projects" / "sub").is_dir()
    ok, d = project_roots.add("~/a/b/c/d", "tok", create=True)
    assert ok and d["made"] == ["~/a", "~/a/b", "~/a/b/c", "~/a/b/c/d"]


def test_create_is_gated_through_may_admin(home, monkeypatch):
    monkeypatch.setattr(auth, "may_admin", lambda b: (None, {"status": 403}))
    for create in (False, True):
        ok, d = project_roots.add("~/work", "tok", create=create)
        assert not ok and d["status"] == 403
    ok, d = project_roots.recreate("~/projects", "tok")
    assert not ok and d["status"] == 403
    assert not (home / "work").exists()


def test_a_missing_root_is_recreated_parents_included(home):
    (home / "work" / "clients").mkdir(parents=True)
    assert project_roots.add("~/work/clients", "tok")[0]
    (home / "work" / "clients").rmdir()
    (home / "work").rmdir()
    assert project_roots.listing()[1]["missing"] is True
    ok, d = project_roots.recreate("~/work/clients", "tok")
    assert ok and d["made"] == ["~/work", "~/work/clients"] and d["roots"][1]["missing"] is False
    ok, d = project_roots.recreate("~/work/clients", "tok")
    assert ok and d["made"] == []
    ok, d = project_roots.recreate("~/elsewhere", "tok")
    assert not ok and d["status"] == 404 and not (home / "elsewhere").exists()
    # Its place taken by a file: refused, the file left alone.
    (home / "work" / "clients").rmdir()
    (home / "work" / "clients").write_text("x")
    ok, d = project_roots.recreate("~/work/clients", "tok")
    assert not ok and d["status"] == 400 and (home / "work" / "clients").read_text() == "x"


def test_recreate_judges_the_stored_path_as_create_does(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / "link"
    link.symlink_to(home / "projects")
    (home / "projects" / "w").mkdir()
    assert project_roots.add("~/link/w", "tok")[0]   # stored resolved: ~/projects/w
    (home / "projects" / "w").rmdir()
    (home / "projects").rmdir()
    (home / "projects").symlink_to(outside)           # the parent now leads out of home
    ok, d = project_roots.recreate(str(home / "projects" / "w"), "tok")
    assert not ok and d["status"] == 400 and not (outside / "w").exists()
