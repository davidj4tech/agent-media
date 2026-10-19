"""A person's own notification setting turning the intake's decision
(contact_notify.py). Nothing is played or written."""

import pytest

from agent_media_core import contact_notify as cn


@pytest.fixture(autouse=True)
def no_resolver():
    cn.set_resolver(None)
    yield
    cn.set_resolver(None)


@pytest.mark.parametrize("decision,how,want", [
    ("play", "default", "play"), ("toast", "default", "toast"), ("hold", "default", "hold"),
    ("play", "quiet", "hold"), ("toast", "quiet", "hold"), ("hold", "quiet", "hold"),
    ("play", "priority", "play"), ("toast", "priority", "toast"), ("hold", "priority", "toast"),
])
def test_adjust(decision, how, want):
    assert cn.adjust(decision, how) == want


def test_no_resolver_is_default():
    assert cn.mode("thread-1", "chats") == "default"


def test_the_resolver_is_asked_and_its_mistakes_are_default():
    cn.set_resolver(lambda key, ch: {"t1": "quiet", "t2": "loud"}.get(key))
    assert cn.mode("t1", "chats") == "quiet"
    assert cn.mode("t2", "chats") == "default"          # not a mode
    assert cn.mode("t3", "chats") == "default"          # no person

    def broken(key, ch):
        raise RuntimeError("store gone")
    cn.set_resolver(broken)
    assert cn.mode("t1", "chats") == "default"
