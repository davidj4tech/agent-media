"""Publishing when the conversation stops, rather than when a timer looks.

The rule this exists to respect: no client re-fetches a guid it already has,
so an episode may only be published once, complete. The only moment anything
knows a conversation *might* be finished is the end of a turn — so every turn
pushes the deadline out, and the last one gets to fire.
"""

import subprocess

import pytest

from agent_media_core import feed_debounce as dbn


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_BASE_URL", "http://red5:8782")
    monkeypatch.setattr(dbn.shutil, "which", lambda n: f"/usr/bin/{n}")


def test_it_is_off_where_no_feed_is_served(monkeypatch):
    """A host that serves no feed must not schedule work for one."""
    monkeypatch.delenv("MEDIA_FEED_BASE_URL", raising=False)
    assert dbn.enabled() is False
    assert dbn.arm() is False


def test_it_is_off_without_systemd(monkeypatch):
    """runit hosts fall back to the poll rather than inventing a scheduler."""
    monkeypatch.setattr(dbn.shutil, "which", lambda n: None)
    assert dbn.enabled() is False


def test_re_arming_replaces_the_pending_timer():
    """`systemd-run --unit` refuses a name that already exists, and re-arming
    is the common case: every turn but the last one."""
    cmd = dbn.command(600)
    assert cmd.index("systemctl --user stop") < cmd.index("systemd-run")
    assert "--unit=agent-media-feed-debounce" in cmd
    assert "--on-active=600" in cmd


def test_it_fires_the_same_unit_the_poll_runs():
    """One place for the publish rules, the environment, and the chain into
    Audiobookshelf."""
    assert "agent-media-feed-publish.service" in dbn.command(60)


def test_the_deadline_is_the_publisher_s_own_quiet_window(monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_QUIET_MIN", "10")
    assert dbn.quiet_s() == 600
    monkeypatch.setenv("MEDIA_FEED_QUIET_MIN", "nonsense")
    assert dbn.quiet_s() == dbn.DEFAULT_QUIET_MIN * 60
    monkeypatch.setenv("MEDIA_FEED_QUIET_MIN", "0")
    assert dbn.quiet_s() == 60      # a floor, not an instant republish loop


def test_arming_is_detached_and_does_not_wait(monkeypatch):
    """It runs on the speech path: an unpublished episode is a wait, a blocked
    reply is silence in the room."""
    seen = {}

    class _Popen:
        def __init__(self, argv, **kw):
            seen["argv"], seen["kw"] = argv, kw

        def wait(self, *a, **k):    # pragma: no cover - must never be called
            raise AssertionError("armed synchronously")

    monkeypatch.setattr(dbn.subprocess, "Popen", _Popen)
    assert dbn.arm(600) is True
    assert seen["argv"][:2] == ["sh", "-c"]
    assert seen["kw"]["start_new_session"] is True
    assert seen["kw"]["stdout"] is subprocess.DEVNULL


def test_a_failure_to_arm_is_never_the_caller_s_problem(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no fork for you")

    monkeypatch.setattr(dbn.subprocess, "Popen", _boom)
    assert dbn.arm(600) is False


def test_the_delay_is_a_turn_landing_not_a_conversation_ending(monkeypatch):
    """What the timer triggers appends now, so there is nothing to wait for
    except a burst of turns arriving together."""
    monkeypatch.delenv("MEDIA_FEED_DEBOUNCE_S", raising=False)
    # Hermetic about both, because they are two different clocks now and a
    # test that leaves one set would make this one read the other's value.
    monkeypatch.delenv("MEDIA_FEED_QUIET_MIN", raising=False)
    assert dbn.debounce_s() == dbn.DEFAULT_DEBOUNCE_S
    monkeypatch.setenv("MEDIA_FEED_DEBOUNCE_S", "5")
    assert dbn.debounce_s() == 15        # a floor: one scan per turn, at most
    monkeypatch.setenv("MEDIA_FEED_DEBOUNCE_S", "300")
    assert dbn.debounce_s() == 300
    # And it is no longer the hour that "finished" means, which publish-quiet
    # still follows when a conversation is archived as one file by hand.
    assert dbn.quiet_s() == dbn.DEFAULT_QUIET_MIN * 60


def test_arming_with_no_argument_uses_the_debounce(monkeypatch):
    seen = {}
    monkeypatch.setattr(dbn, "enabled", lambda: True)
    monkeypatch.setattr(dbn.subprocess, "Popen",
                        lambda argv, **kw: seen.update(cmd=argv[-1]))
    monkeypatch.setenv("MEDIA_FEED_DEBOUNCE_S", "90")
    dbn.arm()
    assert "--on-active=90" in seen["cmd"]
