"""A hold must not be waited out while holding the speech token.

The token is global: one reply owns it and every other session's speech queues.
Waiting for a hold while holding it was bounded in theory by the marker's own
expiry — but call-guard re-asserts the hold every 15s for as long as the phone's
mic looks hot, so the marker never expires. One reply then sat on the token
while playing nothing, and every session's speech waited behind it until each
waiter gave up after MEDIA_SPEECH_LOCK_TIMEOUT_S and played unserialized. Ten
minutes, which is what David heard.
"""

import threading
import time

import pytest

from agent_media_core.intake import submit


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_HOLD_OWNER", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)


def test_the_wait_ends_the_moment_the_hold_lifts():
    submit.set_speech_hold(60, "cece")
    done = threading.Event()
    threading.Thread(target=lambda: (submit._wait_speech_hold(), done.set()),
                     daemon=True).start()
    assert not done.wait(0.4), "returned while a hold was live"
    submit.release_speech_hold("cece")
    assert done.wait(3.0), "did not notice the release"


def test_the_waiter_keeps_its_place_in_the_queue():
    """A wait longer than MEDIA_SPEECH_PENDING_TTL_S would otherwise age the
    reply out of its own session's order, and a later sibling would speak first
    the moment the hold lifted. So the wait refreshes the announcement — on
    entry, and again as it goes on."""
    submit.set_speech_hold(60, "cece")
    beats = []
    waiting = threading.Thread(
        target=lambda: submit._wait_speech_hold(
            refresh=lambda: beats.append(time.monotonic()),
            refresh_every_s=0.2),
        daemon=True)
    waiting.start()
    time.sleep(0.7)
    submit.release_speech_hold("cece")
    waiting.join(timeout=3.0)
    assert len(beats) >= 2, f"announcement refreshed {len(beats)}x — it will age out"


def test_an_unheld_channel_costs_nothing():
    """The common case is no hold at all, and it must not pay for any of this
    — not a sleep, not even a refresh."""
    beats = []
    t = time.monotonic()
    submit._wait_speech_hold(refresh=lambda: beats.append(1))
    assert time.monotonic() - t < 0.1
    assert beats == []


def test_the_token_is_free_while_a_hold_stands(tmp_path, monkeypatch):
    """The property the fix exists for: with a hold up, another session can
    still take the token — it is nobody's while nobody may speak.

    Driven through the lock directly rather than submit_event: what matters is
    that whoever waits for a hold is not the flock's owner while doing it.
    """
    submit.set_speech_hold(60, "cece")
    mine = submit._SpeechPlaybackLock()
    mine.announce(session="pane-a", seq=1.0)
    waited = threading.Event()
    threading.Thread(
        target=lambda: (submit._wait_speech_hold(
            refresh=lambda: mine.announce(session="pane-a", seq=1.0)),
            waited.set()), daemon=True).start()
    time.sleep(0.3)

    other = submit._SpeechPlaybackLock()
    took = threading.Event()
    threading.Thread(target=lambda: (other.acquire(session="pane-b", seq=2.0),
                                     took.set()), daemon=True).start()
    assert took.wait(3.0), "another session could not take a token nobody holds"
    other.release()
    submit.release_speech_hold("cece")
    assert waited.wait(3.0)
