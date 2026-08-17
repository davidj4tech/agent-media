"""A clip spoken into a conversation belongs to it, id or no id.

The Stop hook is handed the Claude session id and records it. `media say` is
not — there is none in the environment — so the lead-in prose before a question,
and anything else an agent speaks from its own shell, lands in history knowing
only its pane. Two surfaces read that field and both got it wrong in their own
way: a scoped traversal skipped those clips inside the very conversation that
said them, and the phone's list drew the conversation twice, same window name,
once for the replies and once for the prose.
"""

import pytest

from agent_media_core import cli


def _row(rid, at, text, pane, session=None, kind=None):
    ex = {"source_pane": pane, "clip_uris": [f"/clips/{rid}.wav"]}
    if session:
        ex["source_session"] = session
    if kind:
        ex["kind"] = kind
    return {"id": rid, "sink": "speech", "uri": f"/clips/{rid}.wav",
            "started_at": at, "target": "phone", "text": text, "extras": ex}


@pytest.fixture
def store(monkeypatch):
    """One pane, one conversation, and an aside that never named it."""
    rows = [
        _row(4, 400.0, "the lead-in before a question", "%179"),
        _row(3, 300.0, "what would you like to clarify?", "%179", "aaa"),
        _row(2, 200.0, "an answer", "%179", "aaa"),
        _row(1, 100.0, "moon enters Libra", "%178"),          # cron: no session
    ]

    class FakeStore:
        def recent_history(self, *, sink=None, limit=20):
            return [dict(r, extras=dict(r["extras"])) for r in rows]

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)
    return rows


def test_an_aside_joins_the_conversation_its_pane_was_holding(store):
    got = {r["id"]: (r.get("extras") or {}).get("source_session")
           for r in cli._speech_history(10)}
    assert got[4] == "aaa"          # the lead-in, which named nothing
    assert got[3] == "aaa"


def test_it_says_that_it_worked_it_out(store):
    # "We know where this belongs" and "it said so itself" are different
    # claims, and only one of them can be wrong.
    rows = {r["id"]: (r.get("extras") or {}) for r in cli._speech_history(10)}
    assert rows[4].get("session_adopted") is True
    assert "session_adopted" not in rows[3]


def test_a_scoped_traversal_stops_skipping_them(store):
    # The one that mattered: `<` inside that conversation could not reach the
    # prose it had just spoken.
    texts = [r["text"] for r in cli._speech_history(10, session="aaa")]
    assert texts[0] == "the lead-in before a question"
    assert len(texts) == 3


def test_a_pane_that_never_named_one_keeps_none(store):
    # The reminders cron speaks belong to no conversation, and inventing one
    # for them would be this list making something up.
    got = {r["id"]: (r.get("extras") or {}).get("source_session")
           for r in cli._speech_history(10)}
    assert got[1] is None


def test_the_nearest_conversation_wins_not_the_newest(monkeypatch):
    # A pane that held one conversation this morning and another this
    # afternoon must not hand every one of the morning's asides to the
    # afternoon's.
    rows = [
        _row(5, 500.0, "afternoon reply", "%1", "pm"),
        _row(4, 460.0, "afternoon aside", "%1"),
        _row(3, 300.0, "morning aside", "%1"),
        _row(2, 260.0, "morning reply", "%1", "am"),
    ]

    class FakeStore:
        def recent_history(self, *, sink=None, limit=20):
            return [dict(r, extras=dict(r["extras"])) for r in rows]

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)
    got = {r["id"]: (r.get("extras") or {}).get("source_session")
           for r in cli._speech_history(10)}
    assert got[4] == "pm" and got[3] == "am"


def test_an_alert_can_be_the_evidence_even_though_it_is_dropped(monkeypatch):
    # "Claude is waiting" clips are filtered out of every list, but they carry
    # the session id — so the adoption runs before the filtering, not after.
    rows = [
        _row(2, 200.0, "the lead-in before a question", "%179"),
        _row(1, 100.0, "a question is waiting", "%179", "aaa", kind="notif"),
    ]

    class FakeStore:
        def recent_history(self, *, sink=None, limit=20):
            return [dict(r, extras=dict(r["extras"])) for r in rows]

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)
    got = cli._speech_history(10, session="aaa")
    assert [r["text"] for r in got] == ["the lead-in before a question"]


def test_the_stores_own_rows_are_left_alone(store):
    cli._speech_history(10)
    assert "source_session" not in store[0]["extras"]
