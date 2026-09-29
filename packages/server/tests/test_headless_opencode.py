"""Headless opencode: sessiond's shared `opencode serve` (sessiond_opencode.py)
and the headless driver, end to end against a fake one.

sessiond runs in-process on a throwaway socket; the server it starts is
`fixtures/fake_opencode.py`, which answers the routes and streams the events
opencode 1.18.33 did in the spike. Nothing here starts a real opencode.
"""

from __future__ import annotations

import json
import sys
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from agent_media_server import driver, permissions, sessiond
from agent_media_server import sessiond_opencode as oc
from agent_media_server.driver import headless as hd

FAKE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


def runnable(script: Path, where: Path) -> str:
    """The fake as a program: itself on POSIX (its #!), a .cmd that runs it
    with this Python on Windows, which cannot run a .py by name."""
    if os.name != "nt":
        return str(script)
    wrapper = where / (script.stem + ".cmd")
    wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    return str(wrapper)


def wait_for(pred, timeout: float = 8.0, step: float = 0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        time.sleep(step)
    raise AssertionError("timed out waiting")


@pytest.fixture(params=[pytest.param("unix", marks=pytest.mark.skipif(
    os.name == "nt", reason="no unix sockets on Windows")), "tcp"])
def host(request, monkeypatch, tmp_path):
    # AF_UNIX paths are short: /tmp, where there is one.
    sockdir = tempfile.mkdtemp(prefix="sdo", dir=None if os.name == "nt" else "/tmp")
    sock = Path(sockdir) / "s.sock"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("MEDIA_HEADLESS", "1")
    # Both transports: a unix socket, and Windows's loopback port + token.
    monkeypatch.setenv("MEDIA_SESSIOND_TRANSPORT", request.param)
    monkeypatch.setenv("MEDIA_SESSIOND_SOCKET", str(sock))
    monkeypatch.setenv("MEDIA_SESSIOND_OPENCODE", runnable(FAKE, tmp_path))
    monkeypatch.setenv("MEDIA_SESSIOND_OPENCODE_START", "15")
    monkeypatch.setenv("FAKE_OPENCODE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("MEDIA_AUTO_TITLE", "0")
    monkeypatch.setattr(sessiond, "mem_available_mb", lambda: None)
    from agent_media_server import send

    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": None)
    driver._reset_for_tests()
    sup = sessiond.Supervisor()
    srv, _t = sessiond.serve(sock, sup, tick=3600)
    ns = type("Host", (), {})()
    ns.sup, ns.work, ns.log = sup, work, tmp_path / "fake.log"
    yield ns
    try:
        sessiond.stop(srv)
    except Exception:  # noqa: BLE001
        pass
    sup.oc.stop()
    shutil.rmtree(sockdir, ignore_errors=True)
    driver._reset_for_tests()


def requests(h) -> list[tuple[str, str, object]]:
    out = []
    for ln in h.log.read_text().splitlines():
        method, path, body = ln.split(" ", 2)
        out.append((method, path, json.loads(body)))
    return out


def start(h, text, **kw):
    ok, d = driver.headless_driver().start(agent="opencode", cwd=str(h.work), text=text,
                                          host="p-work", **kw)
    assert ok, d
    return d["session"]


def state(h, sid) -> str:
    return h.sup.get(sid)["state"]


def test_a_new_opencode_chat_is_headless_in_one_shared_server(host):
    assert driver.for_new("opencode").kind == driver.HEADLESS
    a = start(host, "hello", model="opencode/mimo-v2.6-flash-free")
    b = start(host, "again")
    assert a.startswith("ses_") and b.startswith("ses_") and a != b
    wait_for(lambda: state(host, a) == "waiting" and state(host, b) == "waiting")
    # One server for both, and both live on it.
    assert host.sup.sessions[a].proc is host.sup.sessions[b].proc is host.sup.oc.proc
    rec = sessiond.read_record(a)
    assert rec["agent"] == "opencode" and rec["workspace"] == "p-work"
    assert driver.for_session(a).kind == driver.HEADLESS
    reqs = requests(host)
    made = [r for r in reqs if r[0] == "POST" and r[1].startswith("/session?")]
    from urllib.parse import quote

    assert f"directory={quote(str(host.work), safe='')}" in made[0][1]
    # Strict: everything asks, reading does not (opencode takes the last match).
    rules = made[0][2]["permission"]
    assert rules[0] == {"permission": "*", "pattern": "*", "action": "ask"}
    assert {"permission": "read", "pattern": "*", "action": "allow"} in rules
    prompt = next(r for r in reqs if r[1].startswith(f"/session/{a}/prompt_async"))
    assert prompt[2]["model"] == {"providerID": "opencode", "modelID": "mimo-v2.6-flash-free"}
    assert prompt[2]["parts"] == [{"type": "text", "text": "hello"}]


def test_normal_permissions_leave_opencodes_own_rules(host, monkeypatch):
    monkeypatch.setenv("MEDIA_HEADLESS_PERMISSIONS", permissions.NORMAL)
    start(host, "hi")
    made = next(r for r in requests(host) if r[0] == "POST" and r[1].startswith("/session?"))
    assert "permission" not in (made[2] or {})


def test_a_permission_is_the_same_card_and_allow_runs_it(host):
    sid = start(host, "tool: echo hi")
    wait_for(lambda: state(host, sid) == "approval")
    appr = driver.headless_driver().approval(sid)
    assert appr["kind"] == "tool" and appr["tool"] == "Bash" and appr["agent"] == "opencode"
    assert appr["question"] == "Allow Bash: echo hi?"
    ok, d = driver.headless_driver().answer(sid, {"request_id": appr["id"], "decision": "allow"})
    assert ok, d
    wait_for(lambda: state(host, sid) == "waiting")
    reply = next(r for r in requests(host) if r[1].startswith(f"/permission/{appr['id']}/reply"))
    assert reply[2] == {"reply": "once"}


def test_deny_is_reject_with_the_message_and_numbered_answers_work(host):
    sid = start(host, "tool: rm -rf x")
    wait_for(lambda: state(host, sid) == "approval")
    appr = driver.headless_driver().approval(sid)
    ok, d = driver.headless_driver().answer(sid, {"choice": 2, "key": appr["key"]})
    assert ok, d
    wait_for(lambda: state(host, sid) == "waiting")
    reply = next(r for r in requests(host) if r[1].startswith(f"/permission/{appr['id']}/reply"))
    assert reply[2] == {"reply": "reject", "message": hd.DENY_MESSAGE}


def test_a_question_is_answered_by_label(host):
    sid = start(host, "ask")
    wait_for(lambda: state(host, sid) == "approval")
    appr = driver.headless_driver().approval(sid)
    assert appr["kind"] == "question" and appr["question"] == "Which colour?"
    ok, d = driver.headless_driver().answer(sid, {"choice": 2, "key": appr["key"]})
    assert ok, d
    wait_for(lambda: state(host, sid) == "waiting")
    reply = next(r for r in requests(host) if r[1].startswith(f"/question/{appr['id']}/reply"))
    assert reply[2] == {"answers": [["Blue"]]}


def test_a_message_typed_over_a_permission_declines_it_then_runs(host):
    sid = start(host, "tool: make")
    wait_for(lambda: state(host, sid) == "approval")
    rid = host.sup.get(sid)["pending"][0]["request_id"]
    ok, d = driver.headless_driver().send(sid, "no, do this instead", "no, do this instead")
    assert ok, d
    reqs = requests(host)
    reply = next(r for r in reqs if r[1].startswith(f"/permission/{rid}/reply"))
    assert reply[2]["reply"] == "reject" and reply[2]["message"] == sessiond.TYPED_INSTEAD
    wait_for(lambda: state(host, sid) == "waiting")


def test_interrupt_aborts_the_turn(host):
    sid = start(host, "slow")
    wait_for(lambda: state(host, sid) == "working")
    ok, d = driver.headless_driver().interrupt(sid)
    assert ok and d["interrupted"], d
    assert any(r[1].startswith(f"/session/{sid}/abort") for r in requests(host))
    wait_for(lambda: state(host, sid) == "waiting")


def test_parking_the_last_chat_stops_the_server_and_a_message_brings_it_back(host):
    sid = start(host, "hi")
    wait_for(lambda: state(host, sid) == "waiting")
    first = host.sup.oc.proc
    host.sup.park(sid)
    wait_for(lambda: not host.sup.oc.up)
    assert state(host, sid) == "parked" and not host.sup.get(sid)["live"]
    ok, d = driver.headless_driver().send(sid, "back", "back")
    assert ok and d["opened"], d
    assert host.sup.oc.up and host.sup.oc.proc is not first
    wait_for(lambda: state(host, sid) == "waiting")


def test_close_detaches_and_rename_patches_the_title(host):
    sid = start(host, "hi")
    wait_for(lambda: state(host, sid) == "waiting")
    assert driver.headless_driver().rename(sid, "My chat") == ""
    patch = next(r for r in requests(host) if r[0] == "PATCH")
    assert patch[1].startswith(f"/session/{sid}?") and patch[2] == {"title": "My chat"}
    ok, d = driver.headless_driver().close(sid)
    assert ok and d["closed"]
    assert state(host, sid) == "closed"
    wait_for(lambda: not host.sup.oc.up)


def test_the_model_chip_changes_the_next_prompts_model(host):
    from agent_media_server import session_settings

    sid = start(host, "hi")
    wait_for(lambda: state(host, sid) == "waiting")
    st = session_settings.state(sid)
    assert st["agent"] == "opencode" and st["driver"] == driver.HEADLESS
    assert st["can"] == {"model": True, "plan": False}
    ok, d = driver.headless_driver().configure(sid, model="opencode/other-free")
    assert ok and d["told"]
    driver.headless_driver().send(sid, "next", "next")
    last = [r for r in requests(host) if r[1].startswith(f"/session/{sid}/prompt_async")][-1]
    assert last[2]["model"] == {"providerID": "opencode", "modelID": "other-free"}


def test_the_server_needs_its_password(host):
    import urllib.error
    import urllib.request

    sid = start(host, "hi")
    wait_for(lambda: state(host, sid) == "waiting")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{host.sup.oc.port}/global/health", timeout=5)
    assert e.value.code == 401


# --- the translations, alone ----------------------------------------------------------

def test_question_answers_split_joined_labels_and_keep_free_text():
    qs = [{"question": "A?", "options": [{"label": "x"}, {"label": "y, z"}]},
          {"question": "B?", "options": [{"label": "p"}, {"label": "q"}]},
          {"question": "C?", "options": [{"label": "p"}]}]
    got = oc.question_answers(qs, {"A?": "y, z", "B?": "p, q", "C?": "my own words"})
    assert got == [["y, z"], ["p", "q"], ["my own words"]]


def test_model_ref_needs_a_provider(monkeypatch):
    assert oc.model_ref("opencode/big-pickle") == {"providerID": "opencode", "modelID": "big-pickle"}
    assert oc.model_ref("") is None
    monkeypatch.setenv("MEDIA_HEADLESS_OPENCODE_MODEL", "p/m")
    assert oc.model_ref("") == {"providerID": "p", "modelID": "m"}


def test_edit_and_webfetch_requests_read_as_their_tools():
    edit = oc.request_of("permission", {"permission": "edit", "patterns": ["a.py"],
                                        "metadata": {"filepath": "/w/a.py"}})
    assert edit["tool_name"] == "Edit" and edit["input"] == {"file_path": "/w/a.py"}
    fetch = oc.request_of("permission", {"permission": "webfetch", "patterns": ["https://x"],
                                         "metadata": {}})
    assert fetch["tool_name"] == "WebFetch" and fetch["input"] == {"url": "https://x"}


def test_an_older_sessiond_without_opencode_opens_a_pane(monkeypatch):
    """The canvas restarts after a pull, sessiond only when its own code does:
    until then it refuses opencode, and the chat opens where it used to."""
    monkeypatch.setattr(hd, "call", lambda op, **kw: {"ok": False, "code": "unsupported",
                                                      "error": "no headless adapter for opencode"})
    seen = {}

    class Pane:
        def start(self, **kw):
            seen.update(kw)
            return True, {"session": "ses_x", "pane": "%1"}

    monkeypatch.setattr(driver, "pane_driver", lambda: Pane())
    ok, d = hd.HeadlessDriver().start(agent="opencode", cwd="/w", text="hi", host="p-w")
    assert ok and d["pane"] == "%1" and seen["agent"] == "opencode"
