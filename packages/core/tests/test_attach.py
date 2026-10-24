"""`media attach` and the `Shared file:` line it prints: a reply's file
shown as a chip in the app (§6.18), its path never read aloud."""

import argparse

from agent_media_core import cli
from agent_media_core.intake._text import strip_markdown


def test_speech_skips_shared_file_lines():
    text = "Here it is.\n\nShared file: /home/u/shared/2026-10-25/a.png\n\nMore words."
    assert strip_markdown(text) == "Here it is.\n\nMore words."
    assert strip_markdown("Look.\nShared file: /x/a.png") == "Look."
    # Only a line of its own: the words in a sentence stay.
    assert strip_markdown("The line reads Shared file: and a path.") == "The line reads Shared file: and a path."


def test_attach_copies_into_todays_folder_never_over_another(tmp_path, monkeypatch, capsys):
    root = tmp_path / "shared"
    monkeypatch.setenv("MEDIA_UPLOAD_DIR", str(root))
    src = tmp_path / "shot.png"
    src.write_bytes(b"png")
    assert cli.cmd_attach(argparse.Namespace(files=[str(src), str(src)])) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [ln.rsplit("/", 1)[-1] for ln in lines] == ["shot.png", "shot-2.png"]
    assert all(ln.startswith(f"Shared file: {root}/") for ln in lines)
    assert (root.glob("*/shot-2.png").__next__()).read_bytes() == b"png"


def test_attach_a_file_already_shared_is_not_copied(tmp_path, monkeypatch, capsys):
    root = tmp_path / "shared"
    (root / "2026-10-01").mkdir(parents=True)
    kept = root / "2026-10-01" / "a.pdf"
    kept.write_bytes(b"%PDF")
    monkeypatch.setenv("MEDIA_UPLOAD_DIR", str(root))
    assert cli.cmd_attach(argparse.Namespace(files=[str(kept)])) == 0
    assert capsys.readouterr().out.strip() == f"Shared file: {kept.resolve()}"
    assert len(list(root.rglob("*.pdf"))) == 1


def test_attach_refuses_what_is_not_a_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEDIA_UPLOAD_DIR", str(tmp_path / "shared"))
    assert cli.cmd_attach(argparse.Namespace(files=[str(tmp_path / "nope")])) == 2
    assert "not a file" in capsys.readouterr().err
