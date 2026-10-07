"""The `sasonica` command: dispatch by console-script name, and the shims
`sasonica install` writes (never over a file that is not one of its own)."""

import os

from sasonica import __main__ as sas
from sasonica import install


def test_commands_are_ours_only():
    names = sas.commands()
    assert "media" in names and "media-visual-canvas" in names
    assert "edge-tts" in names          # the default engine runs it as a program
    assert "sasonica" not in names      # not a command of itself
    assert "pip" not in names and "mcp" not in names


def test_alias_runs_the_script(monkeypatch):
    seen = {}

    class EP:
        def load(self):
            def run():
                import sys
                seen["argv"] = list(sys.argv)
                return 3
            return run

    monkeypatch.setattr(sas, "commands", lambda: {"media": EP()})
    assert sas.main(["sessiond", "--x"]) == 3
    assert seen["argv"] == ["media", "sessiond", "--x"]


def test_unknown_command(capsys):
    assert sas.main(["no-such-thing"]) == 2
    assert "no command" in capsys.readouterr().err


def test_binary_path(monkeypatch):
    monkeypatch.setenv("PYAPP", "1")
    assert sas.binary_path() is None
    monkeypatch.setenv("PYAPP", "/opt/sasonica")
    assert sas.binary_path() == "/opt/sasonica"


def test_shims_keep_foreign_files(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "commands", lambda: {"media": None, "media-setup": None})
    (tmp_path / "media").symlink_to("/somewhere/.venv/bin/media")
    written, kept = install.write_shims("/opt/sasonica", tmp_path, force=False, dry_run=False)
    assert written == ["media-setup"] and kept == ["media"]
    assert install.is_shim(tmp_path / "media-setup")
    assert 'exec "/opt/sasonica" media-setup "$@"' in (tmp_path / "media-setup").read_text()
    # Its own shims are rewritten (a moved binary); --force takes the rest.
    written, kept = install.write_shims("/new/sasonica", tmp_path, force=True, dry_run=False)
    assert sorted(written) == ["media", "media-setup"] and not kept
    assert install.is_shim(tmp_path / "media")


# --- update ------------------------------------------------------------------

from sasonica import update  # noqa: E402


def test_wanted_sum_reads_sha256sums():
    sums = "aaa  sasonica-linux-x86_64\nbbb *sasonica-linux-aarch64\n"
    assert update.wanted_sum(sums, "sasonica-linux-aarch64") == "bbb"
    assert update.wanted_sum(sums, "sasonica-linux-riscv") == ""


def test_prune_keeps_what_runs_and_what_is_kept(tmp_path, monkeypatch):
    root = tmp_path / "pyapp"       # its own: other fixtures write into tmp_path
    for v in ("1", "2", "3", "4"):
        (root / "d1" / v).mkdir(parents=True)
    monkeypatch.setattr(update, "in_use", lambda r: {r / "d1" / "2"})
    gone = update.prune(root, {root / "d1" / "4"})
    assert sorted(p.name for p in gone) == ["1", "3"]
    assert sorted(p.name for p in (root / "d1").iterdir()) == ["2", "4"]


def _release(tmp_path, content: bytes):
    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / update.asset()).write_bytes(content)
    import hashlib

    (rel / "SHA256SUMS").write_text(f"{hashlib.sha256(content).hexdigest()}  {update.asset()}\n")
    return rel


def test_update_replaces_the_binary_when_the_release_differs(tmp_path, monkeypatch):
    new = b"#!/bin/sh\necho 'sasonica 2'\n"
    rel = _release(tmp_path, new)
    binary = tmp_path / "sasonica"
    binary.write_bytes(b"#!/bin/sh\necho 'sasonica 1'\n")
    binary.chmod(0o755)
    monkeypatch.setenv("SASONICA_BINARY_BASE", rel.as_uri())
    monkeypatch.setattr(update, "_systemd", lambda: False)
    assert update.main(["--binary", str(binary)]) == 0
    assert binary.read_bytes() == new and os.access(binary, os.X_OK)
    # The same again is up to date.
    assert update.main(["--binary", str(binary)]) == 0


def test_a_bad_download_changes_nothing(tmp_path, monkeypatch):
    rel = _release(tmp_path, b"good")
    (rel / update.asset()).write_bytes(b"tampered")
    binary = tmp_path / "sasonica"
    binary.write_bytes(b"old")
    monkeypatch.setenv("SASONICA_BINARY_BASE", rel.as_uri())
    assert update.main(["--binary", str(binary)]) == 1
    assert binary.read_bytes() == b"old" and not (tmp_path / "sasonica.new").exists()


# --- the quick tunnel ----------------------------------------------------------

import argparse  # noqa: E402

import pytest  # noqa: E402

_no_tunnel_here = pytest.mark.skipif(os.name == "nt", reason="no quick tunnel on Windows yet")


def _args(tmp_path, **kw):
    a = dict(no_tunnel=False, force=False, dry_run=False, bin_dir=tmp_path / "bin")
    a.update(kw)
    return argparse.Namespace(**a)


@_no_tunnel_here
def test_the_tunnel_is_skipped_where_the_phone_has_a_way_in(tmp_path, monkeypatch):
    monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("MEDIA_VISUAL_PAIR_SERVER", raising=False)
    a = _args(tmp_path)
    assert install.tunnel_skip_reason(a, []) == ""
    assert "PAIR_SERVER" in install.tunnel_skip_reason(a, ["MEDIA_VISUAL_PAIR_SERVER=https://x"])
    assert install.tunnel_skip_reason(_args(tmp_path, no_tunnel=True), []) == "--no-tunnel"
    units = tmp_path / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / install.CHECKOUT_CANVAS_UNIT).write_text("")
    assert "checkout" in install.tunnel_skip_reason(a, [])            # red5
    (tmp_path / ".cloudflared").mkdir()
    (tmp_path / ".cloudflared" / "config.yml").write_text("tunnel: x\n")
    assert "named tunnel" in install.tunnel_skip_reason(a, [])


@_no_tunnel_here
def test_the_tunnel_step_adds_the_public_listener_once(tmp_path, monkeypatch, capsys):
    from agent_media_server import tunnel

    monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("MEDIA_VISUAL_PAIR_SERVER", raising=False)
    monkeypatch.setattr(tunnel, "fetch_cloudflared",
                        lambda d, dry_run=False: (str(d / "cloudflared"), "fetched"))
    env = tmp_path / ".config" / "agent-media.env"
    assert install._tunnel_step(_args(tmp_path), env) is True
    assert install._tunnel_step(_args(tmp_path), env) is True
    assert env.read_text().count("MEDIA_VISUAL_PUBLIC=127.0.0.1:8789") == 1
    assert "pairing again" in capsys.readouterr().out
    # No cloudflared: no tunnel service.
    monkeypatch.setattr(tunnel, "fetch_cloudflared", lambda d, dry_run=False: ("", "no build"))
    assert install._tunnel_step(_args(tmp_path), env) is False


def test_the_tunnel_unit_runs_sasonica_tunnel():
    (name, (word, what)), = install.TUNNEL_UNIT.items()
    text = install.unit_text("/opt/sasonica", word, what, "0.0.0.0", 8781)
    assert 'ExecStart="/opt/sasonica" tunnel\n' in text
    assert sas.ALIASES["tunnel"] == ("media-tunnel", ["run"])
    # `sasonica pair`, the command the app's pairing screen names.
    assert sas.ALIASES["pair"] == ("media-visual-canvas", ["pair"])
    assert install.LABELS[name] == "com.sasonica.quick-tunnel"
