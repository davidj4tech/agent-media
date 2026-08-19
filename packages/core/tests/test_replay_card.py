"""A replayed clip must say what it is, not what the last live reply was.

Every display that shows the spoken words — the phone's card, the shade's,
`share_control._speech` — reads two mpv properties the coordinator writes as it
speaks. Replay pushed the audio and left them alone, so tapping a clip in the
history on the phone changed what you heard and nothing on screen: the previous
reply's words, under the previous reply's conversation, for the whole clip.
"""

import pytest

from agent_media_core import cli


class _FakeSink:
    def prefetch(self, paths, target=None):
        return True

    def play(self, uri, target=None, **kw):
        pass

    def play_playlist(self, uris, target=None, **kw):
        pass


@pytest.fixture
def labels(monkeypatch):
    """Replay with the player, the visual and the store stubbed out."""
    said = {}
    monkeypatch.setattr(cli, "SinkSpeech", lambda: _FakeSink())
    monkeypatch.setattr(cli, "_replay_visual", lambda ex: None)
    monkeypatch.setattr(cli.ipc, "set_property", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_sock", lambda: "tcp://phone.example:6602")
    monkeypatch.setattr(cli.StateStore, "set_now_playing",
                        lambda self, sink, **kw: None)
    monkeypatch.setattr(cli._speech_sink, "set_media_title",
                        lambda title, target=None: said.__setitem__("title", title))
    monkeypatch.setattr(cli._speech_sink, "set_reply_text",
                        lambda text, target=None: said.__setitem__("text", text))
    return said


def _row(**ex):
    return {"uri": "remote-say:phone", "text": "The words of the old reply.",
            "extras": {"clip_uris": ["remote-1.mp3"], "clips_remote": True, **ex}}


def test_the_replayed_words_reach_the_card(labels):
    cli._replay_row(_row(source_window="a conversation"))
    assert labels["text"] == "The words of the old reply."
    assert labels["title"] == "a conversation"


def test_a_multi_clip_turn_labels_itself_too(labels):
    cli._replay_row(_row(source_window="a conversation",
                         clip_uris=["remote-1.mp3", "remote-2.mp3"]))
    assert labels["text"] == "The words of the old reply."


def test_a_row_with_no_conversation_does_not_name_one(labels):
    """An empty force-media-title shows the clip's filename, which is worse
    than the last conversation's name — so the write is a no-op down in the
    sink, and replay must not try to route around it."""
    cli._replay_row(_row())
    assert labels["title"] == ""
    assert labels["text"] == "The words of the old reply."
