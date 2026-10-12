"""Windows' logon tasks — canvas and sessiond as well as speech — registered
from task XML (restart on failure, no time limit), and an install made before
that upgraded in place. schtasks is mocked here; on Windows CI the real Task
Scheduler takes an old-style task and the upgrade replaces it."""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from sasonica import install, update

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class _Recorder:
    def __init__(self):
        self.calls, self.xml = [], {}

    def __call__(self, argv, *, dry_run):
        self.calls.append(argv)
        if "/XML" in argv:
            name = argv[argv.index("/TN") + 1]
            self.xml[name] = open(argv[argv.index("/XML") + 1], encoding="utf-16").read()
        return 0


def _parsed(xml):
    return ET.fromstring(xml.split("\n", 1)[1])


def test_the_services_are_registered_from_xml(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(install, "_run", rec)
    a = argparse.Namespace(bind="0.0.0.0", port=8781, dry_run=False)
    assert install._schtasks("C:\\bin\\sasonica.exe", a) == 0
    assert set(rec.xml) == {"Sasonica\\canvas", "Sasonica\\sessiond"}
    canvas = _parsed(rec.xml["Sasonica\\canvas"])
    assert canvas.find("t:Actions/t:Exec/t:Arguments", NS).text == (
        '--headless "C:\\bin\\sasonica.exe" serve --bind 0.0.0.0 --port 8781')
    assert canvas.find("t:Settings/t:RestartOnFailure/t:Count", NS).text == "999"
    sessiond = _parsed(rec.xml["Sasonica\\sessiond"])
    assert sessiond.find("t:Actions/t:Exec/t:Arguments", NS).text == (
        '--headless "C:\\bin\\sasonica.exe" sessiond')
    # Made (replacing any old registration: /F), then ended and started.
    verbs = [(c[1], c[c.index("/TN") + 1]) for c in rec.calls]
    assert verbs == [("/Create", "Sasonica\\canvas"), ("/End", "Sasonica\\canvas"),
                     ("/Run", "Sasonica\\canvas"), ("/Create", "Sasonica\\sessiond"),
                     ("/End", "Sasonica\\sessiond"), ("/Run", "Sasonica\\sessiond")]
    assert all("/F" in c for c in rec.calls if c[1] == "/Create")


def test_the_speech_task_shares_the_xml():
    a = install.speech_task_xml("C:\\b.exe", "PC\\ann")
    b = install.task_xml('--headless "C:\\b.exe" media-sink-speech', "PC\\ann",
                         "Sasonica speech player: mpv on the speech pipe "
                         "(sasonica install --local-speech)")
    assert a == b


_OLD = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Settings><ExecutionTimeLimit>PT72H</ExecutionTimeLimit></Settings>
  <Actions Context="Author"><Exec>
      <Command>conhost.exe</Command>
      <Arguments>--headless "C:\\Users\\A &amp; B\\sasonica.exe" serve --bind 0.0.0.0 --port 9000</Arguments>
  </Exec></Actions>
</Task>"""


@pytest.mark.parametrize("existing,upgraded", [
    (_OLD, True),
    (_OLD.replace("<Settings>", "<Settings><RestartOnFailure/>"), False),   # done already
    ("", False),                                                            # no such task
    (_OLD.replace("conhost.exe", "notepad.exe"), False),                    # not ours
])
def test_an_old_task_is_upgraded_keeping_what_it_runs(monkeypatch, existing, upgraded):
    rec = _Recorder()
    monkeypatch.setattr(install, "_run", rec)
    monkeypatch.setattr(install, "_task_definition", lambda name: existing)
    assert install.upgrade_task("Sasonica\\canvas", "the canvas") is upgraded
    if upgraded:
        x = _parsed(rec.xml["Sasonica\\canvas"])
        assert x.find("t:Actions/t:Exec/t:Arguments", NS).text == (
            '--headless "C:\\Users\\A & B\\sasonica.exe" serve --bind 0.0.0.0 --port 9000')
        assert x.find("t:Settings/t:RestartOnFailure/t:Interval", NS).text == "PT1M"
    else:
        assert rec.calls == []


def test_update_upgrades_both_services_before_restarting(monkeypatch):
    upgraded, ran = [], []
    monkeypatch.setattr(install, "upgrade_task", lambda name, what: upgraded.append(name))
    monkeypatch.setattr(update, "_live_chats", lambda: 0)
    monkeypatch.setattr(update.subprocess, "run",
                        lambda argv, **kw: ran.append(argv)
                        or subprocess.CompletedProcess(argv, 1))
    update._restart_tasks(force_sessiond=False)
    assert upgraded == ["Sasonica\\canvas", "Sasonica\\sessiond"]
    assert ["schtasks", "/Run", "/TN", "Sasonica\\canvas"] in ran


@pytest.mark.skipif(sys.platform != "win32", reason="the real Task Scheduler")
def test_task_scheduler_takes_the_services_and_upgrades_an_old_one():
    """An old-style task (as installs before task XML made it) is upgraded in
    place, running the same thing; a service's XML is accepted as made."""
    name = "SasonicaCI\\canvas-upgrade-check"
    cmd = install.task_command(sys.executable, "serve", "127.0.0.1", 18999)
    try:
        r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
                            "/TN", name, "/TR", cmd], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        before = install._task_definition(name)
        assert before and "<RestartOnFailure>" not in before
        assert install.upgrade_task(name, "CI: the canvas, upgraded")
        after = install._task_definition(name)
        print(after)
        assert "<RestartOnFailure>" in after and "PT0S" in after
        assert install.task_args(sys.executable, "serve", "127.0.0.1", 18999) in after.replace(
            "&quot;", '"')
        assert not install.upgrade_task(name, "again")          # once only
    finally:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], capture_output=True)
    name = "SasonicaCI\\sessiond-xml-check"
    try:
        xml = install.task_xml(install.task_args(sys.executable, "sessiond"),
                               install._task_user(), "CI: the session holder")
        assert install.register_task(name, xml, dry_run=False) == 0
        assert "<RestartOnFailure>" in install._task_definition(name)
    finally:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], capture_output=True)
