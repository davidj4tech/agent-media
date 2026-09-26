"""The heard note: where the voice was when the listener answered (intake.heard).

No state and no audio — `_live_turn` is replaced by the row it would read.
"""

import io
import json

from agent_media_core import book_tracks
from agent_media_core.intake import heard as H

S = ["First point.", "Second point.", "Third point.", "Fourth point."]


def _turn(**kw):
    t = {"sentences": S, "offsets": [0.0, 3.0, 6.0, 9.0],
         "elapsed": 4.0, "delay": 0.0, "sentence": 0}
    t.update(kw)
    return t


def test_position_follows_the_timeline_not_the_written_index():
    assert H.position(_turn(elapsed=4.0, sentence=0)) == 1
    assert H.position(_turn(elapsed=0.5)) == 0
    assert H.position(_turn(elapsed=100.0)) == 3


def test_position_takes_off_the_playout_delay():
    assert H.position(_turn(elapsed=6.5, delay=1.0)) == 1


def test_position_without_a_timeline_uses_the_index():
    assert H.position(_turn(offsets=[], sentence=2)) == 2
    assert H.position(_turn(offsets=[], sentence=None)) is None


def test_the_note_names_what_was_spoken_and_what_was_not():
    text = H.note(_turn(elapsed=4.0))
    assert "sentence 2 of 4" in text
    assert "“Second point.”" in text
    assert "remaining 2 sentences, from “Third point.”" in text


def test_no_note_once_the_last_sentence_is_under_way():
    assert H.note(_turn(elapsed=9.5)) == ""


def test_no_note_without_a_turn_or_for_the_listeners_own_words():
    assert H.note(None) == ""
    assert H.note(_turn(listener=True)) == ""


def test_a_paused_reply_says_so():
    assert "(paused there)" in H.note(_turn(paused=True))


def test_long_sentences_are_quoted_short():
    long = "word " * 40
    text = H.note(_turn(sentences=[long, long, long, long]))
    assert "…" in text and long.strip() not in text


def test_settings_commands_and_harness_notices_get_no_note(monkeypatch):
    monkeypatch.setattr(book_tracks, "_live_turn", lambda s: _turn())
    assert H.note_for({"session_id": "s", "prompt": "/model sonnet"}) == ""
    assert H.note_for({"session_id": "s", "prompt":
                       "<system-reminder>x</system-reminder>"}) == ""
    assert H.note_for({"session_id": "", "prompt": "yes"}) == ""
    assert H.note_for({"session_id": "s", "prompt": "/code-review"})
    assert H.note_for({"session_id": "s", "prompt": "yes"})


def test_main_prints_the_hooks_context(monkeypatch, capsys):
    monkeypatch.setattr(book_tracks, "_live_turn", lambda s: _turn())
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"session_id": "s", "prompt": "yes"})))
    assert H.main() == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    assert "sentence 2 of 4" in out["additionalContext"]


def test_main_is_silent_when_off_or_broken(monkeypatch, capsys):
    monkeypatch.setattr(book_tracks, "_live_turn", lambda s: _turn())
    monkeypatch.setenv("MEDIA_HEARD_NOTE", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"session_id": "s", "prompt": "yes"})))
    assert H.main() == 0
    monkeypatch.delenv("MEDIA_HEARD_NOTE")
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert H.main() == 0
    assert capsys.readouterr().out == ""
