"""`media session list|send` (headless_mail.py): another session reaching a
headless one through sessiond, against an in-process sessiond on a
throwaway socket and the fake `claude` (as test_headless.py, whose fixture is
copied rather than imported, so its isolation is this file's own)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agent_media_server import driver, headless_mail as hm, sessiond

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="unix sockets")


def runnable(script: Path, where: Path) -> str:
    return str(script) if os.access(script, os.X_OK) else f"{sys.executable} {script}"


def wait_for(pred, timeout: float = 8.0, step: float = 0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        time.sleep(step)
    raise AssertionError("timed out waiting")


@pytest.fixture
def host(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    for k, sub in (("HOME", ""), ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
                   ("XDG_CONFIG_HOME", "config")):
        d = home / sub if sub else home
        d.mkdir(exist_ok=True)
        monkeypatch.setenv(k, str(d))
    sockdir = tempfile.mkdtemp(prefix="sm", dir="/tmp")
    sock = Path(sockdir) / "s.sock"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("MEDIA_HEADLESS", "1")
    monkeypatch.setenv("MEDIA_SESSIOND_TRANSPORT", "unix")
    monkeypatch.setenv("MEDIA_SESSIOND_SOCKET", str(sock))
    monkeypatch.setenv("MEDIA_SESSIOND_CLAUDE", runnable(FAKE, tmp_path))
    monkeypatch.setenv("MEDIA_SESSIOND_CLOSE_GRACE", "3")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_CLAUDE_TICK", "0.01")
    monkeypatch.setenv("MEDIA_AUTO_TITLE", "0")
    monkeypatch.setattr(sessiond, "mem_available_mb", lambda: None)
    from agent_media_server import send

    turns: list = []
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": turns.append((s, t, p)))
    monkeypatch.setattr(hm, "POLL_S", 0.05)
    driver._reset_for_tests()
    sup = sessiond.Supervisor()
    srv, _t = sessiond.serve(sock, sup, tick=3600)
    ns = type("Host", (), {})()
    ns.sup, ns.srv, ns.work, ns.turns = sup, srv, work, turns
    yield ns
    try:
        sessiond.stop(srv)
    except Exception:  # noqa: BLE001
        pass
    for s in sup.sessions.values():
        if s.proc is not None and s.proc.poll() is None:
            s.proc.kill()
    shutil.rmtree(sockdir, ignore_errors=True)
    driver._reset_for_tests()


def start(h, text) -> str:
    ok, d = driver.headless_driver().start(agent="claude", cwd=str(h.work), text=text,
                                          host="p-work")
    assert ok, d
    return d["session"]


def state(h, sid) -> str:
    return h.sup.get(sid)["state"]


def results(h, sid) -> list[str]:
    return [e.get("result") for e in h.sup.sessions[sid].events if e.get("type") == "result"]


def run(capsys, *argv) -> tuple[int, dict]:
    rc = hm.main(list(argv) + ["--json"])
    out = capsys.readouterr().out.strip().splitlines()
    return rc, (json.loads(out[-1]) if out else {})


def test_list_shows_the_live_session_with_its_folder(host, capsys):
    sid = start(host, "reply: hi")
    wait_for(lambda: state(host, sid) == "waiting")
    rc, d = run(capsys, "list")
    assert rc == 0 and d["sessiond"] is True
    (row,) = d["sessions"]
    assert row["session"] == sid and row["live"] and row["cwd"] == str(host.work)
    assert row["card"] is False and row["state"] == "waiting"
    rc, d = run(capsys, "list", "--cwd", str(host.work / "elsewhere"))
    assert d["sessions"] == []


def test_send_by_folder_reaches_it_marked_from_the_sender_and_is_not_davids_turn(host, capsys):
    sid = start(host, "reply: hi")
    wait_for(lambda: state(host, sid) == "waiting")
    host.turns.clear()
    rc, d = run(capsys, "send", str(host.work), "--from", "agent-media-92", "reply:", "got it")
    assert rc == 0 and d["ok"] and d["session"] == sid and d["queued"] is False
    wait_for(lambda: "ok: [From Agent-media-92] reply: got it" in results(host, sid))
    assert host.turns == []          # another session's words, not the listener's


def test_a_session_mid_turn_takes_it_after_the_turn(host, capsys):
    sid = start(host, "slow: 1")
    wait_for(lambda: state(host, sid) == "working")
    rc, d = run(capsys, "send", sid[:8], "reply: later")
    assert rc == 0 and d["queued"] is True
    # It joins the running turn at its next tool boundary (fake_claude: one
    # result, a line per message), never interrupting it.
    wait_for(lambda: any("later" in (r or "").splitlines() for r in results(host, sid)),
             timeout=10)


def test_a_card_up_is_never_answered_and_wait_sends_once_it_is(host, capsys):
    sid = start(host, "tool: touch a")
    wait_for(lambda: state(host, sid) == "approval")
    rc, d = run(capsys, "send", sid, "reply: hello")
    assert rc == 4 and d["code"] == "pending"
    assert state(host, sid) == "approval" and host.sup.get(sid)["pending"]
    assert "hello" not in results(host, sid)

    rid = host.sup.get(sid)["pending"][0]["request_id"]

    def answer():
        time.sleep(0.3)
        host.sup.answer(sid, rid, {"behavior": "allow", "updatedInput": {}})

    threading.Thread(target=answer, daemon=True).start()
    rc, d = run(capsys, "send", sid, "--wait", "8", "reply: hello")
    assert rc == 0 and d["ok"]
    wait_for(lambda: "hello" in results(host, sid), timeout=10)


def test_sessiond_refuses_keep_pending_atomically(host):
    sid = start(host, "tool: touch b")
    wait_for(lambda: state(host, sid) == "approval")
    with pytest.raises(sessiond.Refused) as e:
        host.sup.send(sid, "reply: x", keep_pending=True)
    assert e.value.code == "pending"
    assert host.sup.get(sid)["pending"]             # the card is still there


def test_no_live_session_in_the_folder_and_bad_ids(host, capsys, tmp_path):
    rc, d = run(capsys, "send", str(tmp_path / "nobody"), "hello")
    assert rc == 5 and "no live headless session" in d["error"]
    rc, d = run(capsys, "send", "abc", "hello")
    assert rc == 5 and "8 characters" in d["error"]


def test_sessiond_down_is_rc_1(host, capsys):
    sessiond.stop(host.srv)
    rc, d = run(capsys, "send", str(host.work), "hello")
    assert rc == 1 and d["code"] == "down"
