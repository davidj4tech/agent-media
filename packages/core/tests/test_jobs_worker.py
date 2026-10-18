"""This host as a jobs worker (jobs_worker.py), and Windows tasks as services.

Installing the worker for each init, in tmp dirs with the service managers
faked: systemd --user (a generated unit, enabled --now), runit (the template
linked), Windows (a logon task), the phone (left alone). Then the profile row,
and restart_services' Task Scheduler backend (schtasks faked).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

from agent_media_core import jobs_worker, restart_services as rs, setup


@pytest.fixture()
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_worker, "config_path", lambda: tmp_path / "jobs-worker.json")
    monkeypatch.setattr(setup, "services_dir", lambda: None)
    monkeypatch.setattr(setup, "systemd_user_dir", lambda: tmp_path / "systemd")
    ran = []
    monkeypatch.setattr(jobs_worker, "_systemctl", lambda *a: ran.append(a) or 0)
    monkeypatch.setattr(jobs_worker, "_schtasks", lambda *a: ran.append(("schtasks",) + a) or 0)
    return tmp_path, ran


def _paired(tmp_path):
    (tmp_path / "jobs-worker.json").write_text(
        '{"server": "https://red5.sasonica.com", "token": "t", "name": "pn jobs"}')


def test_the_template_is_a_linkable_service_with_its_worker():
    tpl = jobs_worker.template_dir()
    assert (tpl / "run").is_file() and jobs_worker.script().is_file()
    assert subprocess.run(["sh", "-n", str(tpl / "run")]).returncode == 0
    assert "--generic" in (tpl / "run").read_text()
    # Off every host that is not paired: install-services' default leaves it.
    assert setup.service_wanted("jobs-worker", None)[0] is False
    # The phone's phone-jobs.py is the same file.
    phone = tpl.parents[3] / "deploy/phone/service/phone-jobs/phone-jobs.py"
    assert phone.resolve() == jobs_worker.script().resolve()


def test_systemd_writes_a_unit_that_runs_the_checkouts_run_and_starts_it(host, monkeypatch):
    tmp_path, ran = host
    monkeypatch.setattr(jobs_worker, "backend", lambda: "systemd")
    assert jobs_worker.install(echo=lambda s: None)
    unit = (tmp_path / "systemd" / jobs_worker.UNIT).read_text()
    assert f"ExecStart=/bin/sh {jobs_worker.template_dir() / 'run'}" in unit
    assert ran == [("daemon-reload",), ("enable", "--now", jobs_worker.UNIT)]
    assert jobs_worker.install(echo=lambda s: None)             # idempotent
    # Generated, not a copy: drift does not count it.
    from agent_media_core import cli, service_drift
    root = cli._checkout_root()
    assert service_drift.scan_systemd(root, tmp_path / "systemd") == []


def test_runit_links_the_template_dir(host, monkeypatch):
    tmp_path, ran = host
    sv = tmp_path / "sv"
    sv.mkdir()
    monkeypatch.setattr(setup, "services_dir", lambda: sv)
    assert jobs_worker.backend() == "runit"
    assert jobs_worker.install(echo=lambda s: None)
    assert (sv / "jobs-worker").resolve() == jobs_worker.template_dir().resolve()
    assert jobs_worker.installed() and ran == []


def test_the_phone_keeps_its_own_worker(host, monkeypatch):
    tmp_path, ran = host
    sv = tmp_path / "sv"
    (sv / "phone-jobs").mkdir(parents=True)
    monkeypatch.setattr(setup, "services_dir", lambda: sv)
    said = []
    assert jobs_worker.install(echo=said.append)
    assert not (sv / "jobs-worker").exists() and "phone-jobs" in said[0]
    assert jobs_worker.row(check_only=True) == ("ok", "phone-jobs (the phone's worker)")


def test_windows_registers_a_logon_task_and_starts_it(host, monkeypatch):
    tmp_path, ran = host
    monkeypatch.setattr(jobs_worker, "backend", lambda: "windows")
    monkeypatch.setitem(sys.modules, "sasonica", None)          # no Sasonica package
    assert jobs_worker.install(echo=lambda s: None)
    create = next(c for c in ran if c[:2] == ("schtasks", "/Create"))
    assert jobs_worker.TASK in create and "ONLOGON" in create
    assert create[-1].startswith("conhost.exe --headless ") and create[-1].endswith(
        "jobs-worker.py\" --generic")
    assert ("schtasks", "/Run", "/TN", jobs_worker.TASK) in ran


def test_windows_uses_sasonicas_task_xml_when_it_is_here(host, monkeypatch):
    pytest.importorskip("sasonica.install")
    from sasonica import install as sas

    tmp_path, ran = host
    monkeypatch.setattr(jobs_worker, "backend", lambda: "windows")
    made = []
    monkeypatch.setattr(sas, "register_task", lambda name, xml, dry_run: made.append(
        (name, xml)) or 0)
    assert jobs_worker.install(echo=lambda s: None)
    assert made[0][0] == jobs_worker.TASK and "<RestartOnFailure>" in made[0][1]
    assert "--generic" in made[0][1]


def test_the_profile_row(host, monkeypatch):
    tmp_path, ran = host
    monkeypatch.setattr(jobs_worker, "backend", lambda: "systemd")
    assert jobs_worker.row(check_only=True)[0] == "absent"
    _paired(tmp_path)
    assert jobs_worker.row(check_only=True)[0] == "missing"
    assert jobs_worker.row(check_only=False)[0] == "installed"
    assert jobs_worker.row(check_only=True)[0] == "ok"
    rows = {r["name"]: r for r in setup.profile_status(None)}
    assert rows["jobs"]["state"] == "ok" and rows["jobs"]["kind"] == "extra"


def test_jobs_install_needs_a_pairing(host, monkeypatch, capsys):
    from agent_media_core import cli

    assert cli.main(["jobs", "install"]) == 1
    assert "media jobs pair" in capsys.readouterr().err
    assert cli.main(["jobs", "status"]) == 0
    assert "paired: no" in capsys.readouterr().out


def test_the_generic_worker_reads_its_pairing_and_not_the_phones(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("jw_t", jobs_worker.script())
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for k in ("MEDIA_JOBS_SERVER", "MEDIA_JOBS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEDIA_PHONE_JOBS_TOKEN", "the-phones")
    monkeypatch.setattr(mod, "CONFIG_FILE", tmp_path / "jw.json")
    mod.GENERIC = True
    assert (mod._token(), mod._server()) == ("", "")
    (tmp_path / "jw.json").write_text('{"server": "https://s.example/", "token": "pn"}')
    assert (mod._token(), mod._server()) == ("pn", "https://s.example")
    mod.GENERIC = False
    assert mod._token() == "the-phones"


# --- Windows: our Task Scheduler tasks are services to restart ---------------------


CSV = ('"\\Sasonica\\canvas","N/A","Running"\n'
       '"\\Sasonica\\sessiond","N/A","Ready"\n'
       '"\\agent-media\\jobs-worker","N/A","Running"\n'
       '"\\Microsoft\\Windows\\Defrag","N/A","Ready"\n')


def test_our_tasks_are_found_and_named_short(monkeypatch):
    monkeypatch.setattr(rs.os, "name", "nt")
    monkeypatch.setattr(rs, "_schtasks_query", lambda *a: CSV)
    assert rs.schtasks_states() == [("Sasonica\\canvas", "up"), ("Sasonica\\sessiond", "down"),
                                    ("agent-media\\jobs-worker", "up")]
    svcs = [rs.Service(n, s, "schtasks") for n, s in rs.schtasks_states()]
    sel, unknown = rs.resolve(["canvas", "jobs-worker"], svcs)
    assert [s.name for s in sel] == ["Sasonica\\canvas", "agent-media\\jobs-worker"]
    assert unknown == []
    # Task Scheduler does not say which process a task is: restarted detached.
    assert rs.own_services(sel) == sel


def test_off_windows_there_are_no_tasks(monkeypatch):
    monkeypatch.setattr(rs, "_schtasks_query", lambda *a: CSV)
    if rs.os.name != "nt":
        assert rs.schtasks_states() == []


def test_a_task_restarts_as_end_then_run(monkeypatch):
    ran = []

    class R:
        returncode, stdout, stderr = 0, "SUCCESS", ""

    monkeypatch.setattr(rs.subprocess, "run", lambda argv, **k: ran.append(argv) or R())
    ok, _ = rs._restart_one(rs.Service("Sasonica\\canvas", "up", "schtasks"))
    assert ok and ran == [["schtasks", "/End", "/TN", "Sasonica\\canvas"],
                          ["schtasks", "/Run", "/TN", "Sasonica\\canvas"]]
    monkeypatch.setattr(rs, "_schtasks_query", lambda *a: CSV)
    assert rs._is_up(rs.Service("Sasonica\\canvas", "up", "schtasks"))
