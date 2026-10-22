"""A reply worth interrupting for says so in its claim (sasonica roadmap item 7).

A phone paired with several servers plays one server's speech at a time; the
server answering the phone's frames (speech_frames.py) lets a claim marked
`interrupt` take the player from another server. Here: the claim carries the
mark only when asked, and a sink whose claim takes no such keyword (an older
one, a test's) is asked as before. No socket, no player.
"""

from __future__ import annotations

from agent_media_core.intake import submit
from agent_media_core.sinks import speech


def test_the_claim_is_marked_only_when_interrupting():
    plain = speech._claim_of("host:1", 30)
    assert set(plain) == {"owner", "deadline"} and plain["owner"] == "host:1"
    marked = speech._claim_of("host:1", 30, interrupt=True)
    assert marked["interrupt"] is True and marked["owner"] == "host:1"


def test_a_claim_that_takes_no_interrupt_is_asked_as_before():
    seen = []

    def old(target):
        seen.append(("old", target))
        return True

    def new(target, interrupt=False):
        seen.append(("new", target, interrupt))
        return True

    assert submit._with_interrupt(old, True, "t") is True
    assert submit._with_interrupt(new, True, "t") is True
    assert submit._with_interrupt(new, False, "t") is True
    assert seen == [("old", "t"), ("new", "t", True), ("new", "t", False)]
