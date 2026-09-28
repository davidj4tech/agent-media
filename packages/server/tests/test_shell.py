"""`!` in the reply box (shell.py): the command, its output, and how a thread reads it."""

from __future__ import annotations

from agent_media_server import shell
from agent_media_server import transcript as T
from agent_media_core.intake._text import strip_system_blocks


def _user(n: int, text: str) -> dict:
    return {"type": "user", "uuid": f"u{n}", "timestamp": "2026-09-29T06:00:0{n}Z",
            "message": {"role": "user", "content": text}}


def _build(*texts: str) -> list[dict]:
    b = T.Builder()
    for i, t in enumerate(texts):
        b.feed(_user(i, t))
    return b.messages


def test_command_of():
    assert shell.command_of("!pwd") == "pwd"
    assert shell.command_of("  ! ls -la ") == "ls -la"
    assert shell.command_of("!") == ""
    assert shell.command_of("pwd!") == ""


def test_run_in_cwd_and_errors(tmp_path):
    assert shell.run("pwd", str(tmp_path)) == (str(tmp_path), "")
    out, err = shell.run("echo a; echo b >&2; exit 3", str(tmp_path))
    assert (out, err) == ("a", "b")
    assert shell.run("exit 4", str(tmp_path)) == ("", "Exit code 4")
    out, err = shell.run("echo x; sleep 5", str(tmp_path), timeout=0.5)
    assert err == "Command timed out after 0s"


def test_message_round_trips():
    msg = shell.message("ls", "a\nb", "")
    assert shell.parse(msg) == {"command": "ls", "stdout": "a\nb", "stderr": ""}
    assert shell.parse("see <bash-input>ls</bash-input> above") is None
    assert shell.parse("hello") is None


def test_output_is_capped():
    msg = shell.message("yes", "y" * (shell.OUTPUT_MAX + 10), "")
    assert "10 more characters cut" in msg


def test_pane_records_read_as_one_message():
    # Claude Code's bash mode: the command, then its output, as two records.
    msgs = _build("<bash-input>pwd</bash-input>",
                  "<bash-stdout>/w</bash-stdout><bash-stderr></bash-stderr>")
    assert len(msgs) == 1
    m = msgs[0]
    assert m["role"] == "user" and m["parts"][0]["text"] == "!pwd"
    assert m["shell"] == {"command": "pwd", "stdout": "/w", "stderr": ""}


def test_headless_message_reads_the_same():
    msgs = _build(shell.message("pwd", "/w", "oops"))
    assert msgs[0]["shell"] == {"command": "pwd", "stdout": "/w", "stderr": "oops"}


def test_same_command_twice_is_two_messages():
    one = "<bash-input>date</bash-input>"
    out = "<bash-stdout>x</bash-stdout><bash-stderr></bash-stderr>"
    assert len(_build(one, out, one, out)) == 2


def test_speech_leaves_bash_mode_out():
    assert strip_system_blocks(shell.message("pwd", "/w", "")) == ""
