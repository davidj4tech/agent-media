import pytest


@pytest.fixture(autouse=True)
def _no_live_phone_backend(monkeypatch):
    """Keep the suite away from the real phone player.

    `media music` transport routes to the phone-local backend whenever
    MEDIA_MUSIC_LOCAL_ENDPOINT is set (the cli module loads it from
    ~/.config/agent-media.env at import) and the phone's mpv has a track
    loaded — so on a dev box with music playing, an un-scrubbed test run
    would seek/pause the user's actual playback.
    """
    monkeypatch.delenv("MEDIA_MUSIC_LOCAL_ENDPOINT", raising=False)
