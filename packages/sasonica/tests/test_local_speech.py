"""`sasonica install --local-speech`: the Windows speech player's logon task.

schtasks is mocked here; on Windows CI the generated XML is also registered
with the real Task Scheduler (and removed again) to prove it is accepted.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from sasonica import install, update

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _args(**kw):
    d = dict(local_speech="on", dry_run=False)
    d.update(kw)
    return argparse.Namespace(**d)


def _xml(binary="C:\\Users\\A & B\\.local\\bin\\sasonica.exe", user="PC\\ann"):
    return install.speech_task_xml(binary, user)


def test_the_task_runs_the_launcher_headless_and_restarts():
    root = ET.fromstring(_xml().split("\n", 1)[1])     # past the UTF-16 declaration
    ex = root.find("t:Actions/t:Exec", NS)
    assert ex.find("t:Command", NS).text == "conhost.exe"
    assert ex.find("t:Arguments", NS).text == (
        '--headless "C:\\Users\\A & B\\.local\\bin\\sasonica.exe" media-sink-speech')
    rof = root.find("t:Settings/t:RestartOnFailure", NS)
    assert (rof.find("t:Interval", NS).text, rof.find("t:Count", NS).text) == ("PT1M", "999")
    assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == "PC\\ann"
    p = root.find("t:Principals/t:Principal", NS)
    assert p.find("t:LogonType", NS).text == "InteractiveToken"
    assert p.find("t:RunLevel", NS).text == "LeastPrivilege"


class _Recorder:
    def __init__(self, rc=0):
        self.calls, self.xml, self.rc = [], None, rc

    def __call__(self, argv, *, dry_run):
        self.calls.append(argv)
        if "/XML" in argv:
            self.xml = open(argv[argv.index("/XML") + 1], encoding="utf-16").read()
            return self.rc
        return 0


@pytest.fixture
def win(monkeypatch):
    from agent_media_core.entrypoints import sink_speech
    from agent_media_core.sinks import _mpv_ipc as ipc

    monkeypatch.setattr(install, "_windows", lambda: True)
    monkeypatch.setattr(ipc, "_use_pipes", lambda: True)
    monkeypatch.setattr(sink_speech, "find_mpv", lambda: "C:\\mpv\\mpv.exe")
    rec = _Recorder()
    monkeypatch.setattr(install, "_run", rec)
    return rec


def test_on_registers_the_task_from_xml_and_starts_it(win, capsys):
    assert install._local_speech("C:\\bin\\sasonica.exe", _args()) == 0
    verbs = [c[1] for c in win.calls]
    assert verbs == ["/Create", "/End", "/Run"]
    create = win.calls[0]
    assert create[create.index("/TN") + 1] == "Sasonica\\speech" and "/F" in create
    assert "media-sink-speech" in win.xml and "C:\\bin\\sasonica.exe" in win.xml
    out = capsys.readouterr().out
    assert "\\\\.\\pipe\\agent-media-" in out and "sink-speech" in out


def test_without_mpv_it_says_how_and_registers_nothing(win, monkeypatch, capsys):
    from agent_media_core.entrypoints import sink_speech
    monkeypatch.setattr(sink_speech, "find_mpv", lambda: None)
    assert install._local_speech("C:\\bin\\sasonica.exe", _args()) == 0
    assert win.calls == []
    assert "winget install --id shinchiro.mpv -e" in capsys.readouterr().out


def test_a_refused_task_does_not_fail_the_install(win, capsys):
    win.rc = 1
    assert install._local_speech("C:\\bin\\sasonica.exe", _args()) == 0
    assert [c[1] for c in win.calls] == ["/Create"]
    assert "not created" in capsys.readouterr().out


def test_dry_run_prints_the_xml(win, capsys):
    assert install._local_speech("C:\\bin\\sasonica.exe", _args(dry_run=True)) == 0
    assert win.calls == []
    assert "<RestartOnFailure>" in capsys.readouterr().out


def test_off_removes_the_task_and_quits_the_player(win, monkeypatch, capsys):
    from agent_media_core.sinks import _mpv_ipc as ipc
    quits = []
    monkeypatch.setattr(ipc, "endpoint_exists", lambda ep: True)
    monkeypatch.setattr(ipc, "command", lambda ep, *a, **k: quits.append((ep, a)))
    assert install._local_speech("C:\\bin\\sasonica.exe", _args(local_speech="off")) == 0
    assert [c[1] for c in win.calls] == ["/End", "/Delete"]
    assert quits and quits[0][1] == ("quit",)
    assert quits[0][0].startswith("\\\\.\\pipe\\agent-media-")


def test_off_windows_it_is_said_and_nothing_runs(monkeypatch, capsys):
    monkeypatch.setattr(install, "_windows", lambda: False)
    rec = _Recorder()
    monkeypatch.setattr(install, "_run", rec)
    assert install._local_speech("/usr/bin/sasonica", _args()) == 0
    assert rec.calls == [] and "Windows only" in capsys.readouterr().out


def test_update_restarts_the_speech_task_when_there_is_one(monkeypatch):
    ran = []

    def fake_run(argv, **kw):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(update, "_live_chats", lambda: 0)
    monkeypatch.setattr(update.subprocess, "run", fake_run)
    update._restart_tasks(force_sessiond=False)
    assert ["schtasks", "/Run", "/TN", "Sasonica\\speech"] in ran


@pytest.mark.skipif(sys.platform != "win32", reason="the real Task Scheduler")
def test_task_scheduler_accepts_the_xml(tmp_path):
    """Registered for real (under a throwaway name), read back, removed."""
    name = "SasonicaCI\\speech-xml-check"
    xml = install.speech_task_xml(sys.executable, install._task_user())
    path = tmp_path / "task.xml"
    path.write_text(xml, encoding="utf-16")
    r = subprocess.run(["schtasks", "/Create", "/F", "/TN", name, "/XML", str(path)],
                       capture_output=True, text=True)
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        q = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"],
                           capture_output=True, text=True)
        assert q.returncode == 0, q.stderr
        print(q.stdout)
        assert "<RestartOnFailure>" in q.stdout and "media-sink-speech" in q.stdout
    finally:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], capture_output=True)
