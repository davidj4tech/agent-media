"""Tests for media-setup's `server` role (rooms audio hub wiring)."""
import argparse

from agent_media_core import setup


def test_merge_env_defaults_idempotent(tmp_path):
    p = tmp_path / "agent-media.env"
    added = setup._merge_env_defaults(p, [("A", "1"), ("B", "2")], dry_run=False)
    assert set(added) == {"A", "B"}
    assert "A=1" in p.read_text() and "B=2" in p.read_text()
    # second run: A already set is NOT overwritten; only the new key is added
    again = setup._merge_env_defaults(p, [("A", "9"), ("C", "3")], dry_run=False)
    assert again == ["C"]
    txt = p.read_text()
    assert "A=1" in txt and "A=9" not in txt and "C=3" in txt


def test_merge_env_defaults_ignores_comments(tmp_path):
    p = tmp_path / "agent-media.env"
    p.write_text("# A=not-really-set\n")
    assert setup._merge_env_defaults(p, [("A", "1")], dry_run=False) == ["A"]


def test_merge_env_defaults_dry_run_no_write(tmp_path):
    p = tmp_path / "agent-media.env"
    assert setup._merge_env_defaults(p, [("A", "1")], dry_run=True) == ["A"]
    assert not p.exists()


def test_server_refuses_non_systemd(monkeypatch):
    # Termux/runit hosts stay snapclients (and keep the openal AO default) —
    # the server role must not touch them.
    monkeypatch.setattr(setup, "_service_backend", lambda *a, **k: "runit")
    args = argparse.Namespace(backend="auto", music=False, now=False, dry_run=True)
    assert setup.cmd_server(args) == 1


def test_server_dry_run_systemd(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "_service_backend", lambda *a, **k: "systemd")
    monkeypatch.setattr(setup, "systemd_user_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(setup, "_agent_media_env_path", lambda: tmp_path / "am.env")
    args = argparse.Namespace(backend="systemd", music=True, now=False, dry_run=True)
    assert setup.cmd_server(args) == 0
    out = capsys.readouterr().out
    assert "am-sinks.service" in out
    assert "am-snapfifo@.service" in out
    assert "MEDIA_SPEECH_AO=pulse" in out  # the PipeWire AO default
    assert "am-music" in out               # --music wired the second sink
    # dry-run writes nothing
    assert not (tmp_path / "user").exists()


# --- host roles -------------------------------------------------------------
# The regression these guard against is concrete: the phone ran both
# `call-guard` and `call-hold-consumer` for a fortnight, two processes pausing
# one speech socket, and barge-in failed intermittently the whole time.


def test_host_roles_unset_is_none_not_empty(tmp_path, monkeypatch):
    # None means "filter nothing", so a host that has never heard of roles
    # installs exactly what it installed before. An empty set would mean the
    # opposite -- every declaring service skipped -- and an upgrade that
    # silently stops installing services is worse than the bug being fixed.
    monkeypatch.delenv(setup.ROLES_ENV, raising=False)
    monkeypatch.setattr(setup.Path, "home", staticmethod(lambda: tmp_path))
    assert setup.host_roles() is None


def test_host_roles_from_env_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv(setup.ROLES_ENV, "observe, render")
    assert setup.host_roles() == {"observe", "render"}
    monkeypatch.delenv(setup.ROLES_ENV)
    monkeypatch.setattr(setup.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "agent-media-roles").write_text(
        "# this host\nrender\norigin  # the text comes from here\n")
    assert setup.host_roles() == {"render", "origin"}


def test_service_roles_read_from_repo():
    # Read from the real service dirs: the point is that these two files exist
    # and say what the installer needs, not that the parser works on fixtures.
    assert setup.service_roles("call-guard") == ({"observe"}, set())
    assert setup.service_roles("call-hold-consumer") == ({"render"}, {"observe"})


def test_undeclared_service_installs_everywhere():
    wanted, why = setup.service_wanted("sink-speech", {"render"})
    assert wanted and "no roles" in why


def test_phone_gets_the_guard_and_not_the_consumer():
    phone = {"observe", "render"}
    assert setup.service_wanted("call-guard", phone)[0] is True
    wanted, why = setup.service_wanted("call-hold-consumer", phone)
    # The bug in one assertion: the phone DOES render, so a requires-only rule
    # would install the consumer here. It is `conflicts: observe` that stops it.
    assert wanted is False
    assert "observe" in why


def test_house_host_gets_the_consumer_and_not_the_guard():
    red5 = {"render", "origin"}
    assert setup.service_wanted("call-hold-consumer", red5)[0] is True
    wanted, why = setup.service_wanted("call-guard", red5)
    assert wanted is False
    assert "observe" in why


def test_no_roles_declared_keeps_both(monkeypatch):
    # The pre-roles world, preserved exactly.
    assert setup.service_wanted("call-guard", None)[0] is True
    assert setup.service_wanted("call-hold-consumer", None)[0] is True
