"""The Driver seam (driver/): which driver owns a thread, and the pane
driver's interrupt (server-contract.md §12).

With `MEDIA_HEADLESS` unset every lookup answers the pane driver and nothing
asks sessiond — the flag off is no behaviour change. The pane driver's other
methods are today's code, pinned by test_reply / test_contract / test_approval
through the routes; only `interrupt` is new here.
"""

from __future__ import annotations

import pytest

from agent_media_server import driver, panes, sessions

SID = "6c73498c-02c1-4846-8350-a82006973571"


@pytest.fixture(autouse=True)
def _fresh():
    driver._reset_for_tests()
    yield
    driver._reset_for_tests()


def test_flag_off_is_always_the_pane_driver(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the headless driver asked with the flag off")

    monkeypatch.setattr(driver, "headless_driver", boom)
    assert driver.headless_enabled() is False
    assert driver.for_session(SID).kind == "pane"
    assert driver.for_new("claude").kind == "pane"
    assert driver.owned_headless(SID) is False
    assert driver.headless_state(SID) is None


# --- the pane driver's interrupt ---------------------------------------------------

@pytest.fixture()
def keys(monkeypatch):
    pressed: list = []
    monkeypatch.setattr(panes, "_tmux", lambda argv: pressed.append(argv) or "")
    monkeypatch.setattr(panes, "_run", lambda argv: pressed.append(argv) or "")
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "claude")
    from agent_media_server.driver import pane as pane_mod

    monkeypatch.setattr(pane_mod, "INTERRUPT_WATCH_S", 0.7)
    return pressed


def _states(monkeypatch, *seq):
    it = iter(seq)
    last = [seq[-1]]

    def activity_of(session, pane, **kw):
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return {"state": last[0]}

    monkeypatch.setattr(sessions, "activity_of", activity_of)


def test_pane_interrupt_presses_escape_while_working(monkeypatch, keys):
    _states(monkeypatch, "working", "waiting")
    ok, d = driver.pane_driver().interrupt(SID)
    assert ok and d["interrupted"] is True and d["state"] == "waiting"
    assert keys == [["send-keys", "-t", "%42", "Escape"]]


def test_pane_interrupt_never_presses_on_a_dialog(monkeypatch, keys):
    _states(monkeypatch, "approval")
    ok, d = driver.pane_driver().interrupt(SID)
    assert ok and d["interrupted"] is False and d["why"] == "waiting on a question"
    assert keys == []


def test_pane_interrupt_on_an_idle_pane_does_nothing(monkeypatch, keys):
    _states(monkeypatch, "waiting")
    ok, d = driver.pane_driver().interrupt(SID)
    assert ok and d == {"interrupted": False, "why": "not working", "state": "waiting",
                        "pane": "%42"}
    assert keys == []


def test_pane_interrupt_still_working_is_504(monkeypatch, keys):
    _states(monkeypatch, "working")
    ok, d = driver.pane_driver().interrupt(SID)
    assert not ok and d["status"] == 504 and d["error"] == "still working after Escape"


def test_pane_interrupt_not_supported_for_pi(monkeypatch, keys):
    _states(monkeypatch, "working")
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "pi")
    ok, d = driver.pane_driver().interrupt(SID)
    assert ok and d["interrupted"] is False and d["why"] == "not supported for pi"
    assert keys == []


def test_pane_interrupt_not_live(monkeypatch, keys):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    ok, d = driver.pane_driver().interrupt(SID)
    assert ok and d["interrupted"] is False and d["why"] == "not live"
