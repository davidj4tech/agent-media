import os

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


@pytest.fixture(autouse=True)
def _no_remote_say(monkeypatch):
    """Keep the suite on the local render path.

    MEDIA_REMOTE_SAY_CMD replaces rendering entirely: submit_event hands the
    whole reply to another host and returns. Any test asserting on clips,
    history extras or playback then exercises a branch it never meant to, and
    fails in a way that points at the code rather than at the config it
    inherited. That config is not hypothetical — a host that speaks through a
    remote hub (or has media-lane switching lanes by network) sets this
    variable in ~/.config/agent-media.env, so the suite would pass or fail
    depending on which room the developer is standing in.

    A test that wants the remote path should set it explicitly.

    The per-target keys have to go too, and by prefix rather than by name:
    the lane is now chosen by MEDIA_REMOTE_SAY_CMD_<TARGET>, so media-lane
    writes ..._PHONE and scrubbing only the bare name would leave exactly the
    inherited-config hazard above — passing or failing by which room the
    developer is standing in — while looking as though it were handled.
    """
    for key in [k for k in os.environ if k.startswith("MEDIA_REMOTE_SAY_CMD")]:
        monkeypatch.delenv(key, raising=False)
