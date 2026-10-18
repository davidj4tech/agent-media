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
                                          "default": True, "missing": True, "why": "gone"}


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
    (home / "elsewhere").mkdir()
    (home / "inside").symlink_to(home / "elsewhere")
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
    assert ok and d["made"] == ["~/elsewhere/sub"] and (home / "elsewhere" / "sub").is_dir()
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
    (home / "code" / "w").mkdir(parents=True)
    link.symlink_to(home / "code")
    assert project_roots.add("~/link/w", "tok")[0]   # stored resolved: ~/code/w
    (home / "code" / "w").rmdir()
    (home / "code").rmdir()
    (home / "code").symlink_to(outside)               # the parent now leads out of home
    ok, d = project_roots.recreate(str(home / "code" / "w"), "tok")
    assert not ok and d["status"] == 400 and not (outside / "w").exists()


# --- the edge cases (David, 18 Oct 2026: "let's manage the edge cases first") ---

import errno
import json
import shutil
import subprocess
import threading
import time
import unicodedata


def _fake_dev(monkeypatch, paths, shift=1):
    """`_ident` answers another device for these folders (a drive not mounted)."""
    real = project_roots._ident
    want = {str(p) for p in paths}

    def fake(path):
        got = real(path)
        return (got[0] + shift, got[1]) if got and str(path) in want else got
    monkeypatch.setattr(project_roots, "_ident", fake)


def test_1_a_root_on_another_device_is_unmounted_and_refused(home, asker, monkeypatch):
    work = home / "work"
    work.mkdir()
    assert project_roots.add("~/work", "tok")[0]
    rows = json.loads(project_roots.store_path().read_text())
    assert all(isinstance(r["dev"], int) and isinstance(r["ino"], int) for r in rows)
    _fake_dev(monkeypatch, [work])
    assert project_roots.listing()[1] == {"path": str(work), "label": "~/work", "default": False,
                                          "missing": True, "why": "unmounted"}
    ok, d = send.ask("hi", "tok", new_project="x", root="~/work")
    assert not ok and d["status"] == 409 and d["why"] == "unmounted" and asker == []
    assert not (work / "x").exists()
    ok, d = project_roots.recreate("~/work", "tok")
    assert not ok and d["status"] == 409 and d["why"] == "unmounted"
    assert project_roots.remove("~/work", "tok")[0]


def test_1_recreate_refuses_when_the_folder_above_is_on_another_device(home, monkeypatch):
    (home / "mnt" / "drive").mkdir(parents=True)
    assert project_roots.add("~/mnt/drive", "tok")[0]
    (home / "mnt" / "drive").rmdir()
    _fake_dev(monkeypatch, [home / "mnt"])
    ok, d = project_roots.recreate("~/mnt/drive", "tok")
    assert not ok and d["status"] == 409 and "wrong disk" in d["error"]
    assert not (home / "mnt" / "drive").exists()
    monkeypatch.undo()


def test_1_old_rows_get_their_device_on_first_read(home):
    (home / "work").mkdir()
    store = project_roots.store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps([str(home / "projects"), str(home / "work"), str(home / "gone")]))
    assert [r["missing"] for r in project_roots.listing()] == [False, False, True]
    rows = json.loads(store.read_text())
    assert [("dev" in r, "ino" in r) for r in rows] == [(True, True), (True, True), (False, False)]
    assert rows[1]["dev"] == os.stat(home / "work").st_dev


def test_2_a_root_inside_a_git_work_tree_or_another_root_is_refused(home):
    git = shutil.which("git")
    assert git
    (home / "code" / "repo" / "src").mkdir(parents=True)
    subprocess.run([git, "init", "-q", str(home / "code" / "repo")], check=True)
    for bad, says in (("~/code/repo", "is a git repository"), ("~/code/repo/src", "inside the git"),
                      ("~/code/repo/new", "inside the git")):
        for create in (False, True):
            ok, d = project_roots.add(bad, "tok", create=create)
            assert not ok and d["status"] == 400 and says in d["error"], (bad, create, d)
    assert not (home / "code" / "repo" / "new").exists()
    assert project_roots.add("~/code", "tok")[0]    # holding a repo is fine
    (home / "projects" / "sub").mkdir()
    (home / "w" / "x").mkdir(parents=True)
    assert project_roots.add("~/w/x", "tok")[0]
    for bad, says in (("~/projects/sub", "inside the project folder ~/projects"),
                      ("~/projects/new/deep", "inside the project folder ~/projects"),
                      ("~/w", "holds the project folder ~/w/x")):
        ok, d = project_roots.add(bad, "tok", create=True)
        assert not ok and d["status"] == 400 and says in d["error"], (bad, d)
    assert not (home / "projects" / "new").exists()


def test_3_hidden_folders_under_home_are_refused(home):
    (home / ".config").mkdir()
    (home / "work" / ".x").mkdir(parents=True)
    (home / "plain").symlink_to(home / ".config")
    for bad in ("~/.config", "~/.ssh/new", "~/work/.x", "~/work/.y/z", "~/plain"):
        for create in (False, True):
            ok, d = project_roots.add(bad, "tok", create=create)
            assert not ok and d["status"] == 400 and "hidden" in d["error"], (bad, d)
    assert not (home / ".ssh").exists() and not (home / "work" / ".y").exists()


@pytest.mark.parametrize("code,status", [(errno.ENOSPC, 507), (errno.EACCES, 403), (errno.EROFS, 409),
                                         (errno.ENAMETOOLONG, 400)])
@pytest.mark.parametrize("dir_fd", [True, False])
def test_4_a_failure_partway_removes_what_was_made(home, monkeypatch, code, status, dir_fd):
    if dir_fd and not project_roots._dir_fd_ok():
        pytest.skip("no dir_fd here")
    monkeypatch.setattr(project_roots, "DIR_FD", dir_fd)
    real, calls = os.mkdir, []

    def mkdir(path, *a, **kw):
        calls.append(path)
        if len(calls) == 3:
            raise OSError(code, os.strerror(code))
        return real(path, *a, **kw)
    monkeypatch.setattr(os, "mkdir", mkdir)
    ok, d = project_roots.add("~/a/b/c/d", "tok", create=True)
    assert not ok and d["status"] == status and "~/a/b/c" in d["error"]
    assert len(calls) == 3 and not (home / "a").exists()
    # A level that was there already is left alone.
    (home / "a").mkdir()
    calls.clear()
    ok, d = project_roots.add("~/a/b/c/d", "tok", create=True)
    assert not ok and (home / "a").is_dir() and not (home / "a" / "b").exists()


@pytest.mark.parametrize("dir_fd", [True, False])
def test_5_a_link_swapped_in_while_making_is_not_followed(home, tmp_path, monkeypatch, dir_fd):
    if dir_fd and not project_roots._dir_fd_ok():
        pytest.skip("no dir_fd here")
    monkeypatch.setattr(project_roots, "DIR_FD", dir_fd)
    outside = tmp_path / "outside"
    outside.mkdir()
    real = os.mkdir

    def mkdir(path, *a, **kw):
        real(path, *a, **kw)
        if os.path.basename(str(path)) == "a":     # someone swaps it for a link
            fd = kw.get("dir_fd")
            os.rmdir(path, dir_fd=fd) if fd is not None else os.rmdir(path)
            os.symlink(outside, path, dir_fd=fd) if fd is not None else os.symlink(outside, path)
    monkeypatch.setattr(os, "mkdir", mkdir)
    ok, d = project_roots.add("~/a/b", "tok", create=True)
    assert not ok and d["status"] == 409 and "changed while being made" in d["error"]
    assert not list(outside.iterdir())
    assert [r["path"] for r in project_roots.listing()] == [str(home / "projects")]


def test_6_one_folder_under_several_names_is_one_root(home, monkeypatch):
    (home / "work").mkdir()
    for dup in ("~/projects/", "~/projects//", "~/./projects", str(home / "projects") + "/",
                "projects", "~/work/../projects"):
        ok, d = project_roots.add(dup, "tok", create=True)
        assert not ok and d["status"] == 409, (dup, d)
    nfd = unicodedata.normalize("NFD", "café")
    (home / nfd).mkdir()
    assert project_roots.add("~/" + nfd, "tok")[0]
    ok, d = project_roots.add("~/" + unicodedata.normalize("NFC", "café"), "tok", create=True)
    assert not ok and d["status"] == 409
    assert project_roots.find("~/" + unicodedata.normalize("NFC", "café")) == str(home / nfd)
    # This disk keeps case; one that folds it makes ~/Projects the same.
    assert not project_roots._case_insensitive(str(home / "projects"))
    (home / "Projects").mkdir()
    monkeypatch.setattr(project_roots, "_case_insensitive", lambda p: True)
    ok, d = project_roots.add("~/Projects", "tok")
    assert not ok and d["status"] == 409
    monkeypatch.undo()
    assert project_roots._case_insensitive(str(home)) is False


def test_7_two_edits_at_once_both_land(home, monkeypatch):
    for n in ("a", "b", "c", "d"):
        (home / n).mkdir()
    real = project_roots._read

    def slow():
        rows = real()
        time.sleep(0.05)       # widen the read-modify-write window
        return rows
    monkeypatch.setattr(project_roots, "_read", slow)
    results = []
    threads = [threading.Thread(target=lambda n=n: results.append(project_roots.add(f"~/{n}", "tok")))
               for n in ("a", "b", "c", "d")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(ok for ok, _ in results)
    assert sorted(project_roots.load()[1:]) == [str(home / n) for n in ("a", "b", "c", "d")]
    # The same one twice at once: one added, one 409.
    (home / "e").mkdir()
    results.clear()
    threads = [threading.Thread(target=lambda: results.append(project_roots.add("~/e", "tok")))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(d.get("status", 200) for _, d in results) == [200, 409]
    assert project_roots.load().count(str(home / "e")) == 1


def test_8_reserved_names_trailing_dots_and_spaces_are_refused_everywhere(home):
    (home / "aux").mkdir()
    for bad in ("~/CON", "~/nul.txt", "~/work/com1", "~/LPT9.tar.gz", "~/aux", "~/a.", "~/b /c", "~/Prn "):
        for create in (False, True):
            ok, d = project_roots.add(bad, "tok", create=create)
            assert not ok and d["status"] == 400 and ("reserves" in d["error"] or "dot or a space" in d["error"]), (bad, d)
    for fine in ("~/console", "~/com10", "~/lpt", "~/a.b"):
        assert project_roots.add(fine, "tok", create=True)[0], fine
    assert not (home / "CON").exists() and not (home / "work").exists()


def test_8_a_junction_is_a_link(home, monkeypatch):
    class St:
        st_mode = 0o040755
        st_file_attributes = 0x400
    real = os.lstat
    def lstat(p, *a, **kw):
        st = real(p, *a, **kw)      # missing → OSError, as the real one
        return St() if str(p).endswith("/j") else st
    monkeypatch.setattr(os, "lstat", lstat)
    (home / "j").mkdir()
    assert project_roots._is_link(home / "j") and not project_roots._is_link(home / "projects")
    (home / "j").rmdir()
    # Without dir_fd (Windows), a level that turns out a junction is refused,
    # nothing made through it (the junction, no longer ours, is left).
    monkeypatch.setattr(project_roots, "DIR_FD", False)
    ok, d = project_roots.add("~/j/k", "tok", create=True)
    assert not ok and d["status"] == 409 and not (home / "j" / "k").exists()
    assert [r["path"] for r in project_roots.listing()] == [str(home / "projects")]
