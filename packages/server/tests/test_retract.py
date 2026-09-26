"""`POST /session/retract` (server-contract.md §6.19, retract.py).

Pane sessions: the screen is faked (`activity_of`, `_capture_pane`), the keys
are recorded, never pressed. Headless sessions: the real sessiond and the fake
claude of test_headless.
"""

from __future__ import annotations

from agent_media_server import retract, send, sessions
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401 — fixtures
from test_headless import app_host, host, last_text, start, wait_for  # noqa: F401 — fixtures
from test_stop import OTHER, SID, pane, voice  # noqa: F401 — fixtures


def retract_(server, body):
    res, obj = call(server, "POST", "/session/retract", body, AUTH)
    return res.status, obj


def keys_of(keys):
    return [k[-1] for k in keys if k[:1] == ["send-keys"]]


def test_a_working_pane_is_interrupted_and_the_message_marked(server, signed_in, pane, voice,
                                                              monkeypatch):
    screen, keys = pane
    screen[:] = ["working", "working", "waiting"]
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: "❯ \n  esc to interrupt")
    st, body = retract_(server, {"session": SID, "id": "u-1", "text": "make it blue"})
    assert st == 200, body
    assert body["retracted"] == {"id": "u-1", "text": "make it blue"}
    assert body["interrupted"] is True
    # Nothing queued on screen: Escape alone, no Up.
    assert keys_of(keys) == ["Escape"]
    msgs = [{"id": "u-1", "role": "user", "at": 1.0, "parts": [{"type": "text", "text": "make it blue"}]},
            {"id": "a-1", "role": "assistant", "at": 2.0, "parts": []}]
    retract.mark(SID, msgs)
    assert msgs[0].get("retracted") is True and "retracted" not in msgs[1]
    retract.mark(OTHER, msgs[:0])


def test_a_queued_message_in_a_pane_is_taken_back_before_the_escape(server, signed_in, pane,
                                                                    voice, monkeypatch):
    screen, keys = pane
    screen[:] = ["working", "working", "waiting"]
    # The queue hint, then two lines of words in the composer: a Ctrl-U each.
    screens = ["story…\n❯ Press up to edit queued messages",
               "❯ now say banana please", "❯ now say banana please", "❯ "]
    monkeypatch.setattr(sessions, "_capture_pane",
                        lambda p: screens.pop(0) if len(screens) > 1 else screens[0])
    st, body = retract_(server, {"session": SID, "text": "now say banana please"})
    assert st == 200 and body["interrupted"] is True
    assert keys_of(keys) == ["Up", "C-u", "C-u", "Escape"]


def test_an_idle_session_is_only_marked_and_the_next_reply_says_so(server, signed_in, pane,
                                                                   voice, typed):
    screen, keys = pane
    st, body = retract_(server, {"session": SID, "text": "delete the “old” branch"})
    assert st == 200 and body["interrupted"] is False and keys == []
    ok, _ = send.reply("", "keep it", "tok", session=SID)
    sent = [a for n, a in typed if n == "_send_to_pane"]
    assert sent and sent[-1][1].startswith('(I took back my last message, “delete the "old" branch”')
    assert sent[-1][1].endswith("keep it")
    # Owed once: the reply after carries no note.
    send.reply("", "and again", "tok", session=SID)
    assert [a for n, a in typed if n == "_send_to_pane"][-1][1] == "and again"
    # Read back, the bubble loses the note; a message by its words is marked.
    msgs = [{"id": "u-9", "role": "user", "at": 0.0,
             "parts": [{"type": "text", "text": "delete the “old” branch"}]},
            {"id": "u-10", "role": "user", "at": 9e12,
             "parts": [{"type": "text", "text": sent[-1][1]}]}]
    retract.mark(SID, msgs)
    assert msgs[0].get("retracted") is True
    assert msgs[1]["parts"][0]["text"] == "keep it" and not msgs[1].get("retracted")


def test_bad_requests(server, signed_in, pane, voice):
    assert retract_(server, {"session": "nope", "text": "x"})[0] == 400
    assert retract_(server, {"session": SID})[0] == 400
    assert retract_(server, {"session": OTHER, "text": "x"})[0] == 404


def test_a_working_headless_session_drops_its_queue(app_host, server, signed_in, typed, voice):
    from agent_media_server import driver

    sid = start(app_host, "slow: 20")
    wait_for(lambda: any(e.get("subtype") == "task_started"
                         for e in app_host.sup.sessions[sid].events))
    driver.headless_driver().send(sid, "", "reply: plum")
    st, body = retract_(server, {"session": sid, "text": "reply: plum"})
    assert st == 200 and body["interrupted"] is True, body
    # The queued message did not run as its own turn.
    ok, _ = driver.headless_driver().send(sid, "", "reply: mango")
    wait_for(lambda: last_text(app_host, sid) == "mango")
    texts = [e for e in app_host.sup.sessions[sid].events if e.get("type") == "result"]
    assert not any("plum" in str(e.get("result") or "") for e in texts)
    assert not [t for t in typed if t[0] in ("_tmux", "panes.send")]
