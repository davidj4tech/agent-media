"""Replying to a conversation from the Audiobookshelf player."""

import json
import os

import pytest

from agent_media_server import auth_abs, drafts, panes, routing, send, sessions, threads


# --- who may type -------------------------------------------------------------

def test_root_may_reply_without_configuration(monkeypatch):
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    ok, who = auth_abs.may_reply({"username": "david", "type": "root"})
    assert (ok, who) == (True, "david")


def test_root_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("MEDIA_REPLY_ROOT", "0")
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    ok, _ = auth_abs.may_reply({"username": "david", "type": "root"})
    assert ok is False


def test_admin_is_not_enough(monkeypatch):
    # A library-management role is not a keyboard: admins are named or nothing.
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    ok, why = auth_abs.may_reply({"username": "sam", "type": "admin"})
    assert ok is False and "not allowed" in why


def test_named_user_may_reply(monkeypatch):
    monkeypatch.setenv("MEDIA_REPLY_USERS", " sam , cece ")
    assert auth_abs.may_reply({"username": "cece", "type": "user"})[0] is True
    assert auth_abs.may_reply({"username": "guest", "type": "user"})[0] is False


def test_no_identity_is_a_refusal():
    assert auth_abs.may_reply(None)[0] is False


# --- identity comes from ABS, and is cached -----------------------------------

def test_identity_asks_abs_once_per_ttl(monkeypatch):
    calls = []

    def fake_get(url, bearer, path, method="GET"):
        calls.append((path, method, bearer))
        return {"user": {"username": "david", "type": "root"}}, 200

    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(auth_abs, "_abs_get", fake_get)
    auth_abs._IDENT.clear()
    assert auth_abs.abs_identity("tok")[0]["username"] == "david"
    assert auth_abs.abs_identity("tok")[0]["username"] == "david"
    assert calls == [("/api/authorize", "POST", "tok")]


def test_identity_of_a_bad_token_is_none(monkeypatch):
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    auth_abs._IDENT.clear()
    assert auth_abs.abs_identity("nope") == (None, 401)
    assert auth_abs.abs_identity("") == (None, 401)


def test_a_refusal_is_never_cached(monkeypatch):
    # One transient failure used to refuse every reply for the next minute.
    answers = [(None, 0), ({"user": {"username": "d", "type": "root"}}, 200)]
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    # One server, so the two canned answers line up with the two calls. Without
    # this the test reads the dev machine's real abs-bridge.env: an extra server
    # there consumes an answer of its own and the assertion desyncs.
    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: answers.pop(0))
    auth_abs._IDENT.clear()
    assert auth_abs.abs_identity("tok") == (None, 0)
    assert auth_abs.abs_identity("tok")[0]["username"] == "d"


def test_an_unreachable_abs_is_not_a_401(monkeypatch):
    # A 401 sends the app off to refresh its token, and a failed refresh logs
    # the user out — an outage must never do that.
    assert auth_abs._identity_error(0)["status"] == 503
    assert auth_abs._identity_error(503)["status"] == 503
    assert auth_abs._identity_error(401)["status"] == 401
    assert auth_abs._identity_error(500)["status"] == 502


# --- item → session -----------------------------------------------------------

def _manifests(tmp_path, monkeypatch, rows):
    d = tmp_path / "book-tracks"
    d.mkdir()
    for sid, folder in rows:
        (d / f"{sid}.json").write_text(json.dumps({"session": sid, "folder": folder}))
    monkeypatch.setattr(sessions, "_manifest_dir", lambda: d)


def test_item_resolves_to_its_session(tmp_path, monkeypatch):
    _manifests(tmp_path, monkeypatch,
               [("abc-1", "/home/ryer/conversations/scratch/scratch - Drones")])
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    # ABS reports its own mount; only the <author>/<title> tail is shared.
    monkeypatch.setattr(auth_abs, "_abs_get",
                        lambda *a, **k: ({"path": "/conversations/scratch/scratch - Drones"}, 200))
    assert sessions.session_for_item("item1", "tok") == ("abc-1", "")


def test_an_item_with_no_manifest_is_not_a_conversation(tmp_path, monkeypatch):
    _manifests(tmp_path, monkeypatch, [("abc-1", "/x/scratch/scratch - Drones")])
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: ({"path": "/books/Tolkien/Hobbit"}, 200))
    sid, why = sessions.session_for_item("item1", "tok")
    assert sid is None and "not a conversation" in why


def test_an_item_abs_will_not_show_us_is_refused(monkeypatch):
    # The caller's own bearer does the lookup, so ABS's library permissions
    # decide this for us: no item, no reply.
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 404))
    assert sessions.session_for_item("item1", "tok") == (None, "no such item")


def test_an_unreachable_abs_is_not_a_missing_item(monkeypatch):
    # These were the same message, and they send you to opposite places: one
    # says the library is wrong, the other says the server blinked. Status 0
    # is "could not be reached" (see _abs_get).
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 0))
    sid, why = sessions.session_for_item("item1", "tok")
    assert sid is None and "did not answer" in why and "no such item" not in why


def test_a_broken_abs_says_what_it_said(monkeypatch):
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 500))
    sid, why = sessions.session_for_item("item1", "tok")
    assert sid is None and "500" in why and "no such item" not in why


# --- the message itself --------------------------------------------------------

def test_quote_and_reply_land_on_one_line():
    out = send.compose("try the second one", quote="Short answer: no,\n nothing does")
    assert out == 'Re: "Short answer: no, nothing does" — try the second one'
    assert "\n" not in out


def test_a_long_quote_is_clipped():
    out = send.compose("ok", quote="x" * 500)
    assert len(out) < 200 and out.endswith('…" — ok')


def test_no_quote_is_just_the_text():
    assert send.compose("hello", quote="  ") == "hello"


# --- refusals before anything is typed ----------------------------------------

def test_reply_refuses_an_empty_message():
    ok, detail = send.reply("item1", "   ", "tok")
    assert ok is False and detail["error"] == "empty reply"


def test_reply_refuses_a_user_who_may_not_type(monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "guest", "type": "user"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    typed = []
    monkeypatch.setattr(sessions, "session_for_item",
                        lambda *a, **k: typed.append("looked up") or ("s", ""))
    ok, detail = send.reply("item1", "hi", "tok")
    assert ok is False and "not allowed" in detail["error"]
    assert typed == []  # refused before we go anywhere near a pane


def test_a_session_with_no_transcript_is_not_revived(monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(sessions, "session_for_item", lambda *a, **k: ("gone-1", ""))
    monkeypatch.setattr(sessions, "live_sessions", dict)
    monkeypatch.setattr(sessions, "session_exists", lambda s: False)
    opened = []
    monkeypatch.setattr(send, "open_window", lambda *a, **k: opened.append(1) or ("%1", ""))
    ok, detail = send.reply("item1", "hi", "tok")
    assert ok is False and "no transcript" in detail["error"]
    assert opened == []


# --- the live path, and the revive path ---------------------------------------

@pytest.fixture
def _allowed(monkeypatch):
    # Stub the shelving for EVERY test that gets as far as a successful send.
    # Left real, it renders speech and writes a history row against whatever
    # session id the test invented — on a background thread, so it outlives the
    # tmp-dir monkeypatching — and "sess-1" duly appeared in the real library as
    # a conversation called "You: hi". Tests that care about it override this.
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": None)
    monkeypatch.setattr(send, "_ensure_submitted", lambda p, t, timeout=3.0, agent="claude": True)
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(sessions, "session_for_item", lambda *a, **k: ("sess-1", ""))
    monkeypatch.setattr(sessions, "session_exists", lambda s: True)
    monkeypatch.setattr(sessions, "transcript_cwd", lambda s: "/home/ryer/projects/x")


def test_a_live_session_is_typed_into_directly(monkeypatch, _allowed):
    sent = []
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: sent.append((p, t)) or "")
    monkeypatch.setattr(send, "open_window", lambda *a, **k: pytest.fail("should not revive"))
    ok, detail = send.reply("item1", "hi", "tok", quote="a turn")
    assert ok is True
    assert detail == {"session": "sess-1", "pane": "%7", "opened": False,
                      "submitted": True}
    assert sent == [("%7", 'Re: "a turn" — hi')]


def test_a_dead_session_is_revived_in_a_window(monkeypatch, _allowed):
    opened, sent = [], []

    def fake_open(session, cwd, *, resume, agent="claude"):
        opened.append((session, cwd, resume))
        return "%9", ""

    monkeypatch.setattr(sessions, "live_sessions", dict)
    monkeypatch.setattr(send, "open_window", fake_open)
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: sent.append((p, t)) or "")
    ok, detail = send.reply("item1", "hi", "tok")
    assert ok is True and detail["opened"] is True and detail["pane"] == "%9"
    assert opened == [("sess-1", "/home/ryer/projects/x", True)]
    assert sent == [("%9", "hi")]


def test_a_stale_pane_id_falls_through_to_a_revive(monkeypatch, _allowed):
    # Pane ids get recycled, so a live_sessions hit is still probed.
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(panes, "alive", lambda p: False)
    monkeypatch.setattr(send, "open_window", lambda *a, **k: ("%9", ""))
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    ok, detail = send.reply("item1", "hi", "tok")
    assert ok is True and detail["pane"] == "%9"


def test_a_window_that_never_comes_up_is_reported(monkeypatch, _allowed):
    monkeypatch.setattr(sessions, "live_sessions", dict)
    monkeypatch.setattr(send, "open_window", lambda *a, **k: ("%9", "%9 did not come up"))
    ok, detail = send.reply("item1", "hi", "tok")
    assert ok is False and "did not come up" in detail["error"]


def test_branch_never_resumes_and_seeds_a_fresh_session(monkeypatch, _allowed):
    opened, sent = [], []
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(send, "open_window",
                        lambda s, cwd, *, resume, agent="claude": opened.append(resume) or ("%9", ""))
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: sent.append(t) or "")
    ok, detail = send.reply("item1", "go deeper", "tok", quote="a turn", mode="branch")
    assert ok is True and detail["branched"] is True
    assert opened == [False]                       # a branch is not a resume
    assert sent == ['Re: "a turn" — go deeper']    # ... but it carries the quote


# --- focus --------------------------------------------------------------------

def test_focus_refuses_a_pane_that_is_not_claude(monkeypatch):
    monkeypatch.setattr(panes, "tmux_agent_panes", lambda: [{"pane": "%7"}])
    ok, why = send.focus("%3")
    assert ok is False and "not a live agent pane" in why


def test_focus_walks_the_client_to_the_pane(monkeypatch):
    calls = []
    monkeypatch.setattr(panes, "tmux_agent_panes", lambda: [{"pane": "%7"}])
    monkeypatch.setattr(panes, "_tmux",
                        lambda a, **k: calls.append(a) or ("work" if "session_name" in a[-1]
                                                          else "@2" if "window_id" in a[-1] else ""))
    ok, detail = send.focus("%7")
    assert (ok, detail) == (True, "%7")
    assert ["switch-client", "-t", "work"] in calls
    assert ["select-pane", "-t", "%7"] in calls


# --- "should the app draw a reply box here?" -----------------------------------

def test_conversation_says_yes_for_a_live_one(monkeypatch):
    monkeypatch.setattr(sessions, "suggestion_for", lambda *a, **k: "sp4 is up now too")
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(sessions, "session_for_item", lambda *a, **k: ("sess-1", ""))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(sessions, "session_exists", lambda s: True)
    ok, detail = threads.conversation("item1", "tok")
    assert ok is True
    assert detail == {"session": "sess-1", "live": True, "pane": "%7", "resumable": True,
                      "suggestion": "sp4 is up now too"}


def test_conversation_says_no_to_someone_who_may_not_reply(monkeypatch):
    # The box must not appear where the send would be refused.
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "guest", "type": "user"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    ok, detail = threads.conversation("item1", "tok")
    assert ok is False and "not allowed" in detail["error"]


def test_conversation_says_no_for_an_ordinary_audiobook(monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(sessions, "session_for_item", lambda *a, **k: (None, "not a conversation"))
    ok, _ = threads.conversation("item1", "tok")
    assert ok is False


def test_an_unreachable_abs_does_not_read_as_a_bad_login(monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: (None, 0))
    ok, detail = send.reply("item1", "hi", "tok")
    assert ok is False and detail["status"] == 503
    assert "did not answer" in detail["error"]


def test_a_rejected_token_reads_as_401(monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: (None, 401))
    ok, detail = threads.conversation("item1", "tok")
    assert ok is False and detail["status"] == 401


# --- the listener's own words go on the shelf too -------------------------------

def test_a_sent_reply_is_recorded_as_a_turn(monkeypatch, _allowed):
    recorded = []
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": recorded.append((s, t)))
    ok, _ = send.reply("item1", "try the second one", "tok", quote="a turn")
    assert ok is True
    # The words as typed, not the quoted line that went into the pane: the
    # quote is context for the agent, not something the listener said.
    assert recorded == [("sess-1", "try the second one")]


def test_nothing_is_recorded_when_the_send_fails(monkeypatch, _allowed):
    recorded = []
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "pane is gone")
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": recorded.append(s))
    ok, _ = send.reply("item1", "hi", "tok")
    assert ok is False and recorded == []


def test_a_multi_line_reply_keeps_its_lines_for_a_claude_pane():
    # Claude Code in tmux takes a newline (Alt+Enter, panes.send); the quote
    # still goes in on one line, and blank runs and trailing spaces are tidied.
    out = send.compose("first line  \r\nsecond line\n\n\n\nthird")
    assert out == "first line\nsecond line\n\nthird"
    assert send.compose("a\nb", quote="q\nr") == 'Re: "q r" — a\nb'
    assert send.for_pane(out, "%42", "claude") == out


def test_a_multi_line_reply_is_flattened_where_a_newline_would_submit():
    # Codex, pi, Hermes (unprobed) and herdr (no key-by-key send): a newline
    # typed there would submit half a message and strand the rest.
    body = send.compose("first line\nsecond line")
    assert send.for_pane(body, "%42", "codex") == "first line second line"
    assert send.for_pane(body, "%42", "pi") == "first line second line"
    assert send.for_pane(body, "herdr:w1:p1", "claude") == "first line second line"


def test_recording_a_turn_never_touches_the_real_library(monkeypatch):
    """The guard for the mistake above, stated as a test.

    `_record_turn` spawns a thread, so a test that leaves it real escapes the
    fixture teardown that redirected the state store and the clip cache. It
    wrote four conversations' worth of "hi" into the actual shelf before anyone
    noticed. Everything it needs is resolved *inside* the thread, so the only
    safe rule is that no test calls it for real.
    """
    started = []
    monkeypatch.setattr(send.threading, "Thread",
                        lambda **kw: started.append(kw) or type(
                            "T", (), {"start": lambda self: None})())
    send._record_turn("s", "hi")
    assert started and started[0]["daemon"] is True


# --- more than one Audiobookshelf ---------------------------------------------

def test_one_server_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MEDIA_ABS_URLS", raising=False)
    monkeypatch.setattr(sessions.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://one.example/")
    assert auth_abs.abs_urls() == ["http://one.example"]


def test_extra_servers_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_ABS_URLS", " http://two.example , http://one.example/ ,, ")
    monkeypatch.setattr(sessions.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://one.example")
    # Publishing server first, no duplicates, no empties.
    assert auth_abs.abs_urls() == ["http://one.example", "http://two.example"]


def test_extra_servers_can_live_beside_the_abs_config(monkeypatch, tmp_path):
    monkeypatch.delenv("MEDIA_ABS_URLS", raising=False)
    cfg = tmp_path / ".config" / "agent-media"
    cfg.mkdir(parents=True)
    (cfg / "abs-bridge.env").write_text('ABS_URL=http://one.example\nABS_URLS="http://two.example"\n')
    monkeypatch.setattr(sessions.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://one.example")
    assert auth_abs.abs_urls() == ["http://one.example", "http://two.example"]


def test_identity_tries_each_server_and_remembers_which(monkeypatch):
    auth_abs._IDENT.clear()
    asked = []

    def fake_get(url, bearer, path, method="GET"):
        asked.append(url)
        if url == "http://two.example":
            return {"user": {"username": "david", "type": "root"}}, 200
        return None, 401

    monkeypatch.setattr(auth_abs, "abs_urls",
                        lambda: ["http://one.example", "http://two.example"])
    monkeypatch.setattr(auth_abs, "_abs_get", fake_get)
    user, status = auth_abs.abs_identity("tok")
    assert (user["username"], status) == ("david", 200)
    assert asked == ["http://one.example", "http://two.example"]
    # And the item lookups that follow go to the server that knew them.
    assert auth_abs.abs_home("tok") == "http://two.example"


def test_a_token_no_server_knows_is_a_401(monkeypatch):
    auth_abs._IDENT.clear()
    monkeypatch.setattr(auth_abs, "abs_urls",
                        lambda: ["http://one.example", "http://two.example"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    assert auth_abs.abs_identity("tok") == (None, 401)


def test_a_server_being_down_does_not_hide_a_refusal(monkeypatch):
    # One unreachable, one refusing: the token is still the reason, and 401 is
    # what the app must be told rather than "the server did not answer".
    auth_abs._IDENT.clear()
    seq = iter([(None, 0), (None, 401)])
    monkeypatch.setattr(auth_abs, "abs_urls",
                        lambda: ["http://down.example", "http://two.example"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: next(seq))
    assert auth_abs.abs_identity("tok") == (None, 401)


def test_abs_home_falls_back_to_the_publishing_server(monkeypatch):
    auth_abs._IDENT.clear()
    monkeypatch.setattr(auth_abs, "_abs_url", lambda: "http://one.example")
    assert auth_abs.abs_home("never-seen") == "http://one.example"


# --- the transcript log flags a reply in flight ---------------------------------

def _log_ready(monkeypatch, tmp_path, lines):
    """Everything log_for_item needs, with a canned set of transcript lines."""
    from agent_media_core import book_tracks
    _manifests(tmp_path, monkeypatch,
               [("sess-1", "/home/ryer/conversations/scratch/A talk")])
    monkeypatch.setattr(auth_abs, "abs_identity",
                        lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(sessions, "session_for_item", lambda *a, **k: ("sess-1", ""))
    monkeypatch.setattr(book_tracks, "conversation_log", lambda *a, **k: lines)


def test_log_is_pending_when_the_listener_had_the_last_word(monkeypatch, tmp_path):
    _log_ready(monkeypatch, tmp_path,
               [{"who": "agent", "text": "Hi"}, {"who": "you", "text": "A question"}])
    ok, detail = threads.log_for_item("item1", "tok")
    assert ok is True
    assert detail["pending"] is True


def test_log_is_not_pending_once_the_answer_has_landed(monkeypatch, tmp_path):
    _log_ready(monkeypatch, tmp_path,
               [{"who": "you", "text": "A question"}, {"who": "agent", "text": "An answer"}])
    ok, detail = threads.log_for_item("item1", "tok")
    assert ok is True
    assert detail["pending"] is False


def test_an_empty_log_is_not_pending(monkeypatch, tmp_path):
    _log_ready(monkeypatch, tmp_path, [])
    ok, detail = threads.log_for_item("item1", "tok")
    assert ok is True and detail["pending"] is False


# --- pictures on the transcript -----------------------------------------------
# The spool join itself is the canvas's (packages/visual/tests/test_pictures.py);
# the server only asks the callback it was handed.

def test_a_reply_gets_what_the_picture_lookup_answers(monkeypatch):
    drawn = {"k1": (["/img/img-1.svg"], True), "k2": ([], False)}
    monkeypatch.setattr(threads, "_PICTURES_FOR", lambda key: drawn.get(key, ([], False)))
    lines = [{"who": "agent", "text": "a", "key": "k1"},
             {"who": "you", "text": "b", "key": ""},
             {"who": "agent", "text": "c", "key": "k2"}]
    threads.attach_pictures(lines)
    assert lines[0]["images"] == ["/img/img-1.svg"] and lines[0]["figure"] is True
    assert "images" not in lines[1] and "images" not in lines[2]


def test_no_picture_lookup_means_no_pictures(monkeypatch):
    monkeypatch.setattr(threads, "_PICTURES_FOR", None)
    lines = [{"who": "agent", "text": "a", "key": "k1"}]
    threads.attach_pictures(lines)
    assert lines == [{"who": "agent", "text": "a", "key": "k1"}]


# --- the ghost prompt -----------------------------------------------------------

_GHOST = (
    "\x1b[38;5;246mnew task? \x1b[38;5;153m/clear\x1b[38;5;246m to sav…\x1b[39m\n"
    "\x1b[38;5;244m────────────────────\x1b[39m\n"
    "\x1b[39m❯\u00a0\x1b[2msp4 is up now too\x1b[0m\n"
    "\x1b[38;5;244m────────────────────\x1b[39m\n"
    "  \x1b[38;5;211m⏵⏵ bypass permissions on\x1b[39m\n"
)


def test_ghost_prompt_is_the_dim_run_after_the_glyph(monkeypatch):
    monkeypatch.setattr(sessions, "_capture_pane", lambda pane: _GHOST)
    assert sessions.ghost_prompt("%1") == "sp4 is up now too"


def test_ghost_prompt_wraps_onto_dim_continuation_lines(monkeypatch):
    cap = _GHOST.replace(
        "❯\u00a0\x1b[2msp4 is up now too\x1b[0m\n",
        "❯\u00a0\x1b[2mrebuild the APK and\x1b[0m\n  \x1b[2minstall it on p8a\x1b[0m\n")
    monkeypatch.setattr(sessions, "_capture_pane", lambda pane: cap)
    assert sessions.ghost_prompt("%1") == "rebuild the APK and install it on p8a"


def test_ghost_prompt_is_gone_once_something_is_typed(monkeypatch):
    cap = _GHOST.replace("\x1b[2msp4 is up now too\x1b[0m", "yes do that")
    monkeypatch.setattr(sessions, "_capture_pane", lambda pane: cap)
    assert sessions.ghost_prompt("%1") == ""


def test_ghost_prompt_is_empty_on_a_bare_prompt(monkeypatch):
    cap = _GHOST.replace("\x1b[2msp4 is up now too\x1b[0m", "")
    monkeypatch.setattr(sessions, "_capture_pane", lambda pane: cap)
    assert sessions.ghost_prompt("%1") == ""
    monkeypatch.setattr(sessions, "_capture_pane", lambda pane: "")
    assert sessions.ghost_prompt("%1") == ""


def test_truecolor_params_are_not_mistaken_for_dim():
    runs = sessions._dim_runs("\x1b[38;2;10;20;30mplain\x1b[2m ghost\x1b[22m back")
    assert runs == [("plain", False), (" ghost", True), (" back", False)]


# --- a fresh session from the phone ---------------------------------------------

def _amux(tmp_path, monkeypatch, body):
    d = tmp_path / "amux" / "sessions"
    d.mkdir(parents=True)
    (d / "scratch.env").write_text(body)
    monkeypatch.setenv("CC_HOME", str(tmp_path / "amux"))
    for k in ("MEDIA_ASK_SESSION", "MEDIA_ASK_TMUX", "MEDIA_ASK_CWD", "MEDIA_ASK_FLAGS"):
        monkeypatch.delenv(k, raising=False)


def test_ask_target_copies_the_amux_registration(tmp_path, monkeypatch):
    _amux(tmp_path, monkeypatch,
          '# amux session: scratch\nCC_NAME="scratch"\nCC_DIR="/home/ryer/scratch"\n'
          'CC_FLAGS="--dangerously-skip-permissions"\n')
    assert send.ask_target() == ("amux-scratch", "/home/ryer/scratch",
                                  ["--dangerously-skip-permissions"])


def test_ask_target_parts_can_be_overridden(tmp_path, monkeypatch):
    _amux(tmp_path, monkeypatch, 'CC_DIR="/home/ryer/scratch"\nCC_FLAGS="--yolo"\n')
    monkeypatch.setenv("MEDIA_ASK_TMUX", "1")
    monkeypatch.setenv("MEDIA_ASK_FLAGS", "")
    assert send.ask_target() == ("1", "/home/ryer/scratch", [])


def test_ask_target_without_a_registration_is_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_HOME", str(tmp_path / "nowhere"))
    for k in ("MEDIA_ASK_SESSION", "MEDIA_ASK_TMUX", "MEDIA_ASK_CWD", "MEDIA_ASK_FLAGS"):
        monkeypatch.delenv(k, raising=False)
    host, cwd, flags = send.ask_target()
    assert host == "amux-scratch" and cwd and flags == []


def test_session_of_pane_waits_for_a_live_claude(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("MEDIA_PANE_REGISTRY_DIR", str(tmp_path))
    # A row naming a dead pid is the previous tenant of a recycled pane.
    (tmp_path / "9").write_text("11111111-2222-3333-4444-555555555555 999999999 /x")
    assert sessions.session_of_pane("%9", timeout=0.3) == ""
    # This process is not `claude` either; only a pid-less legacy row is trusted.
    (tmp_path / "9").write_text(f"11111111-2222-3333-4444-555555555555 {os.getpid()} /x")
    assert sessions.session_of_pane("%9", timeout=0.3) == ""
    (tmp_path / "9").write_text("11111111-2222-3333-4444-555555555555")
    assert sessions.session_of_pane("%9", timeout=0.3) == "11111111-2222-3333-4444-555555555555"


def test_open_window_targets_the_host_and_passes_flags(monkeypatch):
    calls = []

    def fake_tmux(argv, timeout=10):
        calls.append(argv)
        return "%4" if argv[0] == "new-window" else ""

    monkeypatch.setattr(panes, "_tmux", fake_tmux)
    monkeypatch.setattr(send, "ensure_host", lambda h, c: True)
    monkeypatch.setattr(send, "pane_ready", lambda p, agent="claude": True)
    monkeypatch.setattr(send, "_claude_bin", lambda name="claude": "/home/ryer/.local/bin/claude")
    monkeypatch.setattr(send, "attached_session", lambda: pytest.fail("host was given"))
    pane, err = send.open_window("", "/home/ryer/scratch", resume=False,
                                  host="amux-scratch", flags=["--dangerously-skip-permissions"])
    assert (pane, err) == ("%4", "")
    nw = next(c for c in calls if c[0] == "new-window")
    assert nw[nw.index("-t") + 1] == "amux-scratch:"
    assert nw[-1] == ('exec env -u ANTHROPIC_API_KEY PATH=/home/ryer/.local/bin:"$PATH" '
                      "/home/ryer/.local/bin/claude --dangerously-skip-permissions")


def test_open_window_refuses_a_host_it_cannot_hold(monkeypatch):
    monkeypatch.setattr(send, "ensure_host", lambda h, c: False)
    pane, err = send.open_window("", "/x", resume=False, host="amux-scratch")
    assert pane == "" and "amux-scratch" in err


def test_hold_client_is_a_no_op_when_someone_is_attached(monkeypatch):
    monkeypatch.setattr(send, "_has_client", lambda h: True)
    monkeypatch.setattr(send.subprocess, "run", lambda *a, **k: pytest.fail("no holder needed"))
    monkeypatch.setattr(send.subprocess, "Popen", lambda *a, **k: pytest.fail("no holder needed"))
    assert send.hold_client("amux-scratch", "/x") is True


def test_hold_client_creates_the_session_attached_in_its_own_unit(monkeypatch):
    seen = {"n": 0}
    runs = []

    def has_client(h):
        seen["n"] += 1
        return seen["n"] > 2      # nobody at first; there once the holder is up

    class Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(send, "_has_client", has_client)
    monkeypatch.setattr(send.shutil, "which", lambda n: "/usr/bin/systemd-run")
    monkeypatch.setattr(send.subprocess, "run", lambda argv, **k: runs.append(argv) or Done())
    monkeypatch.setattr(send.subprocess, "Popen", lambda *a, **k: pytest.fail("in-process holder"))
    send._HOLDERS.clear()
    assert send.hold_client("amux-scratch", "/home/ryer/scratch") is True
    run = next(r for r in runs if r[0] == "systemd-run")
    assert "--unit=agent-media-tmux-hold-amux-scratch" in run
    assert "--setenv=SHELL=/bin/sh" in run and "--setenv=TERM=xterm-256color" in run
    assert run[-3:] == ["-qfc", "tmux new-session -A -s amux-scratch -c /home/ryer/scratch",
                        "/dev/null"]
    assert run[-4] == "script"


def test_hold_client_falls_back_to_an_in_process_holder(monkeypatch):
    seen = {"n": 0}
    spawned = []

    class Proc:
        def poll(self):
            return None

    monkeypatch.setattr(send, "_has_client", lambda h: (seen.__setitem__("n", seen["n"] + 1) or seen["n"] > 2))
    monkeypatch.setattr(send.shutil, "which", lambda n: None)
    monkeypatch.setattr(send.subprocess, "Popen", lambda argv, **k: spawned.append((argv, k)) or Proc())
    send._HOLDERS.clear()
    assert send.hold_client("amux-scratch", "/x") is True
    argv, kw = spawned[0]
    assert argv[0] == "script" and kw["env"]["SHELL"] == "/bin/sh"
    send._HOLDERS.clear()


@pytest.fixture
def _asker(monkeypatch, tmp_path):
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": None)
    monkeypatch.setattr(send, "_settle", lambda p, timeout=5.0: None)
    monkeypatch.setattr(send, "_ensure_submitted", lambda p, t, timeout=3.0, agent="claude": True)
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    _amux(tmp_path, monkeypatch, 'CC_DIR="/home/ryer/scratch"\nCC_FLAGS="--yolo"\n')


def test_ask_opens_a_fresh_window_and_types_the_first_message(monkeypatch, _asker):
    opened, sent, shelved = [], [], []
    monkeypatch.setattr(send, "open_window",
                        lambda s, cwd, *, resume, host="", flags=(), agent="claude": opened.append((s, cwd, resume, host, list(flags))) or ("%9", ""))
    monkeypatch.setattr(sessions, "session_of_pane", lambda p, timeout=10.0, agent="claude": "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: sent.append((p, t)) or "")
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": shelved.append((s, t)))
    ok, detail = send.ask("what is the time", "tok", quote="a turn")
    assert ok is True
    assert detail == {"session": "11111111-2222-3333-4444-555555555555", "pane": "%9",
                      "opened": True, "fresh": True, "tmux": "amux-scratch",
                      "agent": "claude", "submitted": True}
    assert opened == [("", "/home/ryer/scratch", False, "amux-scratch", ["--yolo"])]
    assert sent == [("%9", 'Re: "a turn" — what is the time')]
    assert shelved == [("11111111-2222-3333-4444-555555555555", "what is the time")]


def test_ask_without_a_uuid_still_delivers_but_shelves_nothing(monkeypatch, _asker):
    shelved = []
    monkeypatch.setattr(send, "open_window", lambda *a, **k: ("%9", ""))
    monkeypatch.setattr(sessions, "session_of_pane", lambda p, timeout=10.0, agent="claude": "")
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": shelved.append(1))
    ok, detail = send.ask("hi", "tok")
    assert ok is True and detail["session"] is None and shelved == []


def test_ask_refuses_an_empty_message_before_opening_anything(monkeypatch, _asker):
    monkeypatch.setattr(send, "open_window", lambda *a, **k: pytest.fail("opened"))
    ok, detail = send.ask("   ", "tok")
    assert ok is False and "empty" in detail["error"]


def test_ask_refuses_a_user_who_may_not_type(monkeypatch, _asker):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "guest", "type": "user"}, 200))
    monkeypatch.setattr(send, "open_window", lambda *a, **k: pytest.fail("opened"))
    ok, detail = send.ask("hi", "tok")
    assert ok is False and detail["status"] == 403


def test_ask_reports_a_window_that_never_came_up(monkeypatch, _asker):
    monkeypatch.setattr(send, "open_window", lambda *a, **k: ("%9", "%9 did not come up"))
    ok, detail = send.ask("hi", "tok")
    assert ok is False and "did not come up" in detail["error"]


def test_project_target_is_the_newest_conversation_cwd_still_on_disk(tmp_path, monkeypatch):
    import os
    _manifests(tmp_path, monkeypatch, [
        ("old", "/c/p-agent-media/Old thing"),
        ("new", "/c/p-agent-media/New thing"),
        ("gone", "/c/p-agent-media/Moved away"),
        ("other", "/c/scratch/Elsewhere")])
    d = tmp_path / "book-tracks"
    for i, sid in enumerate(("old", "new", "gone", "other")):
        os.utime(d / f"{sid}.json", (i, i))
    here = tmp_path / "agent-media"
    here.mkdir()
    cwds = {"old": "/nope", "new": str(here), "gone": str(tmp_path / "deleted"), "other": str(tmp_path)}
    monkeypatch.setattr(sessions, "transcript_cwd", lambda s: cwds[s])
    # "gone" is newest but its directory is not there any more; "new" is next.
    assert sessions.project_target("p-agent-media") == ("p-agent-media", str(here))
    assert sessions.project_target("p-nowhere") == ("", "")
    assert sessions.project_target("  ") == ("", "")


def test_ask_in_a_project_opens_there_with_the_ask_flags(monkeypatch, _asker):
    opened = []
    monkeypatch.setattr(sessions, "project_target", lambda p: ("p-x", "/home/ryer/projects/x") if p == "p-x" else ("", ""))
    monkeypatch.setattr(send, "open_window",
                        lambda s, cwd, *, resume, host="", flags=(), agent="claude": opened.append((cwd, host, list(flags))) or ("%9", ""))
    monkeypatch.setattr(sessions, "session_of_pane", lambda p, timeout=10.0, agent="claude": "")
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    ok, detail = send.ask("hi", "tok", project="p-x")
    assert ok and detail["tmux"] == "p-x"
    assert opened == [("/home/ryer/projects/x", "p-x", ["--yolo"])]


def test_ask_in_an_unknown_project_opens_nothing(monkeypatch, _asker):
    monkeypatch.setattr(sessions, "project_target", lambda p: ("", ""))
    monkeypatch.setattr(send, "open_window", lambda *a, **k: pytest.fail("opened"))
    ok, detail = send.ask("hi", "tok", project="p-gone")
    assert ok is False and detail["status"] == 404 and "p-gone" in detail["error"]


def test_conversation_for_session_waits_for_the_item_and_its_tracks(tmp_path, monkeypatch):
    sid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {sid: "%9"})
    monkeypatch.setattr(sessions, "session_exists", lambda s: True)
    monkeypatch.setattr(auth_abs, "abs_home", lambda b: "http://phone-abs")
    _manifests(tmp_path, monkeypatch, [])
    ok, detail = threads.conversation_for_session(sid, "tok")
    assert ok is True and detail["item"] is None and detail["scanning"] is False and detail["live"] is True

    (tmp_path / "book-tracks" / f"{sid}.json").write_text(json.dumps(
        {"session": sid, "folder": "/home/ryer/conversations/scratch/scratch - Time"}))
    rows = {"results": [{"id": "li_42", "path": "/conversations/scratch/scratch - Time",
                         "media": {"numTracks": 0}}]}
    asked = []

    def abs_get(url, bearer, path, **k):
        asked.append((url, bearer, path))
        if path == "/api/libraries":
            return {"libraries": [{"id": "pod", "mediaType": "podcast"}, {"id": "conv", "mediaType": "book"}]}, 200
        return rows, 200

    monkeypatch.setattr(auth_abs, "_abs_get", abs_get)
    ok, detail = threads.conversation_for_session(sid, "tok")
    assert detail["item"] is None and detail["scanning"] is True     # created, no tracks yet
    assert all(u == "http://phone-abs" and b == "tok" for u, b, _ in asked)   # the caller's server
    assert not any("/pod/" in p for _, _, p in asked)                # book libraries only

    rows["results"][0]["media"]["numTracks"] = 5
    ok, detail = threads.conversation_for_session(sid, "tok")
    assert detail["item"] == "li_42" and detail["scanning"] is False


def test_conversation_for_session_rejects_a_non_uuid():
    ok, detail = threads.conversation_for_session("../etc", "tok")
    assert ok is False and detail["status"] == 400


def test_settle_returns_once_the_screen_stops_changing(monkeypatch):
    frames = iter(["a", "b", "c", "c", "d"])
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: next(frames))
    monkeypatch.setattr(send.time, "sleep", lambda s: None)
    send._settle("%1")
    assert next(frames) == "d"        # stopped at the first repeat, not the end


_BOX = "─────\n❯ what is the time today\n─────\n  ⏵⏵ bypass permissions on"
_EMPTY = "❯ \n  ⏵⏵ bypass permissions on"


def _presses(monkeypatch):
    """Capture the Enters. They are pressed by the shared loop in core now."""
    from agent_media_core import conversation as conv
    sent = []
    monkeypatch.setattr(conv, "_press_enter", lambda pane: sent.append(pane) or True)
    monkeypatch.setattr(send.time, "sleep", lambda s: None)
    return sent


def _no_press(monkeypatch):
    from agent_media_core import conversation as conv
    monkeypatch.setattr(conv, "_press_enter", lambda pane: pytest.fail("pressed Enter"))
    monkeypatch.setattr(send.time, "sleep", lambda s: None)


def test_ensure_submitted_presses_enter_when_the_text_is_still_in_the_box(monkeypatch):
    # One press, and the box lets go. The press is believed only once it has
    # been seen to work, so the capture answers what the pane would: the box
    # holds the line until an Enter actually arrives.
    sent = _presses(monkeypatch)
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: _EMPTY if sent else _BOX)
    assert send._ensure_submitted("%1", "what is the time today", timeout=0.01) is True
    assert sent == ["%1"]


def test_ensure_submitted_keeps_pressing_while_the_pane_eats_the_key(monkeypatch):
    # The 2026-09-21 case: a TUI still painting swallows Enter after Enter.
    # The old one-press-and-return left the question typed and unsent with
    # nothing to say so.
    from agent_media_core import conversation as conv
    sent = _presses(monkeypatch)
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: _BOX)
    assert send._ensure_submitted("%1", "what is the time today", timeout=0.01) is False
    assert sent == ["%1"] * conv.SUBMIT_PRESSES


def test_ensure_submitted_counts_the_last_press_that_lands(monkeypatch):
    # Taken on the final press: still a send, not a refusal.
    from agent_media_core import conversation as conv
    sent = _presses(monkeypatch)
    monkeypatch.setattr(sessions, "_capture_pane",
                        lambda p: _EMPTY if len(sent) >= conv.SUBMIT_PRESSES else _BOX)
    assert send._ensure_submitted("%1", "what is the time today", timeout=0.01) is True
    assert len(sent) == conv.SUBMIT_PRESSES


def test_ensure_submitted_leaves_a_working_session_alone(monkeypatch):
    _no_press(monkeypatch)
    monkeypatch.setattr(sessions, "_capture_pane",
                        lambda p: "· ↑ 1.2k tokens · esc to interrupt\n❯ \n  ⏵⏵ bypass permissions on")
    assert send._ensure_submitted("%1", "what is the time today", timeout=0.01) is True


def test_ensure_submitted_trusts_an_empty_box(monkeypatch):
    _no_press(monkeypatch)
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: _EMPTY)
    assert send._ensure_submitted("%1", "what is the time today", timeout=0.01) is True


def test_a_reply_that_never_left_the_box_is_a_refusal(monkeypatch, _allowed):
    # Not shelved: a turn recorded here is a question the conversation shows
    # with an answer that is never coming.
    shelved = []
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    monkeypatch.setattr(send, "_ensure_submitted", lambda p, t, timeout=3.0, agent="claude": False)
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": shelved.append(1))
    ok, detail = send.reply("item1", "hi", "tok")
    assert ok is False and shelved == []
    assert detail["submitted"] is False and detail["status"] == 502
    assert "did not take it" in detail["error"] and "%7" in detail["error"]


def test_an_ask_that_never_left_the_box_is_a_refusal(monkeypatch, _asker):
    shelved = []
    monkeypatch.setattr(send, "open_window",
                        lambda s, cwd, *, resume, host="", flags=(), agent="claude": ("%9", ""))
    monkeypatch.setattr(sessions, "session_of_pane", lambda p, timeout=10.0, agent="claude": "sess-9")
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    monkeypatch.setattr(send, "_ensure_submitted", lambda p, t, timeout=3.0, agent="claude": False)
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": shelved.append(1))
    ok, detail = send.ask("what is the time", "tok")
    assert ok is False and shelved == []
    # The pane is named so the app can offer the one thing that fixes it.
    assert detail["pane"] == "%9" and detail["session"] == "sess-9"


DIM = "\x1b[2m"
OFF = "\x1b[0m"


def _pane_is(monkeypatch, capture):
    monkeypatch.setattr(sessions, "_capture_pane", lambda pane: capture)


def test_a_half_typed_line_is_a_draft(monkeypatch):
    _pane_is(monkeypatch, "❯ what about the")
    assert sessions.pane_draft("%1")


def test_an_empty_box_and_a_ghost_are_not_drafts(monkeypatch):
    _pane_is(monkeypatch, "❯ ")
    assert not sessions.pane_draft("%1")
    _pane_is(monkeypatch, f"❯ {DIM}try the other one{OFF}")
    assert not sessions.pane_draft("%1")


def test_rename_is_typed_into_the_session(monkeypatch):
    monkeypatch.setattr(sessions, "conversation_pane", lambda s: "%1")
    monkeypatch.setattr(sessions, "pane_draft", lambda pane: False)
    sent = []
    monkeypatch.setattr(panes, "send", lambda pane, text: sent.append((pane, text)) or "")
    tmuxed = []
    monkeypatch.setattr(panes, "_tmux", lambda argv, timeout=10: tmuxed.append(argv) or "")
    assert send.send_rename("s1", "A better name") == ""
    assert sent == [("%1", "/rename A better name")]
    # The window follows the pane again: renaming it by hand had turned that off.
    assert tmuxed == [["set-window-option", "-t", "%1", "automatic-rename", "on"]]


def test_rename_waits_rather_than_joining_a_draft(monkeypatch):
    monkeypatch.setattr(sessions, "conversation_pane", lambda s: "%1")
    monkeypatch.setattr(sessions, "pane_draft", lambda pane: True)
    monkeypatch.setattr(panes, "send", lambda pane, text: pytest.fail("typed over a draft"))
    assert send.send_rename("s1", "A name") == "something is being typed there"


def test_rename_of_an_ended_session_is_not_typed_anywhere(monkeypatch):
    monkeypatch.setattr(sessions, "conversation_pane", lambda s: "")
    assert send.send_rename("s1", "A name").startswith("no pane")


def test_a_ghost_that_fits_is_the_suggestion(monkeypatch):
    monkeypatch.setattr(sessions, "ghost_prompt", lambda pane: "sp4 is up now too")
    monkeypatch.setattr(sessions, "_followup", lambda s: {"text": "ours", "key": "k1"})
    assert sessions.suggestion_for("sess-1", "%1", "k1") == "sp4 is up now too"


def test_a_truncated_ghost_gives_way_to_the_followup(monkeypatch):
    monkeypatch.setattr(sessions, "ghost_prompt", lambda pane: "enable the working event …")
    monkeypatch.setattr(sessions, "_followup", lambda s: {"text": "enable the working event hook", "key": "k1"})
    assert sessions.suggestion_for("sess-1", "%1", "k1") == "enable the working event hook"
    # ...but not one written for an earlier reply, and never the cut ghost:
    # the right follow-up lands a few seconds later
    assert sessions.suggestion_for("sess-1", "%1", "k2") == ""
    monkeypatch.setattr(sessions, "_followup", lambda s: None)
    assert sessions.suggestion_for("sess-1", "%1", "k1") == ""
    monkeypatch.setattr(sessions, "_followup", lambda s: {"text": "enable the working event hook", "key": "k1"})
    # ...and the /conversation route, with no log in hand, takes it anyway
    assert sessions.suggestion_for("sess-1", "%1") == "enable the working event hook"


def test_no_pane_still_has_the_followup(monkeypatch):
    monkeypatch.setattr(sessions, "ghost_prompt", lambda pane: (_ for _ in ()).throw(AssertionError("no pane")))
    monkeypatch.setattr(sessions, "_followup", lambda s: {"text": "ours", "key": "k1"})
    assert sessions.suggestion_for("sess-1", "", "k1") == "ours"
    monkeypatch.setattr(sessions, "_followup", lambda s: None)
    assert sessions.suggestion_for("sess-1", "", "k1") == ""


def test_log_carries_no_suggestion_while_pending(monkeypatch, tmp_path):
    _log_ready(monkeypatch, tmp_path,
               [{"who": "agent", "text": "Hi", "key": "k1"}, {"who": "you", "text": "A question"}])
    monkeypatch.setattr(sessions, "suggestion_for", lambda *a, **k: "never")
    ok, detail = threads.log_for_item("item1", "tok")
    assert ok and detail["suggestion"] == ""


def test_log_offers_the_suggestion_for_its_last_reply(monkeypatch, tmp_path):
    _log_ready(monkeypatch, tmp_path,
               [{"who": "you", "text": "A question"}, {"who": "agent", "text": "An answer", "key": "k9"}])
    seen = {}
    def fake(session, pane, last_key=None):
        seen.update(session=session, last_key=last_key)
        return "do the thing"
    monkeypatch.setattr(sessions, "suggestion_for", fake)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    ok, detail = threads.log_for_item("item1", "tok")
    assert ok and detail["suggestion"] == "do the thing"
    assert seen == {"session": "sess-1", "last_key": "k9"}


# --- where the assistant button's words go ---------------------------------------

_IDX = [
    {"session": "aaaaaaaa-1111-2222-3333-444444444444", "title": "Sasonica default digital assistant", "live": True, "pane": "%1"},
    {"session": "bbbbbbbb-1111-2222-3333-444444444444", "title": "scratch - Automated drone videography system", "live": False, "pane": None},
    {"session": "cccccccc-1111-2222-3333-444444444444", "title": "scratch - Drone battery choice", "live": False, "pane": None},
]


def test_resolve_target_new_chat_prefix():
    assert routing.resolve_target("New chat, what is the time", _IDX) == ("new", None, "what is the time")
    assert routing.resolve_target("start a new conversation: hello", _IDX)[0] == "new"


def test_resolve_target_names_a_session_and_strips_the_prefix():
    kind, hit, rest = routing.resolve_target("reply to digital assistant, does the earbud work", _IDX)
    assert kind == "session" and hit["session"].startswith("aaaaaaaa") and rest == "does the earbud work"
    kind, hit, rest = routing.resolve_target("in the videography chat: add a gimbal", _IDX)
    assert kind == "session" and hit["session"].startswith("bbbbbbbb") and rest == "add a gimbal"


def test_resolve_target_needs_no_punctuation_as_dictation_has_none():
    kind, hit, rest = routing.resolve_target("reply to digital assistant does the earbud work", _IDX)
    assert kind == "session" and hit["session"].startswith("aaaaaaaa") and rest == "does the earbud work"
    kind, hit, rest = routing.resolve_target("continue with the videography system chat add a gimbal", _IDX)
    assert kind == "session" and hit["session"].startswith("bbbbbbbb") and rest == "add a gimbal"


def test_resolve_target_a_bare_name_is_a_switch():
    kind, hit, rest = routing.resolve_target("reply to digital assistant", _IDX)
    assert kind == "session" and hit["session"].startswith("aaaaaaaa") and rest == ""
    assert routing.resolve_target("open the drone battery one", _IDX)[0] == "session"


def test_resolve_target_is_not_fooled_by_ordinary_sentences():
    assert routing.resolve_target("what is the weather in Melbourne today", _IDX) == ("", None, "what is the weather in Melbourne today")
    assert routing.resolve_target("tell me a joke", _IDX)[0] == ""
    # A named thing that matches nothing is just a message.
    assert routing.resolve_target("reply to the milkman, two pints please", _IDX)[0] == ""


def test_resolve_target_reports_ambiguity_instead_of_guessing():
    kind, hit, rest = routing.resolve_target("reply to drone, go on", _IDX)
    assert kind == "ambiguous" and {r["session"][:8] for r in hit} == {"bbbbbbbb", "cccccccc"} and rest == "go on"


def test_sessions_index_lists_live_by_pane_title_then_the_shelf(tmp_path, monkeypatch):
    live = "aaaaaaaa-1111-2222-3333-444444444444"
    old = "bbbbbbbb-1111-2222-3333-444444444444"
    monkeypatch.setattr(sessions, "live_sessions", lambda: {live: "%1", "dddddddd-0000-0000-0000-000000000000": "%2"})
    monkeypatch.setattr(sessions, "_pane_titles", lambda: {"%1": "Sasonica default digital assistant"})
    _manifests(tmp_path, monkeypatch, [(live, "/c/scratch/scratch - Same one, live"),
                                       (old, "/c/scratch/scratch - Drones")])
    rows = sessions.sessions_index()
    assert [(r["session"][:8], r["title"], r["live"]) for r in rows] == [
        ("aaaaaaaa", "Sasonica default digital assistant", True),   # live wins, by pane title
        ("bbbbbbbb", "scratch - Drones", False)]                    # %2 has no title: skipped


@pytest.fixture
def _router(monkeypatch, _asker):
    monkeypatch.setattr(sessions, "sessions_index", lambda: _IDX)
    monkeypatch.setattr(threads, "item_for_session", lambda s, b: ("li_9", True))
    monkeypatch.setattr(sessions, "session_exists", lambda s: True)
    sent = {}
    monkeypatch.setattr(send, "deliver", lambda s, body, text: sent.update(session=s, body=body) or (True, {"session": s, "pane": "%1", "opened": False}))
    monkeypatch.setattr(send, "ask", lambda text, bearer, **k: sent.update(fresh=text) or (True, {"session": "new-1", "pane": "%9", "opened": True, "fresh": True}))
    return sent


def test_routed_ask_follows_a_spoken_target(_router):
    ok, d = routing.ask_routed("reply to digital assistant, does the earbud work", "tok")
    assert ok and d["mode"] == "continued" and d["how"] == "spoken" and d["item"] == "li_9"
    assert d["title"] == "Sasonica default digital assistant"
    assert _router == {"session": "aaaaaaaa-1111-2222-3333-444444444444", "body": "does the earbud work"}


def test_routed_ask_spoken_new_chat_beats_player_and_sticky(_router):
    ok, d = routing.ask_routed("new chat, what is the time", "tok",
                             player_item="item1", sticky="cccccccc-1111-2222-3333-444444444444")
    assert ok and d["mode"] == "new" and d["how"] == "spoken" and _router == {"fresh": "what is the time"}


def test_routed_ask_prefers_a_picked_target_over_the_words(_router):
    ok, d = routing.ask_routed("reply to digital assistant, hi", "tok",
                             target="cccccccc-1111-2222-3333-444444444444")
    assert d["how"] == "picked" and _router["session"].startswith("cccccccc")
    assert _router["body"] == "reply to digital assistant, hi"     # picked: the words are not parsed


def test_routed_ask_then_the_player_then_sticky_then_new(_router, monkeypatch):
    monkeypatch.setattr(sessions, "session_for_item", lambda item, b: ("bbbbbbbb-1111-2222-3333-444444444444", "") if item == "playing" else (None, "no"))
    ok, d = routing.ask_routed("go on", "tok", player_item="playing", sticky="cccccccc-1111-2222-3333-444444444444")
    assert d["how"] == "player" and _router["session"].startswith("bbbbbbbb")
    ok, d = routing.ask_routed("go on", "tok", player_item="a book", sticky="cccccccc-1111-2222-3333-444444444444")
    assert d["how"] == "sticky" and _router["session"].startswith("cccccccc")
    ok, d = routing.ask_routed("go on", "tok")
    assert d["mode"] == "new" and d["how"] == "default" and _router["fresh"] == "go on"


def test_routed_ask_sends_nothing_when_the_name_is_ambiguous(_router):
    ok, d = routing.ask_routed("reply to drone, go on", "tok")
    assert ok is False and d["status"] == 300 and len(d["ambiguous"]) == 2 and d["text"] == "go on"
    assert _router == {}


def test_routed_ask_target_new_forces_a_fresh_session(_router):
    ok, d = routing.ask_routed("reply to digital assistant, hi", "tok", target="new", sticky="cccccccc-1111-2222-3333-444444444444")
    assert d["mode"] == "new" and d["how"] == "asked" and _router == {"fresh": "reply to digital assistant, hi"}


def test_routed_ask_passes_the_project_to_a_fresh_session(_router, monkeypatch):
    seen = {}
    monkeypatch.setattr(send, "ask", lambda text, bearer, **k: seen.update(k) or (True, {"session": "new-1"}))
    ok, d = routing.ask_routed("hi", "tok", target="new", project="p-x")
    assert ok and d["mode"] == "new" and seen == {"project": "p-x", "agent": "", "cwd": ""}


def test_routed_ask_passes_a_directory_to_a_fresh_session(_router, monkeypatch):
    # `/targets` hands out directories, not series names; they reach `ask`
    # the same way a project does.
    seen = {}
    monkeypatch.setattr(send, "ask", lambda text, bearer, **k: seen.update(k) or (True, {"session": "new-1"}))
    ok, d = routing.ask_routed("hi", "tok", target="new", cwd="/home/ryer/projects/runlet")
    assert ok and seen["cwd"] == "/home/ryer/projects/runlet"


def test_routed_ask_a_bare_name_switches_and_sends_nothing(_router):
    ok, d = routing.ask_routed("reply to digital assistant", "tok")
    assert ok and d["mode"] == "switched" and d["title"] == "Sasonica default digital assistant" and d["item"] == "li_9"
    assert _router == {}


def test_routed_ask_dry_run_says_where_without_sending(_router):
    ok, d = routing.ask_routed("reply to digital assistant does the earbud work", "tok", dry=True)
    assert ok and d["dry"] is True and d["mode"] == "continued" and d["how"] == "spoken"
    assert d["session"].startswith("aaaaaaaa") and d["text"] == "does the earbud work" and d["item"] == "li_9"
    ok, d = routing.ask_routed("what is the time", "tok", dry=True)
    assert d["mode"] == "new" and d["how"] == "default" and d["session"] is None
    assert _router == {}


# --- resume and close --------------------------------------------------------------

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def _manager(monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(send, "_retag", lambda s: None)


def test_resume_a_live_session_just_says_where(monkeypatch, _manager):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(send, "open_window", lambda *a, **k: pytest.fail("already live"))
    ok, d = send.session_resume(SID, "tok")
    assert ok and d == {"session": SID, "pane": "%7", "live": True, "opened": False}


def test_resume_revives_a_dead_session_in_its_directory(monkeypatch, _manager):
    opened = []
    monkeypatch.setattr(sessions, "live_sessions", dict)
    monkeypatch.setattr(sessions, "session_exists", lambda s: True)
    monkeypatch.setattr(sessions, "transcript_cwd", lambda s: "/home/ryer/scratch")
    monkeypatch.setattr(send, "open_window", lambda s, cwd, *, resume, **k: opened.append((s, cwd, resume)) or ("%9", ""))
    ok, d = send.session_resume(SID, "tok")
    assert ok and d["opened"] is True and d["pane"] == "%9"
    assert opened == [(SID, "/home/ryer/scratch", True)]


def test_resume_refuses_a_session_with_no_transcript(monkeypatch, _manager):
    monkeypatch.setattr(sessions, "live_sessions", dict)
    monkeypatch.setattr(sessions, "session_exists", lambda s: False)
    ok, d = send.session_resume(SID, "tok")
    assert ok is False and d["status"] == 404


def test_close_kills_only_the_pane_hosting_the_session(monkeypatch, _manager):
    killed = []
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    monkeypatch.setattr(panes, "_tmux", lambda argv, timeout=10: killed.append(argv) or "")
    ok, d = send.session_close(SID, "tok")
    assert ok and d["closed"] is True and d["pane"] == "%7"
    assert killed[0] == ["kill-pane", "-t", "%7"]


def test_close_of_a_session_that_is_not_live_is_a_no_op(monkeypatch, _manager):
    monkeypatch.setattr(sessions, "live_sessions", dict)
    monkeypatch.setattr(panes, "_tmux", lambda argv, timeout=10: pytest.fail("nothing to kill"))
    ok, d = send.session_close(SID, "tok")
    assert ok and d["closed"] is False and d["live"] is False


def test_a_draft_survives_the_conversation_being_left(tmp_path, monkeypatch, _manager):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert drafts.draft_read(SID, "tok")[1]["text"] == ""
    ok, d = drafts.draft_write(SID, "half a thought", 1700.0, "tok")
    assert ok and d["at"] == 1700.0
    assert drafts.draft_read(SID, "tok")[1] == {"session": SID, "text": "half a thought", "at": 1700.0}


def test_sending_drops_the_draft_rather_than_holding_an_empty_one(tmp_path, monkeypatch, _manager):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    drafts.draft_write(SID, "half a thought", 1700.0, "tok")
    drafts.draft_write(SID, "", 1800.0, "tok")
    assert drafts.draft_read(SID, "tok")[1]["text"] == ""
    assert not (tmp_path / "agent-media" / "drafts" / f"{SID}.json").exists()


def test_a_draft_keeps_the_writer_clock_and_is_capped(tmp_path, monkeypatch, _manager):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # Not the server's: the app compares this against its own local copy.
    ok, d = drafts.draft_write(SID, "x" * 9000, "nonsense", "tok")
    assert ok and len(d["text"]) == drafts._DRAFT_LIMIT and d["at"] > 0


def test_a_draft_is_gated_like_a_reply(monkeypatch):
    assert drafts.draft_read("../x", "tok")[1]["status"] == 400
    assert drafts.draft_write("../x", "hi", 0, "tok")[1]["status"] == 400
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "guest", "type": "user"}, 200))
    assert drafts.draft_read(SID, "tok")[1]["status"] == 403
    assert drafts.draft_write(SID, "hi", 0, "tok")[1]["status"] == 403


def test_manage_needs_a_session_id_and_a_permitted_user(monkeypatch):
    assert send.session_close("../x", "tok")[1]["status"] == 400
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "guest", "type": "user"}, 200))
    assert send.session_resume(SID, "tok")[1]["status"] == 403


def test_a_reply_into_a_live_pane_checks_its_enter_landed(monkeypatch, _allowed):
    checked = []
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    monkeypatch.setattr(send, "_ensure_submitted",
                        lambda p, t, timeout=3.0, agent="claude": checked.append((p, t)) or True)
    ok, _ = send.reply("item1", "a long message that the TUI is still taking when Enter arrives", "tok")
    assert ok and checked == [("%7", "a long message that the TUI is still taking when Enter arrives")]


def test_a_running_session_is_never_opened_twice(monkeypatch):
    from agent_media_core import claude_sessions

    sid = "fa0c34cf-bab6-44f4-bf00-f254a45b81fe"
    monkeypatch.setattr(claude_sessions, "running", lambda: [(42, sid, "")])
    monkeypatch.setattr(panes, "_tmux", lambda *a, **k: pytest.fail("opened a window"))
    pane, err = send.open_window(sid, "/tmp", resume=True, host="p-agent-media")
    assert pane == "" and "already running outside tmux" in err


# --- Codex and pi ----------------------------------------------------------------

def test_new_codex_chat_names_the_agent():
    kind, hit, rest = routing.resolve_target("new codex chat, check the logs", [])
    assert (kind, hit, rest) == ("new", {"agent": "codex"}, "check the logs")
    assert routing.resolve_target("new chat, hi", [])[:2] == ("new", None)


def test_a_fresh_pi_is_told_its_id_up_front(monkeypatch, _asker):
    opened, shelved = [], []
    monkeypatch.setattr(send, "open_window",
                        lambda s, cwd, *, resume, host="", flags=(), agent="claude":
                        opened.append((s, agent, list(flags))) or ("%9", ""))
    monkeypatch.setattr(sessions, "session_of_pane", lambda *a, **k: pytest.fail("pi's id is known"))
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: "")
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": shelved.append(s))
    ok, detail = send.ask("hi", "tok", agent="pi")
    assert ok and detail["agent"] == "pi"
    sid, agent, flags = opened[0]
    assert agent == "pi" and flags == [] and sessions._UUID.fullmatch(sid)
    assert detail["session"] == sid and shelved == [sid]


def test_a_fresh_codex_is_asked_its_session_after_the_send(monkeypatch, _asker):
    order = []
    monkeypatch.setattr(send, "open_window",
                        lambda s, cwd, *, resume, host="", flags=(), agent="claude": ("%9", ""))
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: order.append("send") or "")
    monkeypatch.setattr(sessions, "session_of_pane",
                        lambda p, timeout=10.0, agent="claude": order.append(agent) or "01a0bb92-2fe2-7252-aa57-b8e7c84394a1")
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": None)
    ok, detail = send.ask("hi", "tok", agent="codex")
    assert ok and order == ["send", "codex"]
    assert detail["session"] == "01a0bb92-2fe2-7252-aa57-b8e7c84394a1"


def test_an_unknown_agent_is_refused(_asker):
    ok, detail = send.ask("hi", "tok", agent="gemini")
    assert ok is False and detail["status"] == 400


def test_open_window_runs_the_agents_own_resume(monkeypatch):
    calls = []
    monkeypatch.setattr(panes, "_tmux", lambda argv, timeout=10: calls.append(argv) or "%4")
    monkeypatch.setattr(send, "pane_ready", lambda p, agent="claude": True)
    monkeypatch.setattr(send, "_claude_bin", lambda name="claude": f"/bin/{name}")
    monkeypatch.setattr(send, "attached_session", lambda: "main")
    from agent_media_core import claude_sessions, harnesses
    monkeypatch.setattr(claude_sessions, "running", list)
    monkeypatch.setattr(harnesses, "running", list)
    sid = "01a0bb92-2fe2-7252-aa57-b8e7c84394a1"
    send.open_window(sid, "/tmp", resume=True, agent="codex")
    send.open_window(sid, "/tmp", resume=True, agent="pi")
    send.open_window(sid, "/tmp", resume=False, agent="pi")
    cmds = [c[-1] for c in calls if c[0] == "new-window"]
    assert cmds[0].endswith(f"/bin/codex resume {sid}")
    assert cmds[1].endswith(f"/bin/pi --session {sid}")
    assert cmds[2].endswith(f"/bin/pi --session-id {sid}")


# --- where a new chat can be opened -------------------------------------------

def _places_world(tmp_path, monkeypatch, rows, live=()):
    """`rows` is [(session, cwd, mtime)]; each gets a manifest and a directory."""
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    cwds = {}
    for sid, name, at in rows:
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        cwds[sid] = str(d)
        f = manifests / f"{sid}.json"
        f.write_text(json.dumps({"session": sid, "folder": f"/conversations/x/{sid}"}))
        os.utime(f, (at, at))
    monkeypatch.setattr(sessions, "_manifest_dir", lambda: manifests)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {s: "%1" for s in live})
    monkeypatch.setattr(sessions, "transcript_cwd", lambda sid: cwds.get(sid, ""))
    return cwds


def test_places_are_the_directories_sessions_ran_in(tmp_path, monkeypatch):
    _places_world(tmp_path, monkeypatch, [
        ("s-1", "agent-media", 100), ("s-2", "runlet", 200)])
    names = [p["name"] for p in sessions.places()]
    assert names == ["runlet", "agent-media"]      # newest first


def test_one_row_per_directory_however_many_talks_it_held(tmp_path, monkeypatch):
    cwds = _places_world(tmp_path, monkeypatch, [
        ("s-1", "agent-media", 100), ("s-2", "runlet", 200)])
    cwds["s-1"] = cwds["s-2"]                      # both ran in runlet
    assert [p["name"] for p in sessions.places()] == ["runlet"]


def test_a_session_running_now_is_newer_than_anything_shelved(tmp_path, monkeypatch):
    _places_world(tmp_path, monkeypatch,
                  [("s-1", "agent-media", 100), ("s-2", "runlet", 9e9)],
                  live=["s-1"])
    assert [p["name"] for p in sessions.places()][0] == "agent-media"


def test_places_stop_at_the_limit(tmp_path, monkeypatch):
    _places_world(tmp_path, monkeypatch, [
        (f"s-{i}", f"dir-{i}", 100 + i) for i in range(10)])
    assert len(sessions.places(limit=3)) == 3
    assert len(sessions.places(limit=0)) == 10


def test_a_directory_no_session_has_run_in_is_refused(tmp_path, monkeypatch):
    # /ask opens a shell where it is told to, so the phone picks from the
    # list the server published, not from anywhere on the disk.
    _places_world(tmp_path, monkeypatch, [("s-1", "agent-media", 100)])
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(auth_abs, "may_reply", lambda u: (True, ""))
    ok, detail = send.ask("hi", "tok", cwd="/etc")
    assert not ok and detail["status"] == 404


def test_a_new_hermes_chat_can_be_asked_for_out_loud():
    # The four agents are all namable in the words the button hears.
    for agent in ("claude", "codex", "pi", "hermes"):
        kind, hit, rest = routing.resolve_target(f"new {agent} chat, what time is it", [])
        assert kind == "new" and hit == {"agent": agent} and rest == "what time is it"
