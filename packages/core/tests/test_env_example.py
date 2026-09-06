"""The example config has to stay true.

An example that names a setting nothing reads is worse than no example: it is
a promise the code never made, and the only way to find out is to set it and
watch nothing happen. So every key it mentions must exist in the source, and
the file must survive the loader that will actually read it.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "agent-media.env.example"

# `#MEDIA_FOO=bar` (commented-out, as every line in the example is) or a live
# `MEDIA_FOO=bar`.
LINE = re.compile(r"^#?\s*((?:MEDIA|AGENT_MEDIA)_[A-Z0-9_]+)=(.*)$")

# Read outside this repo, so grepping it will never find them. Each one needs a
# reason: an unexplained entry here is how the check gets hollowed out.
ELSEWHERE = {
    # The dotfiles installer, deciding which checkout to update and run
    # media-setup against instead of cloning a second copy at ~/agent-media.
    "AGENT_MEDIA_DIR",
}


def _lines():
    for n, raw in enumerate(EXAMPLE.read_text().splitlines(), 1):
        m = LINE.match(raw.strip())
        if m:
            yield n, m.group(1), m.group(2), raw


def test_the_example_exists():
    assert EXAMPLE.is_file(), "agent-media.env.example is the documented starting point"


@pytest.mark.skipif(not (REPO / "packages").is_dir(), reason="not a source checkout")
def test_every_key_it_names_is_read_by_something():
    """No inventions and no leftovers from a setting that was renamed."""
    keys = {k for _n, k, _v, _raw in _lines()}
    assert keys, "the example should name some keys"
    hits = subprocess.run(
        ["grep", "-rhoE", "(MEDIA|AGENT_MEDIA)_[A-Z0-9_]+",
         str(REPO / "packages"), str(REPO / "hooks") if (REPO / "hooks").is_dir() else str(REPO / "packages")],
        capture_output=True, text=True,
    ).stdout.split()
    known = set(hits) | ELSEWHERE
    # Per-target suffixes (MEDIA_SPEECH_SOCKET_PHONE) are built at runtime from
    # a base key, so accept a key whose prefix is known.
    unknown = {k for k in keys
               if k not in known
               and not any(k.startswith(b + "_") for b in known)}
    assert not unknown, f"named in the example, read by nothing: {sorted(unknown)}"


def test_no_inline_comments():
    """The parser takes everything after the first `=`.

    The example says so in its own header; it would be a poor advertisement for
    the rule to break it, and the failure is silent — a value with a comment
    glued to the end of it.
    """
    bad = [(n, raw) for n, _k, v, raw in _lines() if "#" in v]
    assert not bad, f"inline comment becomes part of the value: {bad}"


def test_it_loads(tmp_path, monkeypatch):
    """The loader reads it without complaint, and its keys arrive."""
    from agent_media_core.intake import _env

    live = EXAMPLE.read_text().replace("\n#MEDIA_SPEECH_DEFAULT_TARGET=local",
                                       "\nMEDIA_SPEECH_DEFAULT_TARGET=rooms")
    path = tmp_path / "agent-media.env"
    path.write_text(live)
    monkeypatch.setenv("MEDIA_ENV_FILE", str(path))
    monkeypatch.delenv("MEDIA_SPEECH_DEFAULT_TARGET", raising=False)
    _env.load_env_file("test")
    assert os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET") == "rooms"


def test_the_commented_lines_stay_commented(tmp_path, monkeypatch):
    """As shipped it changes nothing — copying it is safe before editing."""
    from agent_media_core.intake import _env

    path = tmp_path / "agent-media.env"
    path.write_text(EXAMPLE.read_text())
    monkeypatch.setenv("MEDIA_ENV_FILE", str(path))
    for k in ("MEDIA_SPEECH_DEFAULT_TARGET", "MEDIA_ABS_URLS", "MEDIA_REPLY_USERS"):
        monkeypatch.delenv(k, raising=False)
    _env.load_env_file("test")
    assert not os.environ.get("MEDIA_ABS_URLS")
    assert not os.environ.get("MEDIA_REPLY_USERS")


def test_it_does_not_carry_secrets():
    """Examples get copied. Nothing here should be worth stealing."""
    for _n, key, value, _raw in _lines():
        if any(w in key for w in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
            assert not value.strip() or value.strip().startswith(("/", "http")), (
                f"{key} in the example should be empty or a path, not a value")
