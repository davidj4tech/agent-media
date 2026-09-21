"""The app contract: pins the JSON shapes `docs/server-contract.md` describes.

These go over real HTTP to an in-process canvas, so what is pinned is what a
client reads — status, envelope, key set — not what a function returns. The
data underneath is faked at the seams (ABS identity, the pane sweep, the
manifest shelf), and the real code between the seam and the socket builds the
answer.

Key sets are pinned exactly on purpose. A key the server adds without the spec
saying so is a key some client will start depending on; failing here is the
prompt to write it down.

Nothing here may reach a pane. Every path that types into tmux is replaced by
a recorder (`typed`) for every test that starts a server, and a test that
could type asserts what was recorded. `/input` in particular is only ever
asked without a token, and its sender is a recorder too.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_media_server import auth_abs, panes
from agent_media_visual import canvas, reply

SID = "6c73498c-02c1-4846-8350-a82006973571"
SID2 = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"
ROOT = {"username": "david", "type": "root"}
AUTH = {"Authorization": "Bearer tok"}


# --- the rig --------------------------------------------------------------------

@pytest.fixture()
def typed(monkeypatch):
    """Record, never perform, everything that would type into a pane."""
    log: list = []

    def rec(name, ret):
        def f(*a, **k):
            log.append((name, a))
            return ret
        return f

    monkeypatch.setattr(canvas, "send_input", rec("send_input", (False, "stubbed")))
    monkeypatch.setattr(reply, "_send_to_pane", rec("_send_to_pane", ""))
    monkeypatch.setattr(panes, "send", rec("panes.send", "stubbed"))
    monkeypatch.setattr(reply, "_tmux", rec("_tmux", ""))
    monkeypatch.setattr(reply, "open_window", rec("open_window", ("", "stubbed")))
    monkeypatch.setattr(reply, "_ensure_submitted", rec("_ensure_submitted", True))
    monkeypatch.setattr(reply, "_record_turn", rec("_record_turn", None))
    monkeypatch.setattr(canvas, "_media", rec("_media", "ok\n"))
    return log


@pytest.fixture()
def shelf(monkeypatch, tmp_path):
    """One shelved conversation (SID, "Sasonica music") and one live one (SID2)."""
    d = tmp_path / "book-tracks"
    d.mkdir()
    (d / f"{SID}.json").write_text(json.dumps(
        {"session": SID, "folder": "/lib/Conversations/p-agent-media/Sasonica music"}))
    monkeypatch.setattr(reply, "_manifest_dir", lambda: d)
    monkeypatch.setattr(reply, "live_sessions", lambda: {SID2: "%42"})
    monkeypatch.setattr(reply, "_pane_titles", lambda: {"%42": "Sasonica web"})
    monkeypatch.setattr(reply, "transcript_cwd", lambda s: str(tmp_path))
    monkeypatch.setattr(reply, "session_exists", lambda s: True)
    monkeypatch.setattr(reply, "_capture_pane", lambda p: "")
    monkeypatch.setattr(reply, "_agent_of_pane", lambda p: "claude")
    monkeypatch.setattr(reply, "_followup", lambda s: None)
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "working")
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(reply, "_STATES_CACHE", (0.0, []))
    reply._NOW_CACHE.clear()
    return tmp_path


@pytest.fixture()
def signed_in(monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda bearer: (ROOT, 200))
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    # An item lookup answers with the shelved conversation's folder; anything
    # else (the library listing item_for_session walks) answers with nothing.
    def get(url, bearer, path, method="GET"):
        if path.startswith("/api/items/"):
            return {"id": "li_1", "path": "/audiobooks/p-agent-media/Sasonica music"}, 200
        return None, 404
    monkeypatch.setattr(auth_abs, "_abs_get", get)


@pytest.fixture()
def server(typed):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), canvas.Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True)
    t.start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def call(addr, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=hdrs)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = raw
    return res, obj


def keys(obj) -> set:
    return set(obj.keys())


# --- the auth envelope, shared by every bearer-gated route ------------------------

GATED_GETS = ["/targets", "/sessions/state", "/conversations",
              f"/conversation?item=li_1", f"/conversation?session={SID}",
              "/conversation/log?item=li_1", f"/draft?session={SID}",
              "/speech/now", f"/commands?session={SID}"]


@pytest.mark.parametrize("path", GATED_GETS)
def test_no_bearer_is_401_with_an_error(server, monkeypatch, path):
    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, obj = call(server, "GET", path)
    assert res.status == 401, obj
    assert obj["ok"] is False and isinstance(obj["error"], str)
    assert res.getheader("Access-Control-Allow-Origin") == "*"


@pytest.mark.parametrize("path", GATED_GETS)
def test_abs_down_is_503_never_401(server, monkeypatch, path):
    # A 401 makes the client refresh its token and, failing that, log out.
    # An outage must not end the session.
    monkeypatch.setattr(auth_abs, "abs_identity", lambda bearer: (None, 0))
    res, obj = call(server, "GET", path, headers=AUTH)
    assert res.status == 503, obj
    assert obj["ok"] is False


@pytest.mark.parametrize("path", GATED_GETS)
def test_a_user_not_allowed_to_reply_is_403(server, monkeypatch, path):
    monkeypatch.setattr(auth_abs, "abs_identity",
                        lambda bearer: ({"username": "guest", "type": "user"}, 200))
    res, obj = call(server, "GET", path, headers=AUTH)
    assert res.status == 403, obj
    assert obj["ok"] is False


# --- thread list ------------------------------------------------------------------

def test_targets_shape(server, shelf, signed_in):
    res, obj = call(server, "GET", "/targets", headers=AUTH)
    assert res.status == 200
    assert keys(obj) == {"ok", "sessions", "places"}
    live, shelved = obj["sessions"]
    assert keys(live) == {"session", "title", "live", "pane"}
    assert live == {"session": SID2, "title": "Sasonica web", "live": True, "pane": "%42"}
    assert keys(shelved) == {"session", "title", "live", "pane", "at"}
    assert shelved["live"] is False and shelved["pane"] is None
    assert [keys(p) for p in obj["places"]] == [{"name", "path", "at"}]


def test_conversations_is_the_same_session_rows(server, shelf, signed_in):
    _, targets = call(server, "GET", "/targets", headers=AUTH)
    res, obj = call(server, "GET", "/conversations", headers=AUTH)
    assert res.status == 200
    assert keys(obj) == {"ok", "sessions"}
    assert [r["session"] for r in obj["sessions"]] == [r["session"] for r in targets["sessions"]]


def test_sessions_state_shape(server, shelf, signed_in):
    res, obj = call(server, "GET", "/sessions/state", headers=AUTH)
    assert res.status == 200
    assert keys(obj) == {"ok", "sessions"}
    assert obj["sessions"] == [{"session": SID2, "tail": "", "state": "working"}]


# --- one conversation -------------------------------------------------------------

def test_conversation_by_item_shape(server, shelf, signed_in):
    res, obj = call(server, "GET", "/conversation?item=li_1", headers=AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "session", "live", "pane", "resumable", "suggestion"}
    assert obj["session"] == SID and obj["live"] is False and obj["pane"] is None


def test_conversation_by_session_shape(server, shelf, signed_in):
    res, obj = call(server, "GET", f"/conversation?session={SID2}", headers=AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "session", "item", "scanning", "live", "pane", "resumable"}
    assert obj["live"] is True and obj["pane"] == "%42" and obj["item"] is None


def test_conversation_by_session_rejects_a_non_id(server, shelf, signed_in):
    res, obj = call(server, "GET", "/conversation?session=nope", headers=AUTH)
    assert res.status == 400 and obj["ok"] is False


def test_conversation_for_an_unknown_item_is_404(server, shelf, signed_in, monkeypatch):
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 404))
    res, obj = call(server, "GET", "/conversation?item=li_x", headers=AUTH)
    assert res.status == 404 and obj == {"ok": False, "error": "no such item"}


def test_conversation_log_shape(server, shelf, signed_in, monkeypatch):
    from agent_media_core import activity, book_tracks

    live_line = {"start": None, "end": None, "who": "agent", "text": "Two. Three.",
                 "at": 20.0, "key": "k2", "live": True,
                 "sentences": ["Two.", "Three."], "sentence": 0, "offsets": [0.0, 1.2],
                 "elapsed": 0.5, "paused": False, "server_time": 1.0, "delay": 0.0}
    monkeypatch.setattr(book_tracks, "conversation_log", lambda s, f, target=None: [
        {"start": 0.0, "end": 4.0, "who": "you", "text": "One?", "at": 10.0, "key": ""},
        live_line])
    monkeypatch.setattr(activity, "attach", lambda s, lines: None)
    res, obj = call(server, "GET", "/conversation/log?item=li_1", headers=AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "session", "lines", "pending", "working",
                         "approval", "suggestion"}
    assert obj["pending"] is False and obj["working"] is None and obj["approval"] is None
    you, agent = obj["lines"]
    assert keys(you) == {"start", "end", "who", "text", "at", "key"}
    # The live line's clock is brought up to the moment the answer leaves.
    assert agent["server_time"] > 1.0 and agent["elapsed"] > 0.5


def test_conversation_log_line_shape_from_book_tracks(monkeypatch):
    """The line fields come from core; pinned here because the app reads them."""
    from agent_media_core import book_tracks

    turn = SimpleNamespace(at=10.0, text="Hello there.", listener=False, key="k1",
                           ask=[{"question": "Which?", "multiSelect": False,
                                 "options": [{"label": "A", "description": ""}]}],
                           command=None, id=7)
    monkeypatch.setattr(book_tracks, "_read_manifest", lambda s: {"turns": []})
    monkeypatch.setattr(book_tracks.session_feed, "turns", lambda s: [turn])
    monkeypatch.setattr(book_tracks, "_live_turn", lambda s: None)
    monkeypatch.setattr(book_tracks, "_abs_ready", lambda t: None)
    (line,) = book_tracks.conversation_log(SID, None)
    assert keys(line) == {"start", "end", "who", "text", "at", "key", "id", "ask"}
    assert line["who"] == "agent" and line["text"] == "Which?" and line["id"] == 7


def test_commands_shape(server, shelf, signed_in, monkeypatch):
    from agent_media_core import slash_menu

    monkeypatch.setattr(slash_menu, "menu", lambda cwd: [
        {"name": "review", "description": "Review a PR", "aliases": []}])
    res, obj = call(server, "GET", f"/commands?session={SID}", headers=AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "cwd", "commands"}
    assert keys(obj["commands"][0]) == {"name", "description", "aliases"}


# --- sending ----------------------------------------------------------------------

def test_ask_dry_run_shape(server, shelf, signed_in, typed):
    res, obj = call(server, "POST", "/ask", {"text": "hello there", "dry": True}, AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "mode", "how", "agent", "session", "title", "item",
                         "text", "dry"}
    assert (obj["mode"], obj["how"], obj["session"]) == ("new", "default", None)
    assert typed == []


def test_ask_dry_run_to_a_picked_session(server, shelf, signed_in, typed):
    res, obj = call(server, "POST", "/ask",
                    {"text": "hello", "target": SID2, "dry": True}, AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "mode", "how", "session", "title", "item", "text", "dry"}
    assert (obj["mode"], obj["how"], obj["title"]) == ("continued", "picked", "Sasonica web")
    assert typed == []


def test_ask_ambiguous_is_300_with_candidates(server, shelf, signed_in, typed, monkeypatch):
    monkeypatch.setattr(reply, "live_sessions", lambda: {SID2: "%42", SID: "%43"})
    monkeypatch.setattr(reply, "_pane_titles",
                        lambda: {"%42": "Sasonica web", "%43": "Sasonica music"})
    res, obj = call(server, "POST", "/ask", {"text": "reply to sasonica, hi"}, AUTH)
    assert res.status == 300, obj
    assert keys(obj) == {"ok", "error", "ambiguous", "text"}
    assert {r["session"] for r in obj["ambiguous"]} == {SID, SID2}
    assert typed == []


def test_ask_empty_is_400(server, shelf, signed_in):
    res, obj = call(server, "POST", "/ask", {"text": "  "}, AUTH)
    assert res.status == 400 and obj == {"ok": False, "error": "empty message"}


def test_reply_to_a_live_session_shape(server, shelf, signed_in, typed, monkeypatch):
    monkeypatch.setattr(reply, "live_sessions", lambda: {SID: "%42"})
    res, obj = call(server, "POST", "/reply", {"item": "li_1", "text": "yes"}, AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "session", "pane", "opened", "submitted"}
    assert obj == {"ok": True, "session": SID, "pane": "%42", "opened": False,
                   "submitted": True}
    # Typed into the recorder, never a pane.
    assert ("_send_to_pane", ("%42", "yes")) in typed


def test_reply_unsent_is_502_with_the_pane(server, shelf, signed_in, typed, monkeypatch):
    monkeypatch.setattr(reply, "live_sessions", lambda: {SID: "%42"})
    monkeypatch.setattr(reply, "_ensure_submitted", lambda *a, **k: False)
    res, obj = call(server, "POST", "/reply", {"item": "li_1", "text": "yes"}, AUTH)
    assert res.status == 502
    assert keys(obj) == {"ok", "error", "pane", "submitted", "session", "opened"}
    assert obj["submitted"] is False


def test_reply_empty_is_400(server, shelf, signed_in):
    res, obj = call(server, "POST", "/reply", {"item": "li_1", "text": ""}, AUTH)
    assert res.status == 400 and obj == {"ok": False, "error": "empty reply"}


def test_draft_round_trip(server, shelf, signed_in):
    res, obj = call(server, "POST", "/draft", {"session": SID, "text": "half", "at": 5}, AUTH)
    assert res.status == 200 and keys(obj) == {"ok", "session", "text", "at"}
    res, obj = call(server, "GET", f"/draft?session={SID}", headers=AUTH)
    assert obj == {"ok": True, "session": SID, "text": "half", "at": 5.0}
    call(server, "POST", "/draft", {"session": SID, "text": ""}, AUTH)
    _, obj = call(server, "GET", f"/draft?session={SID}", headers=AUTH)
    assert obj["text"] == "" and obj["at"] == 0


# --- managing a thread --------------------------------------------------------------

def test_rename_shape(server, shelf, signed_in, monkeypatch):
    from agent_media_core import book_tracks

    monkeypatch.setattr(book_tracks, "rename", lambda s, t: t)
    monkeypatch.setattr(reply, "send_rename", lambda s, t: "no pane: the session is not running")
    res, obj = call(server, "POST", "/rename", {"session": SID, "title": "New name"}, AUTH)
    assert res.status == 200, obj
    assert obj == {"ok": True, "session": SID, "title": "New name", "terminal": False,
                   "why": "no pane: the session is not running"}


def test_session_resume_when_live(server, shelf, signed_in):
    res, obj = call(server, "POST", "/session/resume", {"session": SID2}, AUTH)
    assert res.status == 200
    assert obj == {"ok": True, "session": SID2, "pane": "%42", "live": True, "opened": False}


def test_session_close_when_not_live(server, shelf, signed_in):
    res, obj = call(server, "POST", "/session/close", {"session": SID}, AUTH)
    assert res.status == 200
    assert obj == {"ok": True, "session": SID, "live": False, "closed": False}


def test_session_answer_when_nothing_is_asked_is_409(server, shelf, signed_in, monkeypatch, typed):
    monkeypatch.setattr(reply, "approval_for", lambda pane, agent="claude": None)
    res, obj = call(server, "POST", "/session/answer",
                    {"session": SID2, "choice": 1, "key": "abc"}, AUTH)
    assert res.status == 409
    assert obj == {"ok": False, "error": "that session is not waiting on a question"}
    assert typed == []


# --- speech -------------------------------------------------------------------------

def test_speech_now_quiet_shape(server, shelf, signed_in, monkeypatch):
    monkeypatch.setattr(canvas, "speech_state", lambda: {"kind": "state", "speaking": False})
    res, obj = call(server, "GET", "/speech/now", headers=AUTH)
    assert res.status == 200
    assert keys(obj) == {"ok", "live", "speaking", "paused", "sentence", "session",
                         "title", "item", "pos", "dur", "speed", "muted"}
    assert obj["live"] is False and obj["session"] is None


def test_speech_ctl_takes_only_listener_verbs(server, shelf, signed_in, typed):
    res, obj = call(server, "POST", "/speech/ctl", {"action": "rm -rf"}, AUTH)
    assert res.status == 400 and obj == {"ok": False, "error": "unknown action"}
    res, obj = call(server, "POST", "/speech/ctl", {"action": "toggle"}, AUTH)
    assert res.status == 200 and obj == {"ok": True, "out": "ok\n"}
    assert [n for n, _ in typed] == ["_media"]


# --- the stream ---------------------------------------------------------------------

def test_events_opens_with_retry_and_hello(server, monkeypatch):
    monkeypatch.setattr(canvas.HUB, "last", None)
    monkeypatch.setattr(canvas.HUB, "last_state", None)
    monkeypatch.setattr(canvas.HUB, "last_video", None)
    s = socket.create_connection(server, timeout=5)
    s.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
    buf = b""
    while b"hello" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    assert b"text/event-stream" in head
    assert body.startswith(b"retry: 2000\n\n")
    frame = body.split(b"\n\n")[1]
    assert json.loads(frame.removeprefix(b"data: ")) == {"kind": "hello",
                                                         "page": canvas.PAGE_ID}


# --- not part of the app contract, but must stay shut -------------------------------

def test_input_without_a_token_is_refused_and_types_nothing(server, typed):
    res, obj = call(server, "POST", "/input", {"text": "ls", "target": "speaker"})
    assert res.status == 401 and obj == {"error": "unauthorized"}
    assert typed == []


@pytest.mark.parametrize("path", ["/show", "/ctl", "/say", "/play"])
def test_token_routes_are_refused_without_one(server, typed, path):
    res, obj = call(server, "POST", path, {})
    assert res.status == 401 and obj == {"error": "unauthorized"}
    assert typed == []
