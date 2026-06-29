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
