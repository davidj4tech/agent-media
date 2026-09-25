"""`spoken.resume` (§6.2.2): an interrupted reply says where it stopped, so
the app's ▶ resumes there and marks what was not heard (25 Sep 2026)."""

from agent_media_server import transcript


def test_an_interrupted_line_says_where():
    r = {"sentence": 3, "at_s": 12.5, "dur_s": 40.0}
    out = transcript._spoken({"id": 9, "key": "k", "at": 1.0, "resume": r})
    assert out["resume"] == r


def test_a_live_line_has_no_resume():
    out = transcript._spoken({"id": 9, "key": "k", "at": 1.0, "live": True,
                              "resume": {"sentence": 1, "at_s": 1, "dur_s": 2}})
    assert "resume" not in out


def test_a_heard_line_has_none():
    assert "resume" not in transcript._spoken({"id": 9, "key": "k", "at": 1.0})
