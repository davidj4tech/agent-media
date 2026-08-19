"""Entitlements: what this install has paid for.

The property under test throughout is the one the module promises: **nothing
here can stop a host making sound**. Every malformed, expired, unsigned,
foreign-signed or absent licence has to read as "free tier", never as an
exception, because that is the only behaviour every consumer already has.

The second property is that a licence has to be worth something: a token
anyone can edit is not a licence, so the signature has to actually bind.
"""

import binascii
import json
import os
import time

import pytest

from agent_media_core import _ed25519, entitlements


# --------------------------------------------------------------------------
# RFC 8032 vectors — the curve code is transcribed, so it gets checked against
# the specification rather than against itself.
# --------------------------------------------------------------------------

def test_ed25519_matches_rfc8032_vectors():
    h = binascii.unhexlify
    seed = h("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    pub = h("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
    sig = h("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")

    assert _ed25519.public_key(seed) == pub
    assert _ed25519.sign(seed, b"\x72") == sig
    assert _ed25519.verify(pub, b"\x72", sig)
    assert not _ed25519.verify(pub, b"\x73", sig)


def test_ed25519_verifies_a_signature_it_did_not_make():
    """The empty-message vector, verify-only: signing and verifying agreeing
    with each other proves less than either agreeing with the RFC."""
    h = binascii.unhexlify
    pub = h("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = h("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert _ed25519.verify(pub, b"", sig)


def test_ed25519_returns_false_rather_than_raising_on_junk():
    assert not _ed25519.verify(b"", b"msg", b"")
    assert not _ed25519.verify(b"\x00" * 32, b"msg", b"\x00" * 64)
    assert not _ed25519.verify(b"short", b"msg", b"\x01" * 64)


# --------------------------------------------------------------------------
# fixtures: an install with a signing key it trusts, and nothing inherited
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    """A licence-free home, and no inherited licence from the developer's own.

    Without this the suite would read whatever is installed on the machine it
    runs on — the same hazard conftest scrubs for the remote-say and mailbox
    variables, and the reason CI and a dev box would disagree.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(entitlements.LICENCE_ENV, raising=False)
    monkeypatch.delenv(entitlements.KEYS_ENV, raising=False)
    entitlements.refresh()
    yield
    entitlements.refresh()


@pytest.fixture
def seed(monkeypatch):
    """A signing key this install trusts, via the env key source."""
    raw = os.urandom(32)
    monkeypatch.setenv(entitlements.KEYS_ENV,
                       "test:" + _ed25519.public_key(raw).hex())
    return raw


def _mint(seed, **over):
    now = int(time.time())
    payload = {"kid": "test", "sub": "david", "tier": "plus",
               "feat": ["visual.*"], "iat": now, "exp": 0}
    payload.update(over)
    return entitlements.encode(payload, seed)


def _install(token, monkeypatch):
    monkeypatch.setenv(entitlements.LICENCE_ENV, token)
    entitlements.refresh()


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

def test_a_valid_token_grants_its_features(seed, monkeypatch):
    _install(_mint(seed, feat=["visual.hosted", "engine.studio"]), monkeypatch)

    assert entitlements.tier() == "plus"
    assert entitlements.feature_enabled("visual.hosted")
    assert entitlements.feature_enabled("engine.studio")
    assert not entitlements.feature_enabled("music.rooms")


def test_a_prefix_grant_covers_features_that_did_not_exist_yet(seed, monkeypatch):
    """The reason prefix grants exist: a token sold last year must keep
    working when a new feature ships under a heading it already paid for."""
    _install(_mint(seed, feat=["visual.*"]), monkeypatch)

    assert entitlements.feature_enabled("visual.hosted")
    assert entitlements.feature_enabled("visual.something-invented-later")
    assert not entitlements.feature_enabled("visualiser")   # not a prefix match
    assert not entitlements.feature_enabled("engine.studio")


def test_star_grants_everything(seed, monkeypatch):
    _install(_mint(seed, feat=["*"]), monkeypatch)
    assert entitlements.feature_enabled("anything.at.all")


def test_the_licence_file_is_read_when_the_env_is_unset(tmp_path, seed):
    token = _mint(seed)
    path = tmp_path / ".config" / "agent-media" / "licence"
    path.parent.mkdir(parents=True)
    path.write_text(token + "\n")
    entitlements.refresh()

    assert entitlements.licence_path() == path
    assert entitlements.tier() == "plus"


def test_the_env_may_hold_a_path_instead_of_a_token(tmp_path, seed, monkeypatch):
    elsewhere = tmp_path / "mounted-secret"
    elsewhere.write_text(_mint(seed))
    monkeypatch.setenv(entitlements.LICENCE_ENV, str(elsewhere))
    entitlements.refresh()

    assert entitlements.tier() == "plus"


# --------------------------------------------------------------------------
# every way a licence can be wrong reads as free tier
# --------------------------------------------------------------------------

@pytest.mark.parametrize("token", [
    "",
    "   ",
    "not-a-token",
    "AM1.only-two-parts",
    "AM1.!!!not-base64!!!.!!!nor-this!!!",
    "AM1." + "eyJub3QiOiAianNvbiJ9" + ".AAAA",     # valid b64, unsigned
    "XX9.abc.def",                                  # unknown token version
])
def test_a_broken_token_is_free_tier_not_an_exception(token, seed, monkeypatch):
    _install(token, monkeypatch)

    assert entitlements.entitlement() is entitlements.FREE
    assert entitlements.tier() == "free"
    assert entitlements.feature_enabled("visual.hosted") is False


def test_a_payload_that_is_json_but_not_an_object_is_free_tier(seed, monkeypatch):
    body = entitlements._b64encode(json.dumps(["a", "list"]).encode())
    _install(f"AM1.{body}.{entitlements._b64encode(b'x' * 64)}", monkeypatch)
    assert entitlements.tier() == "free"


def test_an_edited_token_no_longer_verifies(seed, monkeypatch):
    """The whole point: the payload is signed, so raising your own tier by
    editing the file does not work — you have to fork the code instead, which
    the licence permits and this module's docstring says out loud."""
    token = _mint(seed, tier="plus", feat=["visual.hosted"])
    head, body, sig = token.split(".")
    forged = json.dumps({"kid": "test", "sub": "david", "tier": "studio",
                         "feat": ["*"], "iat": 0, "exp": 0},
                        sort_keys=True, separators=(",", ":")).encode()
    _install(f"{head}.{entitlements._b64encode(forged)}.{sig}", monkeypatch)

    assert entitlements.tier() == "free"
    assert not entitlements.feature_enabled("visual.hosted")


def test_a_token_signed_by_an_untrusted_key_is_free_tier(monkeypatch):
    """No `seed` fixture — nothing trusts the key that signed this."""
    token = _mint(os.urandom(32), feat=["*"])
    _install(token, monkeypatch)
    assert entitlements.tier() == "free"


def test_an_expired_token_is_free_tier(seed, monkeypatch):
    _install(_mint(seed, exp=int(time.time()) - 1), monkeypatch)
    assert entitlements.tier() == "free"
    assert not entitlements.feature_enabled("visual.hosted")


def test_expiry_is_evaluated_against_the_clock_not_the_cache(seed, monkeypatch):
    """A long-running service must not hold a licence open past its end just
    because it verified the token once at start-up."""
    soon = int(time.time()) + 60
    _install(_mint(seed, exp=soon), monkeypatch)
    assert entitlements.tier() == "plus"

    monkeypatch.setattr(entitlements.time, "time", lambda: soon + 1)
    assert entitlements.tier() == "free"


def test_no_licence_at_all_is_the_ordinary_state(monkeypatch):
    assert entitlements.entitlement() is entitlements.FREE
    assert entitlements.tier() == "free"
    assert entitlements.status()["have_token"] is False
    assert entitlements.status()["valid"] is False


# --------------------------------------------------------------------------
# trusted keys
# --------------------------------------------------------------------------

def test_keys_come_from_config_as_well_as_the_environment(tmp_path, monkeypatch):
    raw = os.urandom(32)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[licence.keys]\ntest = "%s"\n'
                   % _ed25519.public_key(raw).hex())
    monkeypatch.setenv("MEDIA_CONFIG", str(cfg))
    _install(_mint(raw), monkeypatch)

    assert entitlements.tier() == "plus"


@pytest.mark.parametrize("bad", ["nothex", "aabb", ""])
def test_an_unusable_configured_key_is_ignored_not_fatal(bad, monkeypatch):
    monkeypatch.setenv(entitlements.KEYS_ENV, f"test:{bad}")
    assert entitlements.trusted_keys() == {}
    assert entitlements.tier() == "free"


def test_status_reports_a_present_but_invalid_token(seed, monkeypatch):
    """`tier: free` alone reads as 'no licence installed'. The CLI needs to be
    able to tell the two apart, so status carries both facts."""
    _install("AM1.garbage.garbage", monkeypatch)
    info = entitlements.status()
    assert info["have_token"] is True
    assert info["valid"] is False


# --------------------------------------------------------------------------
# the one gate in core
# --------------------------------------------------------------------------

def test_an_unlicensed_render_engine_is_hidden_but_core_still_renders(
        seed, monkeypatch):
    """A paid engine that is not paid for disappears from the registry; it
    does not raise, and `edge` is still there to speak with."""
    from agent_media_core import extensions

    def paid_engine(text, outfile, *, voice=None):
        return True, ""

    paid_engine.agent_media_requires = "engine.studio"

    class _EP:
        name = "studio"
        value = "pkg:render"

        def load(self):
            return paid_engine

    monkeypatch.setattr(extensions, "entry_points", lambda group: [_EP()])

    _install(_mint(seed, feat=["visual.*"]), monkeypatch)
    assert "studio" not in extensions.discover_render_engines(refresh=True)
    assert "edge" in extensions.all_engine_names()

    _install(_mint(seed, feat=["engine.studio"]), monkeypatch)
    assert "studio" in extensions.discover_render_engines(refresh=True)


def test_an_engine_that_declares_nothing_is_unaffected(monkeypatch):
    """Which is every engine that exists today — the gate must be invisible
    to an install that has never heard of it."""
    from agent_media_core import extensions

    def free_engine(text, outfile, *, voice=None):
        return True, ""

    class _EP:
        name = "espeak"
        value = "pkg:render"

        def load(self):
            return free_engine

    monkeypatch.setattr(extensions, "entry_points", lambda group: [_EP()])
    assert "espeak" in extensions.discover_render_engines(refresh=True)
