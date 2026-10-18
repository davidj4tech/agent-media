"""Service files that are copies of the checkout's (service_drift.py).

p8a's phone-jobs ran 3 Oct copies of `run` and `phone-jobs.py` until 18 Oct
2026: every pull updated the checkout and nothing the service ran. Built in
tmp dirs: a fake checkout, a runit service dir, a systemd user dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_media_core import cli, service_drift


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    tpl = root / "deploy/phone/service/phone-jobs"
    tpl.mkdir(parents=True)
    (tpl / "run").write_text("#!/bin/sh\nexec python3 ./phone-jobs.py\n")
    (tpl / "phone-jobs.py").write_text("print('new')\n")
    (tpl / "supervise").mkdir()                       # runit's state in a linked dir
    (tpl / "supervise" / "stat").write_text("run\n")
    core = root / "packages/core/services/sink-speech"
    (core / "log").mkdir(parents=True)
    (core / "run").write_text("#!/bin/sh\nexec media-sink-speech\n")
    (core / "log" / "run").write_text("#!/bin/sh\nexec svlogd .\n")
    units = root / "packages/visual/systemd"
    units.mkdir(parents=True)
    (units / "agent-media-visual-canvas.service").write_text("[Service]\nExecStart=canvas\n")
    (root / "deploy/systemd").mkdir(parents=True)
    (root / "deploy/systemd/agenda-alarm.timer").write_text("[Timer]\nOnCalendar=daily\n")
    (root / "packages/core/systemd").mkdir(parents=True)
    (root / "packages/core/systemd/agent-media-sink-book.service").write_text("[Service]\n")
    sv = tmp_path / "service"
    sv.mkdir()
    ud = tmp_path / "config" / "systemd" / "user"
    ud.mkdir(parents=True)
    monkeypatch.delenv("MEDIA_DRIFT_OK", raising=False)
    return root, sv, ud


def _copy(src: Path, dest: Path, text: "str | None" = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes() if text is None else text.encode())


def test_a_stale_copied_service_dir_is_drift(tree):
    """The p8a case: a real dir whose files were copied, then the checkout moved."""
    root, sv, ud = tree
    tpl = root / "deploy/phone/service/phone-jobs"
    _copy(tpl / "run", sv / "phone-jobs" / "run")
    _copy(tpl / "phone-jobs.py", sv / "phone-jobs" / "phone-jobs.py", "print('3 Oct')\n")
    found = service_drift.scan(root, sv, ud)
    by = {d.label: d for d in found}
    assert set(by) == {"phone-jobs/run", "phone-jobs/phone-jobs.py"}
    assert by["phone-jobs/run"].same and not by["phone-jobs/phone-jobs.py"].same
    assert by["phone-jobs/phone-jobs.py"].source == tpl / "phone-jobs.py"
    assert service_drift.facts(found) == {"drift": "phone-jobs/phone-jobs.py",
                                          "copies": "phone-jobs/run"}


def test_links_are_not_drift(tree):
    root, sv, ud = tree
    tpl = root / "deploy/phone/service/phone-jobs"
    (sv / "phone-jobs").mkdir()
    (sv / "phone-jobs" / "run").symlink_to(tpl / "run")
    (sv / "phone-jobs" / "phone-jobs.py").symlink_to(tpl / "phone-jobs.py")
    (sv / "sink-speech").symlink_to(root / "packages/core/services/sink-speech")
    (ud / "agent-media-visual-canvas.service").symlink_to(
        root / "packages/visual/systemd/agent-media-visual-canvas.service")
    assert service_drift.scan(root, sv, ud) == []


def test_log_run_and_systemd_copies_are_found(tree):
    root, sv, ud = tree
    core = root / "packages/core/services/sink-speech"
    (sv / "sink-speech").mkdir()
    (sv / "sink-speech" / "run").symlink_to(core / "run")
    _copy(core / "log" / "run", sv / "sink-speech" / "log" / "run", "#!/bin/sh\nold\n")
    _copy(root / "packages/visual/systemd/agent-media-visual-canvas.service",
          ud / "agent-media-visual-canvas.service")
    _copy(root / "deploy/systemd/agenda-alarm.timer", ud / "agenda-alarm.timer",
          "[Timer]\nOnCalendar=hourly\n")
    facts = service_drift.facts(service_drift.scan(root, sv, ud))
    assert facts["drift"].split(",") == ["sink-speech/log/run", "agenda-alarm.timer"]
    assert facts["copies"] == "agent-media-visual-canvas.service"


def test_a_unit_media_setup_generated_is_not_a_copy(tree):
    """install-services writes `ExecStart=/bin/sh <checkout>/…/run`: the code
    it runs is the checkout's, whatever the unit's text."""
    root, sv, ud = tree
    (ud / "agent-media-sink-book.service").write_text(
        f"[Service]\nExecStart=/bin/sh {root}/packages/core/services/sink-book/run\n")
    assert service_drift.scan(root, sv, ud) == []


def test_strangers_are_left_out(tree):
    """sshd, snapclient, a unit of another project: not ours to grade."""
    root, sv, ud = tree
    (sv / "sshd").mkdir()
    (sv / "sshd" / "run").write_text("#!/bin/sh\nexec sshd -D\n")
    (ud / "snapclient.service").write_text("[Service]\n")
    assert service_drift.scan(root, sv, ud) == []


def test_media_drift_ok_keeps_a_deliberate_copy(tree, monkeypatch):
    root, sv, ud = tree
    _copy(root / "packages/visual/systemd/agent-media-visual-canvas.service",
          ud / "agent-media-visual-canvas.service", "[Service]\nlocal=1\n")
    tpl = root / "deploy/phone/service/phone-jobs"
    _copy(tpl / "run", sv / "phone-jobs" / "run", "x")
    monkeypatch.setenv("MEDIA_DRIFT_OK", "agent-media-visual-canvas.service, phone-jobs/run")
    assert service_drift.scan(root, sv, ud) == []


def test_drift_is_a_problem_and_a_matching_copy_is_only_a_note():
    problems = cli.health_problems({"install": "editable", "drift": "phone-jobs/run"})
    assert any("phone-jobs/run" in p and "link-services" in p for p in problems)
    assert cli.health_problems({"install": "editable", "copies": "phone-jobs/run"}) == []
    assert "copy, not linked: phone-jobs/run" in cli.copy_note({"copies": "phone-jobs/run"})


def test_selfcheck_carries_the_drift_facts(tree, monkeypatch):
    root, sv, ud = tree
    _copy(root / "deploy/phone/service/phone-jobs/run", sv / "phone-jobs" / "run", "old")
    monkeypatch.setattr(cli, "_checkout_root", lambda: root)
    monkeypatch.setattr(cli, "_sv_dir", lambda: sv)
    monkeypatch.setattr(cli, "_runit_service_states", lambda r: [])
    monkeypatch.setattr(cli, "_systemd_service_states", lambda: [])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(ud.parents[1]))
    monkeypatch.setenv("XDG_STATE_HOME", str(ud.parents[2] / "state"))
    facts = cli.selfcheck_facts()
    assert facts["drift"] == "phone-jobs/run"
