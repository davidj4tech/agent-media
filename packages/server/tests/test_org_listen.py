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

    def _state(self):
        return self

    def get_resume_pos(self, uri):
        return self.resume_pos.get(uri)


@pytest.fixture()
def book(tree, monkeypatch, tmp_path):
    (tree / "walk.org").write_text(NOTE)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(auth, "may_control_speech", lambda b: (True, {}) if b == "good"
                        else (False, {"error": "no", "status": 403}))
    b = Book()
    monkeypatch.setattr(org_listen, "_book", lambda: b)
    monkeypatch.setattr(org_listen, "_CACHE_TTL_S", 0.0)
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
    assert got["state"] == "playing" and book.calls[-1] == ("seek", 4.0)
    assert len(book.rendered) == 1

    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "toggle"})
    assert got["state"] == "paused" and book.calls[-1] == ("pause",)
    assert _call(server, "POST", "/org/listen/ctl", {"action": "dance"})[0] == 400
    _, got = _call(server, "POST", "/org/listen/ctl", {"action": "stop"})
    assert got == {"ok": True, "state": "idle"}


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
    assert _call(server, "GET", "/org/listen") == (200, {"ok": True, "state": "idle"})
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
