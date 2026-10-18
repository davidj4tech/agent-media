"""Installing services as links, not copies (David, 18 Oct 2026).

p8a's phone-jobs ran 3 Oct copies of its `run` and worker script: pulls never
reached them. Two fixes, both tested here in tmp dirs:

* `media-setup install-services` (runit): a real service dir becomes a link
  when it matches the template; one that differs is reported and left alone
  unless `--force` (then backed up, outside the service root). The phone's own
  `deploy/phone/service/*` install the same way when named.
* `media-setup link-services`: each installed file that is a copy of a
  checkout file (service_drift.scan) becomes a link to it, the copy kept as
  `<file>.bak-<date>`; a differing copy only with `--force`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agent_media_core import service_drift, setup


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    tpl = root / "deploy/phone/service/phone-jobs"
    tpl.mkdir(parents=True)
    (tpl / "run").write_text("#!/bin/sh\nexec python3 ./phone-jobs.py\n")
    (tpl / "run").chmod(0o755)
    (tpl / "phone-jobs.py").write_text("print('new')\n")
    units = root / "deploy/systemd"
    units.mkdir(parents=True)
    (units / "landscape-watch.timer").write_text("[Timer]\nOnCalendar=daily\n")
    sv = tmp_path / "service"
    sv.mkdir()
    ud = tmp_path / "config/systemd/user"
    ud.mkdir(parents=True)
    monkeypatch.delenv("MEDIA_DRIFT_OK", raising=False)
    reloads = []
    monkeypatch.setattr(service_drift, "_daemon_reload", lambda: reloads.append(1))
    return root, sv, ud, reloads


def _copy(src: Path, dest: Path, text: "str | None" = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes() if text is None else text.encode())


def test_identical_copies_become_links_and_are_kept_as_bak(tree):
    root, sv, ud, reloads = tree
    tpl = root / "deploy/phone/service/phone-jobs"
    _copy(tpl / "run", sv / "phone-jobs/run")
    _copy(root / "deploy/systemd/landscape-watch.timer", ud / "landscape-watch.timer")
    said = []
    assert service_drift.relink(service_drift.scan(root, sv, ud), echo=said.append) == 0
    assert (sv / "phone-jobs/run").resolve() == tpl / "run"
    assert (ud / "landscape-watch.timer").resolve() == root / "deploy/systemd/landscape-watch.timer"
    baks = sorted(p.name for p in (sv / "phone-jobs").iterdir() if ".bak-" in p.name)
    assert len(baks) == 1 and baks[0].startswith("run.bak-")
    assert reloads == [1]                           # a unit changed: systemd reloads
    # Idempotent: nothing left to link.
    assert service_drift.scan(root, sv, ud) == []
    assert service_drift.relink([], echo=said.append) == 0


def test_a_differing_copy_is_reported_not_overwritten(tree):
    root, sv, ud, _ = tree
    tpl = root / "deploy/phone/service/phone-jobs"
    _copy(tpl / "phone-jobs.py", sv / "phone-jobs/phone-jobs.py", "print('3 Oct')\n")
    said = []
    assert service_drift.relink(service_drift.scan(root, sv, ud), echo=said.append) == 1
    live = sv / "phone-jobs/phone-jobs.py"
    assert not live.is_symlink() and live.read_text() == "print('3 Oct')\n"
    assert "differs" in said[0] and "--force" in said[0]


def test_force_links_a_differing_copy_and_keeps_it(tree):
    root, sv, ud, _ = tree
    tpl = root / "deploy/phone/service/phone-jobs"
    _copy(tpl / "phone-jobs.py", sv / "phone-jobs/phone-jobs.py", "print('3 Oct')\n")
    assert service_drift.relink(service_drift.scan(root, sv, ud), force=True,
                                echo=lambda s: None) == 0
    live = sv / "phone-jobs/phone-jobs.py"
    assert live.resolve() == tpl / "phone-jobs.py"
    bak = next(p for p in (sv / "phone-jobs").iterdir() if ".bak-" in p.name)
    assert bak.read_text() == "print('3 Oct')\n"


def test_dry_run_touches_nothing(tree):
    root, sv, ud, reloads = tree
    _copy(root / "deploy/phone/service/phone-jobs/run", sv / "phone-jobs/run")
    said = []
    assert service_drift.relink(service_drift.scan(root, sv, ud), dry_run=True,
                                echo=said.append) == 0
    assert not (sv / "phone-jobs/run").is_symlink() and reloads == []
    assert said[0].startswith("# would link")


def test_link_services_is_a_media_setup_command(tree, monkeypatch):
    root, sv, ud, _ = tree
    _copy(root / "deploy/phone/service/phone-jobs/run", sv / "phone-jobs/run")
    monkeypatch.setattr(service_drift, "scan", lambda: service_drift.scan_runit(root, sv))
    assert setup.main(["link-services", "--dry-run"]) == 0
    assert setup.main(["link-services"]) == 0
    assert (sv / "phone-jobs/run").is_symlink()


# --- install-services, runit -------------------------------------------------------


@pytest.fixture()
def runit(tree, monkeypatch):
    root, sv, _, _ = tree
    templates = root / "packages/core/services"
    (templates / "sink-speech").mkdir(parents=True)
    (templates / "sink-speech/run").write_text("#!/bin/sh\nexec media-sink-speech\n")
    monkeypatch.setattr(setup, "service_templates_dir", lambda: templates)
    monkeypatch.setattr(setup, "phone_service_templates_dir",
                        lambda: root / "deploy/phone/service")
    monkeypatch.setattr(setup, "media_share_dir", lambda: root.parent / "share")
    monkeypatch.setattr(setup, "_link_entrypoints", lambda dry_run: True)
    monkeypatch.setattr(setup, "_merge_env_defaults", lambda *a, **k: [])
    return root, sv, templates


def _install(sv, *names, force=False):
    return setup.cmd_install_services(argparse.Namespace(
        services=list(names), backend="runit", root=str(sv), dry_run=False, now=False,
        force=force))


def test_the_phones_own_services_install_as_links_when_named(runit):
    root, sv, _ = runit
    assert _install(sv, "phone-jobs") == 0
    assert (sv / "phone-jobs").is_symlink()
    assert (sv / "phone-jobs").resolve() == root / "deploy/phone/service/phone-jobs"
    assert _install(sv, "phone-jobs") == 0          # idempotent


def test_a_real_dir_of_links_or_identical_copies_becomes_a_link(runit):
    """What p8a has now: a real dir whose files are links into the checkout."""
    root, sv, _ = runit
    tpl = root / "deploy/phone/service/phone-jobs"
    (sv / "phone-jobs/supervise").mkdir(parents=True)
    (sv / "phone-jobs/run").symlink_to(tpl / "run")
    _copy(tpl / "phone-jobs.py", sv / "phone-jobs/phone-jobs.py")
    assert _install(sv, "phone-jobs") == 0
    assert (sv / "phone-jobs").is_symlink()


def test_a_real_dir_that_differs_is_left_alone_unless_forced(runit, capsys):
    root, sv, _ = runit
    tpl = root / "deploy/phone/service/phone-jobs"
    _copy(tpl / "run", sv / "phone-jobs/run")
    _copy(tpl / "phone-jobs.py", sv / "phone-jobs/phone-jobs.py", "print('3 Oct')\n")
    assert _install(sv, "phone-jobs") == 1
    assert not (sv / "phone-jobs").is_symlink()
    assert "differs" in capsys.readouterr().err
    assert _install(sv, "phone-jobs", force=True) == 0
    assert (sv / "phone-jobs").is_symlink()
    backups = list((root.parent / "share/service-backups").iterdir())
    assert len(backups) == 1 and backups[0].name.startswith("phone-jobs.bak-")
    assert (backups[0] / "phone-jobs.py").read_text() == "print('3 Oct')\n"
