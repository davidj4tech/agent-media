"""The config surface: what this host is, and who its peers are.

The property under test throughout is that nothing here can stop a host making
sound. Every failure mode reads as "nothing configured", because every consumer
has a defined behaviour for unset and none has one for "the config raised".
"""

import pytest

from agent_media_core import config


def _write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


FLEET = """
[host]
roles = ["render", "origin"]

[peers.phone]
host  = "p8a"
roles = ["observe", "render"]

[peers.speaker]
host  = "sp4"
roles = ["render"]
"""


def test_reads_roles_and_peers(tmp_path):
    p = _write(tmp_path, FLEET)
    assert config.host_roles(p) == {"render", "origin"}
    assert config.peer_hosts(p) == ["p8a", "sp4"]
    assert config.peer("phone", p).host == "p8a"


def test_a_peer_can_be_found_by_role_not_just_alias(tmp_path):
    """The point of the whole design: a call site says what it needs done,
    not which machine does it."""
    p = _write(tmp_path, FLEET)
    assert config.peer("observe", p).host == "p8a"
    assert [x.host for x in config.peers_with("render", p)] == ["p8a", "sp4"]
    assert config.peers_with("origin", p) == []      # that is this host


def test_standalone_is_an_empty_peers_table(tmp_path):
    """"Standalone" is a configuration, not a mode: same code, no peers."""
    p = _write(tmp_path, '[host]\nroles = ["observe", "render", "origin"]\n')
    assert config.host_roles(p) == {"observe", "render", "origin"}
    assert config.peers(p) == {}
    assert config.peer_hosts(p) == []
    assert config.peer("origin", p) is None


def test_missing_file_is_not_an_error(tmp_path):
    p = tmp_path / "nope.toml"
    assert config.load(p) == {}
    assert config.peers(p) == {}


def test_malformed_toml_is_ignored_not_raised(tmp_path, caplog):
    """A typo in the config must not take the audio stack down with it."""
    p = _write(tmp_path, "[host\nroles = [")
    assert config.load(p) == {}
    assert config.peers(p) == {}
    assert any("ignoring" in r.message.lower() or "ignoring" in r.getMessage()
               for r in caplog.records)


def test_peer_without_a_host_is_skipped(tmp_path):
    """Defaulting the host to the alias would dial a machine nobody named."""
    p = _write(tmp_path, '[peers.ghost]\nroles = ["render"]\n')
    assert config.peers(p) == {}


def test_env_beats_the_file(tmp_path, monkeypatch):
    p = _write(tmp_path, FLEET)
    monkeypatch.setenv(config.ROLES_ENV, "observe")
    assert config.host_roles(p) == {"observe"}


def test_unset_everywhere_is_none_not_empty(tmp_path, monkeypatch):
    """None means "filter nothing" to the service installer; the empty set
    means "install only services that make no demands". An upgrade that
    silently stopped installing services would be worse than any bug this
    prevents."""
    monkeypatch.delenv(config.ROLES_ENV, raising=False)
    monkeypatch.setattr(config, "legacy_roles_path", lambda: tmp_path / "absent")
    assert config.host_roles(tmp_path / "absent.toml") is None


def test_legacy_roles_file_still_works(tmp_path, monkeypatch):
    """The plain roles file predates the TOML and is still deployed."""
    legacy = tmp_path / "agent-media-roles"
    legacy.write_text("# this host\nobserve\nrender  # renders the phone lane\n")
    monkeypatch.delenv(config.ROLES_ENV, raising=False)
    monkeypatch.setattr(config, "legacy_roles_path", lambda: legacy)
    assert config.host_roles(tmp_path / "absent.toml") == {"observe", "render"}


def test_comments_are_not_roles(tmp_path, monkeypatch):
    """Caught in review, and it matters: without comment-stripping every word
    of the explanation becomes a role, the host appears able to do everything,
    and no service is ever filtered out."""
    legacy = tmp_path / "roles"
    legacy.write_text("# red5 renders speech and originates replies\nrender\n")
    monkeypatch.delenv(config.ROLES_ENV, raising=False)
    monkeypatch.setattr(config, "legacy_roles_path", lambda: legacy)
    assert config.host_roles(tmp_path / "absent.toml") == {"render"}


@pytest.mark.parametrize("value,expected", [
    (["Observe", " render "], {"observe", "render"}),
    ("observe, render", {"observe", "render"}),
    ([], set()),
    (None, set()),
    (42, set()),
])
def test_role_lists_are_forgiving(value, expected):
    assert config._as_roles(value) == expected
