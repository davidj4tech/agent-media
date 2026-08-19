"""Source-agnostic rooms duck: under speech the coordinator lowers the audible
Snapcast music-stream clients' volumes (per-client, since Group.SetVolume isn't
in the snapserver RPC) and restores them after — independent of whether this
coordinator's Mopidy is the one playing. Gated by MEDIA_DUCK_ROOMS_STREAM.
"""

import pytest

from agent_media_core.route import coordinator as coord_mod
from agent_media_core.state import StateStore


@pytest.fixture
def state(tmp_path):
    return StateStore(tmp_path / "state.db")


@pytest.fixture
def fake_snap(monkeypatch):
    """Fake snapcast layer: a mutable client table + recorded SetVolume calls."""
    clients = {
        "hpo-music": 100,
        "p8ar-music": 80,
    }
    calls = []

    def clients_on_stream(stream_id, timeout=4.0, audible_only=True):
        return [{"id": cid, "percent": pct, "muted": False, "connected": True}
                for cid, pct in clients.items()]

    def set_client_volume(client_id, percent, muted=None, timeout=4.0):
        calls.append((client_id, percent))
        clients[client_id] = percent

    monkeypatch.setattr(coord_mod.snapcast, "clients_on_stream", clients_on_stream)
    monkeypatch.setattr(coord_mod.snapcast, "set_client_volume", set_client_volume)
    return clients, calls


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("MEDIA_DUCK_ROOMS_STREAM", "am-music")
    # Don't let a stray host duck-volume override leak in from the environment.
    monkeypatch.delenv("MEDIA_DUCK_VOLUME", raising=False)
    monkeypatch.delenv("AAR_MOPIDY_DUCK_VOLUME", raising=False)


def _coord(state):
    # A music sink that never reports a track — proves the rooms duck fires
    # even with nothing on this coordinator's Mopidy (the whole point).
    class _SilentMusic:
        def now_playing_uri(self, target):
            return None

    return coord_mod.Coordinator(music=_SilentMusic(), state=state)


def test_duck_lowers_audible_clients_and_restores(enabled, fake_snap, state):
    clients, calls = fake_snap
    c = _coord(state)

    c._rooms_duck(15)
    # Both clients were above 15 → both ducked to 15.
    assert clients == {"hpo-music": 15, "p8ar-music": 15}
    assert sorted(calls) == [("hpo-music", 15), ("p8ar-music", 15)]
    # Marker holds the *pre-duck* volumes for restore.
    marker = state.get_rooms_duck()
    assert marker["vols"] == {"hpo-music": 100, "p8ar-music": 80}

    calls.clear()
    c._rooms_unduck()
    assert clients == {"hpo-music": 100, "p8ar-music": 80}
    assert state.get_rooms_duck() is None


def test_strand_recovery_keeps_original_baseline(enabled, fake_snap, state):
    """A second duck with no intervening unduck (e.g. a killed process left a
    marker) must reuse the original volumes, not re-capture the ducked ones —
    otherwise restore would lock the rooms at the ducked level."""
    clients, calls = fake_snap
    c = _coord(state)

    c._rooms_duck(15)            # 100/80 -> 15/15, marker remembers 100/80
    c._rooms_duck(15)            # marker present: must NOT recapture 15/15
    assert state.get_rooms_duck()["vols"] == {"hpo-music": 100, "p8ar-music": 80}

    c._rooms_unduck()
    assert clients == {"hpo-music": 100, "p8ar-music": 80}


def test_release_and_reapply_toggle_rooms_duck(enabled, fake_snap, state):
    clients, _ = fake_snap
    c = _coord(state)

    c._rooms_duck(10)
    assert clients == {"hpo-music": 10, "p8ar-music": 10}

    c.release_music_duck()       # mid-response mute → restore, keep marker
    assert clients == {"hpo-music": 100, "p8ar-music": 80}
    assert state.get_rooms_duck() is not None

    c.reapply_music_duck()       # unmute → re-duck from the marker
    assert clients == {"hpo-music": 10, "p8ar-music": 10}


def test_disabled_when_env_unset_is_total_noop(fake_snap, state, monkeypatch):
    monkeypatch.delenv("MEDIA_DUCK_ROOMS_STREAM", raising=False)
    clients, calls = fake_snap
    c = _coord(state)

    c._rooms_duck(15)
    c._rooms_unduck()
    c.reapply_music_duck()
    assert calls == []
    assert clients == {"hpo-music": 100, "p8ar-music": 80}
    assert state.get_rooms_duck() is None


def test_one_failing_client_does_not_strand_the_others(enabled, fake_snap, state,
                                                       monkeypatch):
    """The restore used to run in one try block and clear the marker in a
    `finally`: the first client that threw took the rest of the loop with it,
    and the debt was erased anyway. That is how the fleet ended up with every
    music room parked at the duck level and nothing recording it."""
    clients, _ = fake_snap
    c = _coord(state)
    c._rooms_duck(15)

    ok = coord_mod.snapcast.set_client_volume

    def flaky(client_id, percent, muted=None, timeout=4.0):
        if client_id == "hpo-music":
            raise coord_mod.snapcast.SnapcastError("Client.SetVolume: timed out")
        ok(client_id, percent, muted=muted, timeout=timeout)

    monkeypatch.setattr(coord_mod.snapcast, "set_client_volume", flaky)
    monkeypatch.setattr(coord_mod.snapcast, "client_exists", lambda cid, timeout=4.0: True)
    c._rooms_unduck()

    # The client behind the failure is restored, not skipped.
    assert clients["p8ar-music"] == 80
    # ... and the one that failed is still owed, so the next unduck retries it.
    assert state.get_rooms_duck()["vols"] == {"hpo-music": 100}

    monkeypatch.setattr(coord_mod.snapcast, "set_client_volume", ok)
    c._rooms_unduck()
    assert clients["hpo-music"] == 100
    assert state.get_rooms_duck() is None


def test_a_client_the_server_no_longer_knows_is_dropped(enabled, fake_snap, state,
                                                        monkeypatch):
    """A renamed or deleted client cannot be restored — holding its debt would
    keep the marker alive for ever, which is its own kind of stuck."""
    clients, _ = fake_snap
    c = _coord(state)
    c._rooms_duck(15)

    def gone(client_id, percent, muted=None, timeout=4.0):
        raise coord_mod.snapcast.SnapcastError("Client.SetVolume: Client not found")

    monkeypatch.setattr(coord_mod.snapcast, "set_client_volume", gone)
    monkeypatch.setattr(coord_mod.snapcast, "client_exists", lambda cid, timeout=4.0: False)
    c._rooms_unduck()

    assert state.get_rooms_duck() is None


def test_a_capture_that_is_all_at_the_duck_level_is_reported(enabled, fake_snap,
                                                             state):
    """The strand is unfixable after the fact — the real baseline is gone — but
    it must stop being silent."""
    clients, _ = fake_snap
    clients["hpo-music"] = 10
    clients["p8ar-music"] = 10
    c = _coord(state)

    c._rooms_duck(10)

    errs = [e for e in state.recent_errors(limit=5)
            if "already at the duck level" in (e.get("message") or "")]
    assert errs, "a fleet parked at the duck level should be logged"
