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

from agent_media_server import auth_abs, panes, send, sessions, speech
from agent_media_visual import canvas

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
    monkeypatch.setattr(send, "_send_to_pane", rec("_send_to_pane", ""))
    monkeypatch.setattr(panes, "send", rec("panes.send", "stubbed"))
    monkeypatch.setattr(panes, "_tmux", rec("_tmux", ""))
    monkeypatch.setattr(send, "open_window", rec("open_window", ("", "stubbed")))
    monkeypatch.setattr(send, "_ensure_submitted", rec("_ensure_submitted", True))
    monkeypatch.setattr(send, "_record_turn", rec("_record_turn", None))
    monkeypatch.setattr(canvas, "_media", rec("_media", "ok\n"))
    return log


@pytest.fixture()
def shelf(monkeypatch, tmp_path):
    """One shelved conversation (SID, "Sasonica music") and one live one (SID2)."""
    d = tmp_path / "book-tracks"
    d.mkdir()
    (d / f"{SID}.json").write_text(json.dumps(
        {"session": SID, "folder": "/lib/Conversations/p-agent-media/Sasonica music"}))
    monkeypatch.setattr(sessions, "_manifest_dir", lambda: d)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID2: "%42"})
    monkeypatch.setattr(sessions, "_pane_titles", lambda: {"%42": "Sasonica web"})
    monkeypatch.setattr(sessions, "transcript_cwd", lambda s: str(tmp_path))
    monkeypatch.setattr(sessions, "session_exists", lambda s: True)
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: "")
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "claude")
    monkeypatch.setattr(sessions, "_followup", lambda s: None)
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "working")
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(sessions, "_STATES_CACHE", (0.0, []))
    speech._NOW_CACHE.clear()
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
              "/speech/now", f"/commands?session={SID}", "/audio/targets", "/dashboard",
              f"/threads/{SID}/agents", "/search?q=x"]


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
    # `recap` and `archived` joined on 22 Sep 2026, deliberately: Claude
    # Code's latest "while you were away" summary, the list's preview line,
    # and the server-kept archive flag (§6.1). Null / false here: the rig has
    # no transcripts and nothing archived. `rested` and `pinned` joined the
    # same day, deliberately: the idle reaper's mark on a session it closed
    # ({"at", "reason"} | null, always null while live) and the keep-open pin
    # (§6.1, §6.4 /session/pin).
    # `project` and `cwd` joined on 22 Sep 2026, deliberately: where the
    # thread ran, for the small line under its title and the By-project order
    # (§6.1). Null when unknown — the live row here has no transcript; the
    # shelved one is filed under a series, which names its project.
    # `harness` joined on 23 Sep 2026: which agent holds the conversation
    # ("claude", "codex", "pi", "hermes"), now that the list is every
    # harness's sessions and not only the ones that spoke (§6.16).
    # `priority` joined on 24 Sep 2026: the thread always speaks — never held
    # by the desk toast, never silenced by a pane mute (§6.4 /session/priority);
    # `speech` the same day, its level: interrupt | auto | normal | quiet.
    assert keys(live) == {"session", "title", "live", "pane", "recap", "archived",
                          "rested", "pinned", "project", "cwd", "harness", "priority", "speech", "speech_own"}
    assert live == {"session": SID2, "title": "Sasonica web", "live": True, "pane": "%42",
                    "recap": None, "archived": False, "rested": None, "pinned": False,
                    "project": None, "cwd": None, "harness": "claude", "priority": False, "speech": "normal", "speech_own": False}
    assert keys(shelved) == {"session", "title", "live", "pane", "at", "recap", "archived",
                             "rested", "pinned", "project", "cwd", "harness", "priority", "speech", "speech_own"}
    assert shelved["project"] == "p-agent-media" and shelved["cwd"] is None
    assert shelved["rested"] is None and shelved["pinned"] is False
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
    # `mem_mb` per row and the `host` block joined on 22 Sep 2026,
    # deliberately (§6.1). The rig's live session has no process, so its
    # memory is unknown: null, and not counted in the sum.
    # `title` joined the same day, the name /targets gives the session.
    assert keys(obj) == {"ok", "sessions", "host"}
    _, targets = call(server, "GET", "/targets", headers=AUTH)
    title = next(r["title"] for r in targets["sessions"] if r["session"] == SID2)
    assert obj["sessions"] == [{"session": SID2, "tail": "", "state": "working",
                                "mem_mb": None, "title": title}]
    assert keys(obj["host"]) == {"mem_total_mb", "mem_available_mb", "sessions_mem_mb"}
    assert obj["host"]["sessions_mem_mb"] == 0


# --- one conversation -------------------------------------------------------------

def test_conversation_by_item_shape(server, shelf, signed_in):
    res, obj = call(server, "GET", "/conversation?item=li_1", headers=AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "session", "live", "pane", "resumable", "suggestion"}
    assert obj["session"] == SID and obj["live"] is False and obj["pane"] is None


def test_conversation_by_session_shape(server, shelf, signed_in):
    res, obj = call(server, "GET", f"/conversation?session={SID2}", headers=AUTH)
    assert res.status == 200, obj
    # `suggestion` joined on 22 Sep 2026 (§10: the session form gains what the
    # item form has), deliberately.
    assert keys(obj) == {"ok", "session", "item", "scanning", "live", "pane", "resumable",
                         "suggestion"}
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
    res, obj = call(server, "GET", "/conversation/log?item=li_1&messages=1", headers=AUTH)
    assert res.status == 200, obj
    # `recap` joined on 22 Sep 2026, deliberately (§6.2): the thread's "while
    # you were away" card, never a line. `messages` and `older` joined on
    # 22 Sep 2026, deliberately (§6.2.2): the thread as its transcript has
    # it, the newest first page of it, and whether there is more before.
    assert keys(obj) == {"ok", "session", "lines", "messages", "older", "pending",
                         "working", "approval", "suggestion", "recap"}
    assert obj["pending"] is False and obj["working"] is None and obj["approval"] is None
    assert obj["recap"] is None
    # No transcript in the rig, so the messages are the lines, reshaped.
    assert [m["role"] for m in obj["messages"]] == ["user", "assistant"]
    assert keys(obj["messages"][0]) == {"id", "role", "at", "parts", "spoken", "turn"}
    assert obj["older"] is False
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

    # The newest turn also hands over its timeline when it has one — without
    # `live`, which is a claim about playing and not one this makes.
    spoken = SimpleNamespace(at=20.0, text="One. Two.", listener=False, key="k2",
                             ask=[], command=None, id=8,
                             sentences=["One.", "Two."], durations=[1.5, 2.0],
                             starts=[0.0, 1.4])
    monkeypatch.setattr(book_tracks.session_feed, "turns", lambda s: [spoken])
    (line,) = book_tracks.conversation_log(SID, None)
    assert keys(line) == {"start", "end", "who", "text", "at", "key", "id",
                          "sentences", "offsets", "measured"}
    assert line["offsets"] == [0.0, 1.4] and line["measured"] is True


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
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID2: "%42", SID: "%43"})
    monkeypatch.setattr(sessions, "_pane_titles",
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
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    res, obj = call(server, "POST", "/reply", {"item": "li_1", "text": "yes"}, AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "session", "pane", "opened", "submitted"}
    assert obj == {"ok": True, "session": SID, "pane": "%42", "opened": False,
                   "submitted": True}
    # Typed into the recorder, never a pane.
    assert ("_send_to_pane", ("%42", "yes")) in typed


def test_reply_unsent_is_502_with_the_pane(server, shelf, signed_in, typed, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    monkeypatch.setattr(send, "_ensure_submitted", lambda *a, **k: False)
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
    monkeypatch.setattr(send, "send_rename", lambda s, t: "no pane: the session is not running")
    res, obj = call(server, "POST", "/rename", {"session": SID, "title": "New name"}, AUTH)
    assert res.status == 200, obj
    assert obj == {"ok": True, "session": SID, "title": "New name", "terminal": False,
                   "why": "no pane: the session is not running"}


def test_rename_auto_names_it_from_the_conversation(server, shelf, signed_in, monkeypatch):
    from agent_media_core import book_tracks
    from agent_media_server import threads

    seen = {}
    monkeypatch.setattr(threads, "auto_title", lambda s: seen.setdefault("s", s) and "Speech bar fixes")
    monkeypatch.setattr(book_tracks, "rename", lambda s, t: t)
    monkeypatch.setattr(send, "send_rename", lambda s, t: None)
    res, obj = call(server, "POST", "/rename", {"session": SID, "auto": True}, AUTH)
    assert res.status == 200, obj
    assert seen["s"] == SID
    assert obj == {"ok": True, "session": SID, "title": "Speech bar fixes", "terminal": True, "why": None}


def test_rename_auto_without_a_name_is_502(server, shelf, signed_in, monkeypatch):
    from agent_media_server import threads

    monkeypatch.setattr(threads, "auto_title", lambda s: "")
    res, obj = call(server, "POST", "/rename", {"session": SID, "auto": True}, AUTH)
    assert res.status == 502 and obj["error"] == "could not think of a name"


def test_auto_title_cleans_the_models_line(monkeypatch):
    from agent_media_core.intake import _summary
    from agent_media_server import threads

    monkeypatch.setattr(threads, "_conversation_text", lambda s: "Person: fix the bar")
    monkeypatch.setattr(_summary, "_chat", lambda *a, **k: 'Title: "Speech bar fixes."\nmore')
    assert threads.auto_title(SID) == "Speech bar fixes"
    monkeypatch.setattr(_summary, "_chat", lambda *a, **k: None)
    assert threads.auto_title(SID) == ""


def test_session_resume_when_live(server, shelf, signed_in):
    res, obj = call(server, "POST", "/session/resume", {"session": SID2}, AUTH)
    assert res.status == 200
    assert obj == {"ok": True, "session": SID2, "pane": "%42", "live": True, "opened": False}


def test_session_close_when_not_live(server, shelf, signed_in):
    res, obj = call(server, "POST", "/session/close", {"session": SID}, AUTH)
    assert res.status == 200
    assert obj == {"ok": True, "session": SID, "live": False, "closed": False}


def test_session_answer_when_nothing_is_asked_is_409(server, shelf, signed_in, monkeypatch, typed):
    monkeypatch.setattr(sessions, "approval_for", lambda pane, agent="claude", session="": None)
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
                         "title", "item", "pos", "dur", "speed", "muted", "target",
                         "replay", "queued"}
    assert obj["live"] is False and obj["session"] is None
    assert obj["replay"] is False and obj["queued"] == []


def test_speech_now_names_a_replay_and_what_waits(server, shelf, signed_in, monkeypatch):
    """A replay is reported as what is heard, under its own session, and a
    reply that arrived meanwhile is listed as waiting rather than playing."""
    replayed, waiting = "6c73498c-02c1-4846-8350-a82006973571", \
        "5f8ca313-c85f-469e-afc7-f3068bc2bfda"
    monkeypatch.setattr(canvas, "speech_state", lambda: {
        "kind": "state", "speaking": True, "session": replayed, "replay": True,
        "sentence": "An older sentence.",
        "queued": [{"session": waiting, "urgent": False, "at": 1790031449.7}]})
    monkeypatch.setattr(sessions, "sessions_index", lambda: [
        {"session": waiting, "title": "Speech bar"},
        {"session": replayed, "title": "Filters"}])
    from agent_media_server import threads
    monkeypatch.setattr(threads, "item_for_session", lambda s, b: (None, False))
    speech._NOW_CACHE.clear()
    speech._TITLE_CACHE.clear()
    res, obj = call(server, "GET", "/speech/now", headers=AUTH)
    assert res.status == 200, obj
    assert obj["session"] == replayed and obj["replay"] is True
    assert obj["sentence"] == "An older sentence."
    assert [keys(q) for q in obj["queued"]] == [{"session", "title", "urgent", "at"}]
    q = obj["queued"][0]
    assert (q["session"], q["title"], q["urgent"], q["at"]) == \
        (waiting, "Speech bar", False, 1790031449.7)
    assert obj["title"] == "Filters"


def test_speech_now_names_the_turn_it_is_on(server, shelf, signed_in, monkeypatch):
    """`turn` keys the player to a log line (`{at, id}`), so a reader can tell
    whether the sentences it holds are the ones being spoken. Without it the
    app has a position and nothing to say what the position is into — which is
    what a line that carries its timeline without being live needs."""
    sid = "6c73498c-02c1-4846-8350-a82006973571"
    monkeypatch.setattr(canvas, "speech_state", lambda: {
        "kind": "state", "speaking": True, "session": sid, "pos": 4.2,
        "turn": {"at": 1790031449.7, "id": 91}})
    monkeypatch.setattr(sessions, "sessions_index", lambda: [
        {"session": sid, "title": "Filters"}])
    from agent_media_server import threads
    monkeypatch.setattr(threads, "item_for_session", lambda s, b: (None, False))
    speech._NOW_CACHE.clear()
    speech._TITLE_CACHE.clear()
    _, obj = call(server, "GET", "/speech/now", headers=AUTH)
    assert obj["turn"] == {"at": 1790031449.7, "id": 91}

    # Nothing playing: no turn to name, and the quiet shape is unchanged.
    monkeypatch.setattr(canvas, "speech_state", lambda: {
        "kind": "state", "speaking": False, "turn": {"at": 1790031449.7}})
    _, quiet = call(server, "GET", "/speech/now", headers=AUTH)
    assert "turn" not in quiet


def test_speech_ctl_says_why_a_replay_failed(server, shelf, signed_in, monkeypatch):
    monkeypatch.setattr(canvas, "_media_ctl", lambda argv, timeout: (
        "error: that reply's audio is no longer on this host (cache cleared)"))
    res, obj = call(server, "POST", "/speech/ctl",
                    {"action": "replay-id", "arg": 9222}, AUTH)
    assert res.status == 200
    assert obj["ok"] is True
    assert obj["error"] == "that reply's audio is no longer on this host (cache cleared)"


def test_speech_ctl_takes_only_listener_verbs(server, shelf, signed_in, typed):
    res, obj = call(server, "POST", "/speech/ctl", {"action": "rm -rf"}, AUTH)
    assert res.status == 400 and obj == {"ok": False, "error": "unknown action"}
    res, obj = call(server, "POST", "/speech/ctl", {"action": "toggle"}, AUTH)
    assert res.status == 200 and obj == {"ok": True, "out": "ok\n"}
    assert [n for n, _ in typed] == ["_media"]


# --- where the audio goes (§6.9) --------------------------------------------------

@pytest.fixture()
def audio_host(monkeypatch):
    """A host with the four speech targets configured from scratch, and
    none of the real env's per-target keys. Nothing is reached: a bridge is
    never probed, and the choice files are under the test's state dir."""
    import os

    from agent_media_server import audio

    for k in list(os.environ):
        if k.startswith(("MEDIA_SPEECH_SOCKET_", "MEDIA_SPEECH_DEVICE_",
                         "MEDIA_REMOTE_SAY_CMD")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "app")
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_APP", "tcp://127.0.0.1:1")
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_APP", "")
    audio._reset_cache()
    yield
    audio._reset_cache()


OPTION_KEYS = {"name", "label", "available", "why"}


def test_audio_targets_shape(server, signed_in, audio_host):
    res, obj = call(server, "GET", "/audio/targets", headers=AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "channels"}
    assert keys(obj["channels"]) == {"speech", "music"}
    sp, mu = obj["channels"]["speech"], obj["channels"]["music"]
    assert keys(sp) == {"current", "default", "overridden", "options"}
    assert (sp["current"], sp["default"], sp["overridden"]) == ("app", "app", False)
    assert [o["name"] for o in sp["options"]] == ["app", "rooms", "local"]
    assert all(keys(o) == OPTION_KEYS for o in sp["options"])
    assert sp["options"][0]["label"] == "Phone (Sasonica)"
    assert keys(mu) == {"current", "next", "overridden", "options"}
    assert all(keys(o) == OPTION_KEYS for o in mu["options"])
    assert res.getheader("Access-Control-Allow-Origin") == "*"


def test_audio_target_sets_and_clears_speech(server, signed_in, audio_host):
    res, obj = call(server, "POST", "/audio/target",
                    {"channel": "speech", "target": "rooms"}, AUTH)
    assert res.status == 200, obj
    assert keys(obj) == {"ok", "channel", "current", "default", "overridden", "options"}
    assert (obj["channel"], obj["current"], obj["overridden"]) == ("speech", "rooms", True)
    from agent_media_core import audio_targets
    assert audio_targets.speech_default() == "rooms"
    _, got = call(server, "GET", "/audio/targets", headers=AUTH)
    assert got["channels"]["speech"]["current"] == "rooms"   # the cache was dropped
    res, obj = call(server, "POST", "/audio/target",
                    {"channel": "speech", "target": None}, AUTH)
    assert res.status == 200 and obj["current"] == "app" and obj["overridden"] is False
    assert audio_targets.speech_default() == "app"


@pytest.mark.parametrize("body", [
    {"channel": "speech", "target": "banana"},
    {"channel": "speech", "target": "phone"},      # known, but not configured here
    {"channel": "speech", "target": 3},
    {"channel": "lights", "target": "rooms"},
    {"channel": "music", "target": "attic"},
])
def test_audio_target_refuses_what_it_cannot_do(server, signed_in, audio_host, body):
    res, obj = call(server, "POST", "/audio/target", body, AUTH)
    assert res.status == 400, obj
    assert obj["ok"] is False and isinstance(obj["error"], str)
    from agent_media_core import audio_targets
    assert audio_targets.speech_override() is None and audio_targets.music_pref() is None


def test_audio_target_sets_the_next_music_play(server, signed_in, audio_host):
    res, obj = call(server, "POST", "/audio/target",
                    {"channel": "music", "target": "rooms"}, AUTH)
    assert res.status == 200, obj
    assert (obj["channel"], obj["next"], obj["overridden"]) == ("music", "rooms", True)


@pytest.mark.parametrize("who,status", [((None, 401), 401), ((None, 0), 503),
                                        (({"username": "guest", "type": "user"}, 200), 403)])
def test_audio_target_is_gated(server, monkeypatch, audio_host, who, status):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda bearer: who)
    res, obj = call(server, "POST", "/audio/target",
                    {"channel": "speech", "target": "rooms"}, AUTH)
    assert res.status == status, obj
    assert obj["ok"] is False
    from agent_media_core import audio_targets
    assert audio_targets.speech_override() is None


def test_a_paired_device_may_choose(server, audio_host):
    from agent_media_server import devices
    code, _ = devices.mint_code("pixel")
    got = devices.redeem(code, "pixel", "127.0.0.1")
    res, obj = call(server, "POST", "/audio/target", {"channel": "speech", "target": "local"},
                    {"Authorization": f"Bearer {got['token']}"})
    assert res.status == 200 and obj["current"] == "local"


def test_speech_now_names_where_it_plays(server, shelf, signed_in, audio_host, monkeypatch):
    monkeypatch.setattr(canvas, "speech_state", lambda: {"kind": "state", "speaking": False})
    _, obj = call(server, "GET", "/speech/now", headers=AUTH)
    assert obj["target"] == "app"                     # quiet: where the next one goes
    from agent_media_core import audio_targets
    audio_targets.set_speech_override("rooms")
    _, obj = call(server, "GET", "/speech/now", headers=AUTH)
    assert obj["target"] == "rooms"


def test_speech_now_while_live_names_the_reply_s_own_target(server, shelf, signed_in,
                                                            audio_host, monkeypatch):
    """Moved mid-reply: the bar says where THIS reply is, not the next."""
    from agent_media_core import audio_targets
    from agent_media_core.state import StateStore
    StateStore().set_now_playing("speech", uri="/tmp/x.mp3", started_at=1.0,
                                 target="app", extras={})
    audio_targets.set_speech_override("rooms")
    monkeypatch.setattr(canvas, "speech_state",
                        lambda: {"kind": "state", "speaking": True, "session": SID})
    _, obj = call(server, "GET", "/speech/now", headers=AUTH)
    assert obj["live"] is True and obj["target"] == "app"


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


def test_a_reply_marks_the_thread_read_unless_kept(server, shelf, signed_in, typed, monkeypatch):
    """A reply ends the thread's reply being read (at its sentence); the reply
    box's Keep reading chip sends `keep_reading` and leaves it playing."""
    from agent_media_server import speech

    seen = []
    monkeypatch.setattr(speech, "reply_read", lambda s, keep=False: seen.append((s, keep)))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    call(server, "POST", "/reply", {"session": SID, "text": "yes"}, AUTH)
    call(server, "POST", "/reply", {"session": SID, "text": "and",
                                    "keep_reading": True}, AUTH)
    assert seen == [(SID, False), (SID, True)]
