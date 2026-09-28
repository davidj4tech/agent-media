"""The `sasonica` command: dispatch by console-script name, and the shims
`sasonica install` writes (never over a file that is not one of its own)."""

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
