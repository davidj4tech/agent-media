"""Notes: the Org tree browsed, searched and captured into over HTTP, with no
Emacs anywhere (notes.py). Each test builds its own tree; the memory store is
stubbed so nothing reaches David's real one."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_server import app, auth, notes

INBOX = """\
#+title: Inbox

* HIGH URGENCY
** TODO Fix the TV ssh
   SCHEDULED: <2026-09-20 Sun 08:45>
   Some body text about the telly.
** DONE Old thing
** NEXT [#A] Call the bank :phone:
   DEADLINE: <2026-09-24 Thu>
* Later
** WAITING Parcel
"""

@pytest.fixture(autouse=True)
def _paragtd(monkeypatch):
    """These trees are laid out the paragtd way, and the tests were written
    against its views and refile targets (the notes-paragtd package; plain
    Org is test_notes_plain.py)."""
    monkeypatch.setenv("MEDIA_NOTES_PROFILE", "paragtd")


#: What the stubbed store was asked to remember, this test.
REMEMBERED: list[dict] = []


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    root = tmp_path / "org"
    (root / "roam" / "projects").mkdir(parents=True)
    (root / "roam" / "sessions" / "inbox").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "config.org").write_text("secret")
    (root / "inbox.org").write_text(INBOX)
    (root / "astro.org").write_text(
        "* Full moon\n  SCHEDULED: <2026-09-01 Tue>\n* New moon\n  SCHEDULED: <2026-09-23 Wed>\n")
    (root / "lunar.org").write_text(
        "* Full moon routine\n<2026-09-01 Tue>\n* New moon routine\n<2026-09-23 Wed>\n")
    (root / "roam" / "projects" / "yoga.org").write_text(
        ":PROPERTIES:\n:ID:       yoga-id\n:END:\n#+title: agent-yoga\n\nsee [[id:bank-id][bank]]\n")
    (root / "roam" / "projects" / "bank.org").write_text(
        ":PROPERTIES:\n:ID: bank-id\n:END:\n#+title: Bank\n\ntelly money\n")
    (root / "roam" / "sessions" / "inbox" / "s1.org").write_text("#+title: s1\ntelly session\n")
    monkeypatch.setenv("MEDIA_NOTES_DIR", str(root))
    monkeypatch.setattr(auth, "gate", lambda bearer: (
        ({"username": "david"}, {}) if bearer == "good"
        else (None, {"error": "not allowed", "status": 401})))
    remembered = REMEMBERED
    remembered.clear()
    monkeypatch.setattr(notes, "_memory_call", lambda m, p, body=None, timeout=6.0: (
        remembered.append(body) or {} if m == "POST"
        else {"memories": [{"id": "m1", "text": "telly memory", "score": 0.8}]}))
    monkeypatch.setattr(notes, "_IDS", {"at": 0.0, "map": {}})
    return root


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True)
    t.start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _call(addr, method, path, body=None, bearer="good"):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    res = conn.getresponse()
    data = json.loads(res.read() or b"{}")
    conn.close()
    return res.status, data


def test_views_list_the_files_and_folders(tree, server):
    status, got = _call(server, "GET", "/notes")
    assert status == 200
    names = {v["name"]: v for v in got["views"]}
    assert names["inbox"]["count"] == 3          # TODO, NEXT, WAITING — not DONE
    assert names["roam-projects"]["count"] == 2
    assert "agenda" in names and "next" not in names   # no next-actions.org here


def test_everything_is_gated(tree, server):
    for path in ("/notes", "/notes/view?name=inbox", "/notes/read?path=inbox.org",
                 "/notes/search?q=telly"):
        assert _call(server, "GET", path, bearer="bad")[0] == 401
    assert _call(server, "POST", "/notes/capture", {"text": "x"}, bearer="bad")[0] == 401
    assert "HIGH" in (tree / "inbox.org").read_text() and "* TODO x" not in (
        tree / "inbox.org").read_text()


def test_a_file_view_reads_states_and_dates(tree, server):
    _, got = _call(server, "GET", "/notes/view?name=inbox")
    by_title = {h["title"]: h for h in got["items"]}
    assert "Old thing" not in by_title
    assert by_title["Fix the TV ssh"]["scheduled"] == "2026-09-20"
    bank = by_title["Call the bank"]
    assert (bank["state"], bank["priority"], bank["tags"], bank["deadline"]) == (
        "NEXT", "A", ["phone"], "2026-09-24")
    _, done = _call(server, "GET", "/notes/view?name=inbox&done=1")
    assert "Old thing" in {h["title"] for h in done["items"]}


def test_the_agenda_has_the_lunar_routines_not_the_astro_alerts(tree):
    items = notes._agenda(dt.date(2026, 9, 22))
    titles = [h["title"] for h in items]
    assert titles == ["Fix the TV ssh", "New moon routine", "Call the bank"]
    assert items[0]["overdue"] is True


def test_a_plain_timestamp_is_an_event_on_its_day(tree):
    items = [h for h in notes._agenda(dt.date(2026, 9, 22)) if h["path"] == "lunar.org"]
    # The full moon three weeks back is over, not overdue.
    assert [(h["title"], h["date"], h["overdue"]) for h in items] == [
        ("New moon routine", "2026-09-23", False)]
    # The day after, it is gone too.
    assert not [h for h in notes._agenda(dt.date(2026, 9, 24)) if h["path"] == "lunar.org"]


def test_read_a_subtree_and_follow_id_links(tree, server):
    _, views = _call(server, "GET", "/notes/view?name=inbox")
    tv = next(h for h in views["items"] if h["title"] == "Fix the TV ssh")
    status, got = _call(server, "GET", f"/notes/read?path=inbox.org&at={tv['at']}")
    assert status == 200
    assert got["title"] == "Fix the TV ssh"
    assert "telly" in got["text"] and "Old thing" not in got["text"]
    _, note = _call(server, "GET", "/notes/read?path=roam/projects/yoga.org")
    assert note["title"] == "agent-yoga"
    assert note["links"] == [{"label": "bank", "path": "roam/projects/bank.org"}]


def test_read_refuses_outside_the_tree(tree, server):
    for bad in ("../etc/passwd", ".git/config.org", "/etc/hostname", "roam"):
        assert _call(server, "GET", f"/notes/read?path={bad}")[0] == 404
    # A line that is not a heading any more: the file moved under the app.
    assert _call(server, "GET", "/notes/read?path=inbox.org&at=2")[0] == 409


def test_search_notes_and_memory(tree, server):
    _, got = _call(server, "GET", "/notes/search?q=TELLY")
    paths = {h["path"] for h in got["notes"]}
    assert paths == {"inbox.org", "roam/projects/bank.org"}   # sessions left out
    assert got["memories"][0]["text"] == "telly memory"
    _, every = _call(server, "GET", "/notes/search?q=telly&all=1&memory=0")
    assert "roam/sessions/inbox/s1.org" in {h["path"] for h in every["notes"]}
    assert every["memories"] == []


def test_capture_appends_one_entry_and_remembers(tree, server):
    status, got = _call(server, "POST", "/notes/capture",
                        {"text": "Buy milk\n* not a heading\nsecond line"})
    assert status == 200 and got["path"] == "inbox.org"
    text = (tree / "inbox.org").read_text()
    assert text.startswith(INBOX)
    tail = text[len(INBOX):]
    assert tail.startswith("* TODO Buy milk\n:PROPERTIES:\n:CREATED: [")
    assert tail.endswith(":END:\n * not a heading\nsecond line\n")
    _, sub = _call(server, "GET", f"/notes/read?path=inbox.org&at={got['at']}")
    assert sub["title"] == "Buy milk"
    for _ in range(50):
        if REMEMBERED:
            break
        threading.Event().wait(0.02)
    assert REMEMBERED[0]["text"].endswith("Buy milk\n* not a heading\nsecond line")


def test_capture_a_note_without_memory(tree, server):
    (tree / "inbox.org").write_text("* x")          # no trailing newline
    _, got = _call(server, "POST", "/notes/capture",
                   {"text": "idea", "kind": "note", "memory": False})
    assert (tree / "inbox.org").read_text().startswith("* x\n* idea\n:PROPERTIES:")
    assert got["at"] == 2 and got["remembered"] is False
    threading.Event().wait(0.1)
    assert REMEMBERED == []
    assert _call(server, "POST", "/notes/capture", {"text": "  "})[0] == 400


# --- search without ripgrep -------------------------------------------------------

def test_the_built_in_search_finds_what_ripgrep_does(tree, monkeypatch):
    rg = notes._rg("telly", everything=False, limit=30)
    monkeypatch.setattr(notes.shutil, "which", lambda name: None)
    scan = notes._rg("telly", everything=False, limit=30)
    assert {(h["path"], h["line"]) for h in scan} == {(h["path"], h["line"]) for h in rg}
    every = notes._rg("telly", everything=True, limit=30)
    assert "roam/sessions/inbox/s1.org" in {h["path"] for h in every}


# --- setting it up -----------------------------------------------------------------

from agent_media_server import harnesses as setup_windows, notes_setup  # noqa: E402


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """A host with no notes yet, and a caller allowed to set them up."""
    root = tmp_path / "org"
    monkeypatch.setenv("MEDIA_NOTES_DIR", str(root))
    monkeypatch.setenv("MEDIA_PARAGTD_DIR", str(tmp_path / "paragtd"))
    monkeypatch.delenv("MEDIA_NOTES_REPO", raising=False)
    monkeypatch.setattr(auth, "may_control_speech", lambda bearer: (
        (True, {}) if bearer == "good" else (False, {"error": "no", "status": 403})))
    monkeypatch.setattr(notes, "_memory_call", lambda *a, **k: None)
    calls: list[tuple] = []
    monkeypatch.setattr(notes_setup, "_systemctl", lambda *a: (
        calls.append(a) or ((0, "") if a[0] == "enable" else (1, "disabled"))))
    windows: list[list[str]] = []
    monkeypatch.setattr(setup_windows, "_window",
                        lambda argv, title: (windows.append(argv) or "%99", ""))
    monkeypatch.setattr(setup_windows, "_remember", lambda *a: None)
    return root, calls, windows


def _states(addr):
    status, got = _call(addr, "GET", "/notes/setup")
    assert status == 200
    return {c["name"]: c for c in got["components"]}


def test_setup_is_gated(fresh, server):
    assert _call(server, "GET", "/notes/setup", bearer="bad")[0] == 403
    assert _call(server, "POST", "/notes/setup", {"component": "org", "action": "create"},
                 bearer="bad")[0] == 403
    assert not fresh[0].exists()


def test_a_fresh_host_can_start_a_set_of_notes(fresh, server, monkeypatch):
    root, _, _ = fresh
    rows = _states(server)
    assert rows["org"]["state"] == "missing" and rows["org"]["actions"] == ["create"]
    assert rows["sync"]["state"] == "off"
    assert rows["memory"]["state"] == "down" and rows["memory"]["optional"]
    status, got = _call(server, "POST", "/notes/setup", {"component": "org", "action": "create"})
    assert status == 200 and got["done"] and "inbox.org" in got["created"]
    assert (root / "tickler.org").read_text().endswith("* Tickler\n")
    assert (root / "roam" / "projects").is_dir()
    assert _states(server)["org"]["state"] == "ok"
    # And the notes routes work on it straight away.
    monkeypatch.setattr(auth, "gate", lambda bearer: ({"username": "david"}, {}))
    assert _call(server, "POST", "/notes/capture", {"text": "first"})[0] == 200
    assert "* TODO first" in (root / "inbox.org").read_text()


def test_create_never_overwrites(fresh, server):
    root, _, _ = fresh
    root.mkdir()
    (root / "inbox.org").write_text("* mine\n")
    rows = _states(server)
    assert rows["org"]["state"] == "ok" and "create" in rows["org"]["actions"]
    _call(server, "POST", "/notes/setup", {"component": "org", "action": "create"})
    assert (root / "inbox.org").read_text() == "* mine\n"
    assert (root / "someday.org").is_file()


def test_clone_needs_a_repo_and_runs_in_a_window(fresh, server, monkeypatch):
    root, _, windows = fresh
    assert _call(server, "POST", "/notes/setup",
                 {"component": "org", "action": "clone"})[0] == 409
    monkeypatch.setenv("MEDIA_NOTES_REPO", "git@example:me/org.git")
    assert _states(server)["org"]["actions"] == ["clone", "create"]
    status, got = _call(server, "POST", "/notes/setup", {"component": "org", "action": "clone"})
    assert status == 200 and got["pane"] == "%99"
    assert windows == [["git", "clone", "git@example:me/org.git", str(root)]]


def test_sync_enables_the_timer_on_a_repo_with_a_remote(fresh, server):
    root, calls, _ = fresh
    _call(server, "POST", "/notes/setup", {"component": "org", "action": "create"})
    assert "no remote" in _states(server)["sync"]["why"]
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "x:y"], check=True)
    row = _states(server)["sync"]
    assert row["state"] == "off" and row["actions"] == ["enable"]
    status, got = _call(server, "POST", "/notes/setup", {"component": "sync", "action": "enable"})
    assert status == 200 and ("enable", "--now", "org-autosync.timer") in calls


def test_paragtd_installs_in_a_window(fresh, server, tmp_path):
    _, _, windows = fresh
    assert _states(server)["paragtd"]["actions"] == ["install"]
    status, got = _call(server, "POST", "/notes/setup",
                        {"component": "paragtd", "action": "install"})
    assert status == 200 and got["pane"] == "%99"
    assert "bin/bootstrap" in windows[0][-1] and str(tmp_path / "paragtd") in windows[0][-1]
    assert _call(server, "POST", "/notes/setup",
                 {"component": "memory", "action": "install"})[0] == 400


# --- reading aloud -----------------------------------------------------------------

def test_spoken_drops_the_org_furniture():
    text = notes.spoken(":PROPERTIES:\n:ID: x\n:END:\n#+title: T\n* TODO Call the bank :phone:\n"
                        "  SCHEDULED: <2026-09-20 Sun>\n- [ ] ask about [[id:abc][the loan]]\nPlain.\n")
    assert text == "Todo: Call the bank.\nask about the loan\nPlain."


def test_say_hands_the_note_to_media_say(tree, server, monkeypatch):
    monkeypatch.setattr(auth, "may_control_speech", lambda b: (True, {}) if b == "good"
                        else (False, {"error": "no", "status": 403}))
    started = []

    class FakePopen:
        def __init__(self, argv, **kw):
            started.append(argv)
            self.stdin = self
            self.data = b""

        def write(self, b):
            started.append(b.decode())

        def close(self):
            pass

    monkeypatch.setattr(notes.subprocess, "Popen", FakePopen)
    assert _call(server, "POST", "/notes/say", {"path": "inbox.org"}, bearer="bad")[0] == 403
    status, got = _call(server, "POST", "/notes/say", {"path": "roam/projects/bank.org"})
    assert status == 200 and got["title"] == "Bank"
    assert started[0][-1] == "say" and started[1] == "telly money"
    assert _call(server, "POST", "/notes/say", {"path": "nope.org"})[0] == 404
