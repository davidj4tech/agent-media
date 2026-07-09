"""Test isolation for the visual package.

core's cli.py calls load_env_file at module import, so a combined run
(`pytest packages/visual/tests packages/core/tests`) pulls the machine's
real ~/.config/agent-media.env into os.environ during collection — e.g. a
real MEDIA_VISUAL_ENGINE=svg flips engine-default tests. Scrub every
MEDIA_* var so these tests always see package defaults; tests that need a
value set it explicitly via monkeypatch.setenv.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_media_env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("MEDIA_"):
            monkeypatch.delenv(k, raising=False)
    # Point the state store at a throwaway dir: speech_state() enriches from
    # the REAL now_playing row otherwise, so a test run while something is
    # actually speaking (or a stale row exists) grows a surprise "sentence"
    # key and fails.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
