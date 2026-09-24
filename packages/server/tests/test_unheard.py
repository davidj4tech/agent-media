"""A held reply's spoken block says it is unheard (§6.2.2), until it plays."""

from agent_media_server.transcript import _spoken


def test_unheard_rides_on_spoken():
    assert _spoken({"id": 7, "key": "k", "at": 1.0, "unheard": True})["unheard"] is True
    assert "unheard" not in _spoken({"id": 7, "key": "k", "at": 1.0})


def test_a_line_being_heard_is_not_unheard():
    out = _spoken({"id": 7, "key": "k", "at": 1.0, "unheard": True, "live": True})
    assert "unheard" not in out and "live" in out
