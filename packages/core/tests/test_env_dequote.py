"""Env values must survive the loader intact.

The loader dequoted with `.strip('"').strip("'")`, which removes quote
characters from either end whether or not they pair up. MEDIA_REMOTE_SAY_CMD
is a shell command whose last argument is quoted — two layers of it, because
ssh re-splits anything not still quoted when it joins its arguments — so the
value came back with its closing quotes eaten and its opening ones intact.
The shell that ran it died on an unterminated string, and the line in the
config file it came from looked perfectly correct.
"""

import os

from agent_media_core.intake._env import _dequote, load_env_file


def test_trailing_quote_is_not_eaten():
    cmd = """ssh host "timeout 5 sh -c 'exec sh /a/b.sh'" """.strip()
    assert _dequote(cmd) == cmd


def test_matched_wrapping_pair_is_removed_once():
    assert _dequote('"hello world"') == "hello world"
    assert _dequote("'hello world'") == "hello world"
    assert _dequote('""quoted""') == '"quoted"'      # one pair, not all of them


def test_unwrapped_values_are_untouched():
    assert _dequote("plain") == "plain"
    assert _dequote('"mismatched\'') == '"mismatched\''
    assert _dequote('') == ''
    assert _dequote('"') == '"'


def test_loader_preserves_a_two_layer_shell_command(tmp_path, monkeypatch):
    env = tmp_path / "agent-media.env"
    cmd = ('timeout 450 ssh -o BatchMode=yes p8a '
           '"timeout 420 sh -c \'[ -r /a/say.sh ] && exec sh /a/say.sh; '
           'exec sh /b/say.sh\'"')
    env.write_text(f"MEDIA_REMOTE_SAY_CMD={cmd}\n")
    monkeypatch.setenv("MEDIA_ENV_FILE", str(env))
    monkeypatch.delenv("MEDIA_REMOTE_SAY_CMD", raising=False)

    load_env_file("test")

    got = os.environ["MEDIA_REMOTE_SAY_CMD"]
    assert got == cmd
    assert got.count('"') % 2 == 0 and got.count("'") % 2 == 0, \
        "unbalanced quotes reach /bin/sh as a syntax error, not as silence"
