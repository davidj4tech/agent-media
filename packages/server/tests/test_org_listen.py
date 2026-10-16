"""Listening to a note with follow-along (org_listen.py, contract §6.10).

The renderer and the book channel are stubbed: nothing is synthesised and
nothing reaches a player, so no test plays on David's phone."""

from __future__ import annotations

import json
import time

import pytest

from agent_media_core import docs
from agent_media_server import auth, org_listen

from test_org import _call, server, tree  # noqa: F401  (fixtures)

NOTE = "#+title: Walk\n\n* Plan\nFirst we go.\n\nThen we rest.\n* Back\nHome again.\n"


class Book:
    """The book channel, as org_listen sees it."""

    def __init__(self):
        self.calls: list = []
        self.np: dict = {"idle": True}
        self.resume_pos: dict = {}

    def book_play(self, uri, resume=True, start_ms=-1, target="", title=""):
        self.calls.append(("play", uri, start_ms, title))
        self.np = {"idle": False, "uri": uri, "position_ms": start_ms,
                   "duration_ms": 9000, "paused": False}
        return {"ok": True}

    def book_seek(self, s, target=""):
        self.calls.append(("seek", s))
        self.np["position_ms"] = int(s * 1000)
        return {"ok": True}

    def book_pause(self, target=""):
        self.calls.append(("pause",))
        self.np["paused"] = True
        return {"ok": True}

    def book_resume(self, target=""):
        self.calls.append(("resume",))
        self.np["paused"] = False
        return {"ok": True}

    def book_stop(self, target=""):
        self.calls.append(("stop",))
        self.np = {"idle": True}
        return {"ok": True}

    def book_now_playing(self, target=""):
        return dict(self.np)

    def book_skip(self, seconds, target=""):
        self.calls.append(("skip", seconds))
        return {"ok": True}

    def book_speed(self, rate, target=""):
        self.calls.append(("speed", rate))
        self.np["speed"] = rate
        return {"ok": True}

    def _book_target(self, name=""):
        return name

    def _state(self):
        return self

    def get_resume_pos(self, uri):
        return self.resume_pos.get(uri)

    def set_resume_pos(self, uri, pos_ms):
        self.resume_pos[uri] = pos_ms


@pytest.fixture()
def book(tree, monkeypatch, tmp_path):
    (tree / "walk.org").write_text(NOTE)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))   # listen-prefs.json
    monkeypatch.setattr(auth, "may_control_speech", lambda b: (True, {}) if b == "good"
                        else (False, {"error": "no", "status": 403}))
    b = Book()
    monkeypatch.setattr(org_listen, "_book", lambda: b)

    # The player itself: one read, one batch of commands (org_listen._read,
    # _send), recorded as the calls the tests name.
    def send(cmds):
        for c in cmds:
            if c[0] == "seek":
                b.calls.append(("seek", float(c[1])))
                b.np["position_ms"] = int(float(c[1]) * 1000)
            elif c[1] == "pause":
                b.calls.append(("pause",) if c[2] else ("resume",))
                b.np["paused"] = c[2]
            elif c[1] == "speed":
                b.calls.append(("speed", c[2]))
                b.np["speed"] = c[2]
            else:
                b.np["muted" if c[1] == "mute" else c[1]] = c[2]
    monkeypatch.setattr(org_listen, "_read", lambda: dict(b.np))
    monkeypatch.setattr(org_listen, "_send", send)
    monkeypatch.setattr(org_listen, "_CACHE_TTL_S", 0.0)
    # A press acts on a reading up to 6 s old: not the last test's player.
    org_listen._forget_cache()
    org_listen._LAST.clear()
    rendered = []

    def fake_render(sections, stem, force=False, extra=None, **kw):
        # A second per paragraph, as the real one measures them.
        rendered.append(stem)
        clip = docs.doc_cache_dir() / f"{stem}.mp3"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"mp3")
        rows, clock = [], 0.0
        for sec in sections:
            for line, _t in docs._pieces(sec):
                rows.append({"start": clock, "end": clock + 1, "line": line})
                clock += 1
        docs.timings_path(clip).write_text(json.dumps(
            {**(extra or {}), "duration": clock, "timings": rows}))
        return clip

    monkeypatch.setattr(docs, "render_sections", fake_render)
    b.rendered = rendered
    return b


def _until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_projection_keeps_each_paragraphs_line():
    secs = docs.sections_for(NOTE, "org")
    assert [(s.heading, s.line, s.paras) for s in secs] == [
        ("Plan", 2, [(3, "First we go."), (5, "Then we rest.")]),
        ("Back", 6, [(7, "Home again.")])]
    md = "# Top\n\nOne\ntwo.\n\n```\nx\n```\n- a\n- b\n"
    (top,) = docs.sections_for(md)
    assert top.line == 0
    assert top.paras == [(2, "One two."), (5, "A code example follows, 1 line."),
                         (8, "a"), (9, "b")]


def test_listen_renders_then_plays_and_follows(book, server):
    assert _call(server, "POST", "/org/listen", {"path": "walk.org"}, bearer="bad")[0] == 403
    status, got = _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert status == 200 and got["state"] == "rendering" and got["title"] == "Walk"
    assert _until(lambda: book.calls)
    kind, uri, start, title = book.calls[0]
    assert (kind, start, title) == ("play", 0, "Walk") and uri.endswith(".mp3")

    # Paragraphs a second each: heading 2, 3, 5, heading 6, 7.
    book.np["position_ms"] = 2500
    status, got = _call(server, "GET", "/org/listen")
    assert status == 200
    assert got["state"] == "playing" and got["path"] == "walk.org" and got["line"] == 5

    # Cached now: a tap on a paragraph is a seek, not a render or a reload.
    status, got = _call(server, "POST", "/org/listen", {"path": "walk.org", "line": 7})
    assert got["state"] == "playing" and book.calls[-2:] == [("seek", 4.0), ("resume",)]
    assert len(book.rendered) == 1

    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "toggle"})
    assert got["state"] == "paused" and book.calls[-1] == ("pause",)
    assert _call(server, "POST", "/org/listen/ctl", {"action": "dance"})[0] == 400
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "stop"})
    assert got == {"ok": True, "state": "idle", "speed": 1.0, "volume": 100}


def test_a_heading_is_its_own_render_with_lines_of_its_own(book, server):
    _call(server, "POST", "/org/listen", {"path": "walk.org", "at": 7})
    assert _until(lambda: book.calls)
    book.np["position_ms"] = 1200          # the heading's text is line 1 of the subtree
    _, got = _call(server, "GET", "/org/listen")
    assert got["at"] == 7 and got["line"] == 1


def test_a_note_heard_to_the_end_starts_again(book, server):
    _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert _until(lambda: book.calls)
    uri = book.calls[0][1]
    book.resume_pos[uri] = 4500             # of 5 s
    _call(server, "POST", "/org/listen/ctl", {"action": "stop"})
    _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert book.calls[-1][2] == 0
    book.resume_pos[uri] = 1500
    _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert book.calls[-1][2] == 1500


def test_nothing_playing_is_idle_and_bad_notes_refused(book, server):
    assert _call(server, "GET", "/org/listen") == (
        200, {"ok": True, "state": "idle", "speed": 1.0, "volume": 100})
    assert _call(server, "GET", "/org/listen", bearer="bad")[0] == 401
    assert _call(server, "POST", "/org/listen", {"path": "nope.org"})[0] == 404
    assert book.calls == []


def test_a_project_document_reads_and_listens(book, server, tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    (proj / "app" / "docs").mkdir(parents=True)
    (proj / "app" / ".git").mkdir()
    (proj / "app" / ".git" / "x.md").write_text("# secret\n")
    doc = proj / "app" / "docs" / "plan.md"
    doc.write_text("# The plan\n\nFirst part.\n\n## Then\n\nSecond part.\n")
    (proj / "app" / "notes.py").write_text("x = 1\n")
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(proj))
    monkeypatch.setenv("MEDIA_DOC_ROOTS", str(tmp_path / "nowhere"))

    status, got = _call(server, "GET", "/doc/read?path=" + str(doc))
    assert status == 200 and got["title"] == "The plan" and got["fmt"] == "md"
    assert got["text"].startswith("# The plan")
    for bad in (proj / "app" / ".git" / "x.md", proj / "app" / "notes.py",
                tmp_path / "elsewhere.md", proj / "app" / "docs" / ".." / ".." / ".." / "x.md"):
        assert _call(server, "GET", "/doc/read?path=" + str(bad))[0] == 404, bad
    assert _call(server, "GET", "/doc/read?path=" + str(doc), bearer="bad")[0] == 401

    status, got = _call(server, "POST", "/org/listen", {"doc": str(doc)})
    assert status == 200 and got["state"] == "rendering" and got["doc"] == str(doc)
    assert _until(lambda: book.calls)
    assert "/doc-" in book.calls[0][1]
    book.np["position_ms"] = 3500            # heading 0, 2, heading 4, then 6
    _, got = _call(server, "GET", "/org/listen")
    assert got["doc"] == str(doc) and "path" not in got and got["line"] == 6
    assert _call(server, "POST", "/org/listen", {"doc": str(tmp_path / "x.md")})[0] == 404


def test_a_note_plays_on_the_phones_own_book_player(monkeypatch):
    # Not where speech goes: the app's speech player cannot open a host path.
    monkeypatch.delenv("MEDIA_LISTEN_TARGET", raising=False)
    monkeypatch.delenv("MEDIA_BOOK_SOCKET_PHONE", raising=False)
    assert org_listen._target() == ""
    monkeypatch.setenv("MEDIA_BOOK_SOCKET_PHONE", "tcp://p8a:6603")
    assert org_listen._target() == "phone"
    monkeypatch.setenv("MEDIA_LISTEN_TARGET", "rooms")
    assert org_listen._target() == "rooms"


def test_prev_and_next_move_by_paragraph(book, server):
    _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert _until(lambda: book.calls)
    book.np["position_ms"] = 1500           # in the second paragraph (1 s)
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "next"})
    assert book.calls[-1] == ("seek", 2.0) and got["line"] == 5
    book.np["position_ms"] = 2500           # half a second in: back one
    _call(server, "POST", "/org/listen/ctl", {"action": "prev"})
    assert book.calls[-1] == ("seek", 1.0)
    book.np["position_ms"] = 0
    _call(server, "POST", "/org/listen/ctl", {"action": "prev"})
    assert book.calls[-1] == ("seek", 0.0)
    _call(server, "POST", "/org/listen/ctl", {"action": "stop"})
    assert _call(server, "POST", "/org/listen/ctl", {"action": "next"})[0] == 409


def test_heard_to_the_end_is_ended_and_resume_starts_again(book, server):
    _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert _until(lambda: book.calls)
    book.np.update({"position_ms": 8900, "duration_ms": 9000, "paused": True})
    _, got = _call(server, "GET", "/org/listen")
    assert got["state"] == "ended"
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "resume"})
    assert book.calls[-2:] == [("seek", 0.0), ("resume",)] and got["state"] == "playing"
    book.np.update({"position_ms": 4000, "paused": True})
    _, got = _call(server, "GET", "/org/listen")
    assert got["state"] == "paused"


def test_the_sheets_knobs(book, server, monkeypatch):
    _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert _until(lambda: book.calls)
    book.np.update({"speed": 1.0, "volume": 100, "muted": False})
    _, got = _call(server, "GET", "/org/listen")
    assert got["volume"] == 100 and got["muted"] is False and got["speed"] == 1.0
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "speed+"})
    assert book.calls[-1] == ("speed", 1.1) and got["speed"] == 1.1
    _call(server, "POST", "/org/listen/ctl", {"action": "speed0"})
    assert book.calls[-1] == ("speed", 1.0)
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "vol-"})
    assert got["volume"] == 90
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "mute"})
    assert got["muted"] is True
    _call(server, "POST", "/org/listen/ctl", {"action": "seek", "to": 3})
    assert book.calls[-1] == ("seek", 3.0)
    book.np["position_ms"] = 3500
    org_listen._forget_cache()
    _call(server, "POST", "/org/listen/ctl", {"action": "skip", "by": -15})
    assert book.calls[-1] == ("seek", 0.0)              # 3.5 s back by 15: the start
    assert _call(server, "POST", "/org/listen/ctl", {"action": "seek"})[0] == 400


def test_speed_and_volume_are_kept_for_the_next_note(book, server, tmp_path):
    """The knobs turn with nothing playing, and every note loads with them
    (David, 15 Oct 2026: "change speed and volume ... even when something's
    not playing")."""
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "speed+"})
    assert got["state"] == "idle" and got["speed"] == 1.1
    _call(server, "POST", "/org/listen/ctl", {"action": "speed+"})
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "vol-"})
    assert (got["speed"], got["volume"]) == (1.2, 90)
    assert book.calls == []                            # nothing to send it to
    assert _call(server, "POST", "/org/listen/ctl", {"action": "mute"})[0] == 409
    assert json.loads((tmp_path / "state" / "agent-media" / "listen-prefs.json").read_text()) \
        == {"speed": 1.2, "volume": 90}
    _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert _until(lambda: ("speed", 1.2) in book.calls)
    assert book.np["volume"] == 90
    # Turned while a note plays: kept for the next one too.
    org_listen._forget_cache()
    _call(server, "POST", "/org/listen/ctl", {"action": "speed0"})
    _call(server, "POST", "/org/listen/ctl", {"action": "stop"})
    book.np = {"idle": True}
    org_listen._forget_cache()
    _, got = _call(server, "GET", "/org/listen")
    assert (got["state"], got["speed"], got["volume"]) == ("idle", 1.0, 90)


def test_a_press_is_one_batch_on_the_last_reading(book, server, monkeypatch):
    """A press reads nothing first when the last reading is fresh: on the
    phone a read is ~0.9 s, and a press used to cost four or five."""
    _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert _until(lambda: book.calls)
    monkeypatch.setattr(org_listen, "_CACHE_TTL_S", 1.0)
    reads, sends = [], []
    monkeypatch.setattr(org_listen, "_read", lambda: reads.append(1) or dict(book.np))
    monkeypatch.setattr(org_listen, "_send", lambda cmds: sends.append(cmds))
    org_listen._forget_cache()
    _call(server, "GET", "/org/listen")
    assert len(reads) == 1
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "pause"})
    _, got2 = _call(server, "POST", "/org/listen/ctl", {"action": "speed+"})
    assert len(reads) == 1 and len(sends) == 2
    assert sends[0] == [["set_property", "pause", True]]
    assert got["state"] == "paused" and got2["speed"] == 1.1


def test_a_note_plays_in_the_apps_book_player(book, server, monkeypatch, tmp_path):
    """MEDIA_LISTEN_ENDPOINT: the app's book player (a media session, so a
    headset's pause reaches it), streamed from the clip server; the place is
    kept here, and stop then resume reopens it there."""
    monkeypatch.setenv("MEDIA_LISTEN_ENDPOINT", "tcp://127.0.0.1:16627")
    monkeypatch.setenv("MEDIA_BOOK_HTTP_ROOT", str(tmp_path / "cache" / "agent-media"))
    monkeypatch.setenv("MEDIA_BOOK_BASEURL_PHONE", "http://red5:8780")
    sent: list = []

    def send(cmds):
        sent.append(cmds)
        for c in cmds:
            if c[0] == "loadfile":
                book.np = {"idle": False, "uri": c[1], "position_ms": 0,
                           "duration_ms": 5000, "paused": False}
            elif c[0] == "seek":
                book.np["position_ms"] = int(float(c[1]) * 1000)
            elif c[0] == "stop":
                book.np = {"idle": True}
            elif c[1] == "pause":
                book.np["paused"] = c[2]
    monkeypatch.setattr(org_listen, "_send", send)
    _call(server, "POST", "/org/listen", {"path": "walk.org"})
    assert _until(lambda: sent)
    load = sent[0]
    url = load[0][1]
    assert load[0][0] == "loadfile" and load[0][2] == "replace"
    assert url.startswith("http://red5:8780/docs/") and url.endswith(".mp3")
    assert load[1] == ["set_property", "force-media-title", "Walk"]
    assert book.calls == []                       # not the book channel
    book.np["position_ms"] = 2500
    _, got = _call(server, "GET", "/org/listen")
    assert got["state"] == "playing" and got["line"] == 5

    _call(server, "POST", "/org/listen/ctl", {"action": "pause"})
    clip = str(org_listen._clip_of(url))
    assert abs(book.resume_pos[clip] - 2500) < 200   # run on by the clock
    book.np["position_ms"] = 1500
    org_listen._forget_cache()
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "stop"})
    assert sent[-1] == [["stop"]] and got["state"] == "idle"
    assert book.resume_pos[clip] == 1500
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "toggle"})
    assert sent[-1][0] == ["loadfile", url, "replace"] and sent[-1][-1] == ["seek", 1.5, "absolute"]
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "vol+"})
    assert got["volume"] == 100                   # the app's player has no boost
    _call(server, "POST", "/org/listen/ctl", {"action": "speed+"})
    _call(server, "POST", "/org/listen/ctl", {"action": "stop"})
    _call(server, "POST", "/org/listen/ctl", {"action": "toggle"})
    assert sent[-1][-2:] == [["set_property", "speed", 1.1], ["set_property", "volume", 100]]


def test_the_apps_player_idle_at_the_end_reads_as_ended(monkeypatch):
    """The app's player goes idle at the end of a file (Termux's mpv stays
    paused on its last frame): read as paused at the end, so "ended"."""
    from agent_media_core.sinks import _mpv_ipc

    monkeypatch.setattr(org_listen, "_socket", lambda: "tcp://x:1")
    monkeypatch.setattr(_mpv_ipc, "display_properties", lambda *a, **k: {
        "idle-active": True, "eof-reached": True, "path": "http://h/docs/a.mp3",
        "time-pos": None, "duration": 9.0, "pause": False})
    got = org_listen._read()
    assert got["paused"] is True and got["position_ms"] == 9000 and org_listen._ended(got)
    monkeypatch.setattr(_mpv_ipc, "display_properties", lambda *a, **k: {
        "idle-active": True, "eof-reached": False, "path": None})
    assert org_listen._read() == {"idle": True}


def test_a_projects_documents_and_finding_one(tree, server, tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    app_dir = proj / "app"
    (app_dir / "docs" / "proposals").mkdir(parents=True)
    (app_dir / ".git").mkdir()
    (app_dir / "README.md").write_text("# The app\n\nWhat it is.\n")
    (app_dir / "docs" / "proposals" / "2026-08-01-old.md").write_text("# Old idea\n\nStatus: proposed\n\nA kettle.\n")
    (app_dir / "docs" / "proposals" / "2026-09-01-new.md").write_text("# New idea\n\nStatus: built\n\nThe kettle sings.\n")
    (app_dir / "docs" / "guide.md").write_text("# Guide\n\nNo kettles here.\n")
    (app_dir / "docs" / "empty.md").write_text("")
    clone = proj / "app-copy" / "docs" / "proposals"
    clone.mkdir(parents=True)
    (clone / "2026-09-01-new.md").write_text("# New idea\n\nThe kettle sings.\n")
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(proj))
    monkeypatch.setenv("MEDIA_DOC_ROOTS", str(tmp_path / "nowhere"))

    status, got = _call(server, "GET", "/docs?project=p-app")
    assert status == 200 and got["root"] == str(app_dir)
    assert [d["title"] for d in got["docs"]] == ["The app", "New idea", "Old idea", "Guide"]
    assert got["docs"][1]["date"] == "2026-09-01" and got["docs"][1]["kind"] == "proposals"
    _, by_path = _call(server, "GET", "/docs?path=" + str(app_dir / "docs"))
    assert by_path["root"] == str(app_dir)
    for bad in ("/docs?project=..", "/docs?project=nope", "/docs?path=" + str(proj), "/docs"):
        assert _call(server, "GET", bad)[0] == 404, bad
    assert _call(server, "GET", "/docs?project=app", bearer="bad")[0] == 401

    from agent_media_server import doc_read
    hits = doc_read.search(["kettle", "sings"])
    assert [h["title"] for h in hits] == ["New idea"]          # one copy, not the clone's too
    assert hits[0]["snippet"] == "The kettle sings." and hits[0]["project"] in ("app", "app-copy")
    assert [h["title"] for h in doc_read.search(["guide"])] == ["Guide"]


def test_finding_a_document_without_rg(tmp_path, monkeypatch):
    """No ripgrep (GitHub's runners, a fresh Mac or Windows): the search walks
    the documents itself and finds the same one, once."""
    from agent_media_server import doc_read
    proj = tmp_path / "projects"
    for name in ("app", "app-copy"):
        (proj / name / "docs").mkdir(parents=True)
        (proj / name / "docs" / "new.md").write_text("# New idea\n\nThe kettle sings.\n")
    (proj / "app" / ".git").mkdir()
    (proj / "app" / ".git" / "kettle.md").write_text("# Hidden\n\nThe kettle sings.\n")
    (proj / "app" / "docs" / "guide.md").write_text("# Guide\n\nNo kettles here.\n")
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(proj))
    monkeypatch.setenv("MEDIA_DOC_ROOTS", str(tmp_path / "nowhere"))
    monkeypatch.setattr(doc_read, "_rg", lambda args, cwd: None)
    hits = doc_read.search(["kettle", "sings"])
    assert [h["title"] for h in hits] == ["New idea"]
    assert hits[0]["project"] in ("app", "app-copy") and hits[0]["snippet"] == "The kettle sings."
    assert [h["title"] for h in doc_read.search(["guide"])] == ["Guide"]


def test_a_projects_documents_carry_area_and_state(tree, server, tmp_path, monkeypatch):
    root = tmp_path / "projects" / "app"
    (root / "docs" / "proposals").mkdir(parents=True)
    (root / "docs" / "decisions").mkdir()
    (root / "packages" / "core").mkdir(parents=True)
    (root / "README.md").write_text("# App\n")
    (root / "packages" / "core" / "README.md").write_text("# Core\n")
    props = root / "docs" / "proposals"
    (props / "2026-09-01-a.md").write_text("# A\n\nStatus: proposal, nothing built\n")
    (props / "2026-09-02-b.md").write_text("# B\n\nStatus: steps 1-2 built\n")
    (props / "2026-09-03-c.md").write_text("# C\n\nStatus: **built**, 2026-09-05\n")
    (props / "2026-09-04-d.md").write_text("# D\n\nIntro text. Status: **proposal**, nothing built.\n")
    (props / "2026-09-05-e.md").write_text("# E\n\nStatus: not doing. Superseded by F.\n")
    (props / "2026-09-06-f.md").write_text("# F\n\nNo status at all.\n")
    (root / "docs" / "decisions" / "2026-09-07-g.md").write_text("# G\n")
    monkeypatch.setenv("MEDIA_PROJECTS_DIR", str(tmp_path / "projects"))
    _, got = _call(server, "GET", "/docs?project=app")
    by = {d["title"]: d for d in got["docs"]}
    assert {t: by[t]["state"] for t in "ABCDEFG"} == {
        "A": "open", "B": "partly", "C": "settled", "D": "open", "E": "settled",
        "F": "open", "G": "settled"}
    assert by["D"]["status"] == "proposal, nothing built"      # found mid-line
    assert (by["App"]["area"], by["Core"]["area"], by["A"]["area"]) == ("readme", "elsewhere", "docs")
