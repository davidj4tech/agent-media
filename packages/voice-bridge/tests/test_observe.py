"""Failure reporting must never cost the human their words.

observe exists because a silent failure here is indistinguishable from success:
HA gets its 200 and says something reassuring either way. So the reporting path
is allowed to do nothing, but it is never allowed to raise.
"""

import pytest

from tmux_voice_bridge import observe


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Never write test failures into the real error table."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MEDIA_NOTIFY_DISABLED", "1")


def test_report_is_a_no_op_without_core(monkeypatch, capsys):
    """The package installs standalone; core may simply not be there."""
    monkeypatch.setattr(observe, "_core", lambda: None)
    observe.report("nothing to see", notify=False, target="local session x")
    assert "nothing to see" in capsys.readouterr().err


def test_report_survives_a_broken_state_store(monkeypatch, capsys):
    """A busted observability backend must not break injection reporting."""
    monkeypatch.setattr(observe, "_core", lambda: object())

    import agent_media_core.state as state_mod

    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("state.db is on fire")

    monkeypatch.setattr(state_mod, "StateStore", Boom)
    observe.report("still reported to stderr", notify=False)
    assert "still reported to stderr" in capsys.readouterr().err


def test_report_survives_a_broken_notifier(monkeypatch):
    import agent_media_core._notify as notify_mod

    def boom(**kw):
        raise RuntimeError("no notifier here")

    monkeypatch.setattr(notify_mod, "notify", boom)
    observe.report("notify blew up", notify=True)   # must not raise


def test_report_writes_to_the_error_table():
    """The whole point: it lands somewhere `media errors` can read."""
    from agent_media_core.state import StateStore

    observe.report("injection failed — spoken text was not delivered",
                   notify=False, target="local session ghost", chars=23)

    rows = StateStore().recent_errors(component="voice-bridge", limit=5)
    assert len(rows) == 1
    assert "not delivered" in rows[0]["message"]
    assert rows[0]["extras"]["target"] == "local session ghost"
    assert rows[0]["extras"]["chars"] == 23


@pytest.mark.parametrize("notify", [True, False])
def test_report_never_raises(notify):
    observe.report("smoke", notify=notify, weird=object())
