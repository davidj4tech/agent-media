"""Cross-host owner claim on a shared remote (tcp://) broker.

The playback flock only serializes one host; the phone's mpv is driven by every
host. SinkSpeech stores an owner token in mpv `user-data` on the broker itself
so a second machine can tell the broker is taken and wait rather than clobber a
still-playing reply. These tests drive the token methods against an in-memory
fake of the mpv IPC layer.
"""

import pytest

from agent_media_core.sinks import speech as SP
from agent_media_core.sinks._mpv_ipc import MpvIpcError
from agent_media_core.types import Target


class _FakeBroker:
    """Dict-backed stand-in for one mpv instance's property store."""

    def __init__(self):
        self.store = {}

    def get_property(self, sock, name, timeout=2.0):
        if name not in self.store:
            raise MpvIpcError("get_property: property not found")
        return self.store[name]

    def set_property(self, sock, name, value):
        self.store[name] = value


@pytest.fixture
def broker(monkeypatch):
    fb = _FakeBroker()
    monkeypatch.setattr(SP.ipc, "get_property", fb.get_property)
    monkeypatch.setattr(SP.ipc, "set_property", fb.set_property)
    # A tcp:// endpoint makes the target "remote" (owner token active).
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")
    # No real desync sleep in claim_broker.
    monkeypatch.setattr(SP.time, "sleep", lambda *_a, **_k: None)
    # Freeze the clock so deadline comparisons are deterministic.
    monkeypatch.setattr(SP.time, "time", lambda: 1000.0)
    return fb


PHONE = Target(name="phone")
LOCAL = Target(name="local")


def test_local_target_is_never_owned(broker):
    sink = SP.SinkSpeech()
    # Local/rooms: flock handles it, token machinery is inert.
    assert sink.active_other_owner(LOCAL) is None
    assert sink.claim_broker(LOCAL) is True
    sink.refresh_broker(LOCAL)   # no-op, must not touch the store
    sink.release_broker(LOCAL)
    assert broker.store == {}


def test_claim_when_free_then_visible_to_others(broker):
    sink = SP.SinkSpeech()
    assert sink.active_other_owner(PHONE) is None      # unset -> free
    assert sink.claim_broker(PHONE) is True
    tok = broker.store[SP._BROKER_OWNER_KEY]
    assert tok["owner"] == SP._broker_owner_id()
    assert tok["deadline"] == 1000.0 + SP.BROKER_TTL_S
    # It's ours, so *we* still see it as free-to-take (not an "other" owner).
    assert sink.active_other_owner(PHONE) is None


def test_another_active_owner_blocks_claim(broker):
    sink = SP.SinkSpeech()
    broker.store[SP._BROKER_OWNER_KEY] = {"owner": "otherhost:42",
                                          "deadline": 1000.0 + 5}
    info = sink.active_other_owner(PHONE)
    assert info and info["owner"] == "otherhost:42"
    assert sink.claim_broker(PHONE) is False           # must not steal it
    # Store still shows the other host.
    assert broker.store[SP._BROKER_OWNER_KEY]["owner"] == "otherhost:42"


def test_expired_owner_is_free(broker):
    sink = SP.SinkSpeech()
    broker.store[SP._BROKER_OWNER_KEY] = {"owner": "otherhost:42",
                                          "deadline": 1000.0 - 1}  # past
    assert sink.active_other_owner(PHONE) is None       # expired -> free
    assert sink.claim_broker(PHONE) is True             # take it over
    assert broker.store[SP._BROKER_OWNER_KEY]["owner"] == SP._broker_owner_id()


def test_refresh_only_extends_our_own_claim(broker):
    sink = SP.SinkSpeech()
    # Someone else's claim must not be extended by our refresh.
    broker.store[SP._BROKER_OWNER_KEY] = {"owner": "otherhost:42",
                                          "deadline": 1000.0 + 5}
    sink.refresh_broker(PHONE)
    assert broker.store[SP._BROKER_OWNER_KEY]["deadline"] == 1000.0 + 5
    # Ours does get pushed out.
    sink.claim_broker(PHONE)  # expired? no — other is active. So claim fails.
    # Take over by expiring the other, then claim + refresh.
    broker.store[SP._BROKER_OWNER_KEY]["deadline"] = 1000.0 - 1
    assert sink.claim_broker(PHONE) is True
    broker.store[SP._BROKER_OWNER_KEY]["deadline"] = 1000.0 + 1  # simulate age
    sink.refresh_broker(PHONE)
    assert broker.store[SP._BROKER_OWNER_KEY]["deadline"] == 1000.0 + SP.BROKER_TTL_S


def test_release_only_clears_our_own_claim(broker):
    sink = SP.SinkSpeech()
    # Not ours: leave it alone.
    broker.store[SP._BROKER_OWNER_KEY] = {"owner": "otherhost:42",
                                          "deadline": 1000.0 + 5}
    sink.release_broker(PHONE)
    assert broker.store[SP._BROKER_OWNER_KEY]["owner"] == "otherhost:42"
    # Ours: clears.
    broker.store[SP._BROKER_OWNER_KEY]["deadline"] = 1000.0 - 1
    sink.claim_broker(PHONE)
    sink.release_broker(PHONE)
    assert broker.store[SP._BROKER_OWNER_KEY]["owner"] == ""


def test_unreachable_broker_does_not_wedge(broker, monkeypatch):
    """If the bridge is down we can't coordinate — claim must proceed rather
    than block a reply, and reads report 'free' rather than a phantom owner."""
    sink = SP.SinkSpeech()

    def boom(*_a, **_k):
        raise MpvIpcError("connection refused")

    monkeypatch.setattr(SP.ipc, "get_property", boom)
    monkeypatch.setattr(SP.ipc, "set_property", boom)
    assert sink.active_other_owner(PHONE) is None
    assert sink.claim_broker(PHONE) is True
