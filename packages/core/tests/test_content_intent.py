"""Tests for queue-time content-type intent.

A YouTube/HTTP URL is indistinguishable from music by URI alone, so a
spoken-word stream would be ducked rather than paused. music_play records
the caller's intent in the state store; the interruption coordinator reads
it back so longform pauses-and-resumes. The intent must outlive the
per-clip now_playing wipe in Coordinator.after_speech.
"""

from agent_media_core.route.coordinator import Coordinator
from agent_media_core.route.policy import coerce_content_type, detect_content_type
from agent_media_core.state import StateStore
from agent_media_core.types import ContentType


YT = "yt:https://www.youtube.com/watch?v=abc123"


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def test_coerce_accepts_labels_and_rejects_junk():
    assert coerce_content_type("audiobook") is ContentType.AUDIOBOOK
    assert coerce_content_type("DJ_SET") is ContentType.DJ_SET
    assert coerce_content_type("dj-set") is ContentType.DJ_SET
    assert coerce_content_type("bogus") is None
    assert coerce_content_type("") is None
    assert coerce_content_type(None) is None


def test_bare_youtube_url_detects_as_music():
    # The behaviour this feature works around: no hint → ducked.
    assert detect_content_type(YT) is ContentType.MUSIC


def test_intent_round_trips(tmp_path):
    st = _store(tmp_path)
    assert st.get_music_intent() is None
    st.set_music_intent(YT, "audiobook")
    assert st.get_music_intent() == {"uri": YT, "content_type": "audiobook"}
    st.clear_music_intent()
    assert st.get_music_intent() is None


def test_coordinator_prefers_intent_over_uri(tmp_path):
    st = _store(tmp_path)
    co = Coordinator(state=st)
    st.set_music_intent(YT, "audiobook")
    assert co._content_type_for(YT) is ContentType.AUDIOBOOK


def test_coordinator_falls_back_to_detection_without_intent(tmp_path):
    st = _store(tmp_path)
    co = Coordinator(state=st)
    assert co._content_type_for(YT) is ContentType.MUSIC


def test_intent_survives_now_playing_wipe(tmp_path):
    # after_speech clears the music now_playing row after every clip; the
    # intent lives in `meta` so the second interruption still pauses.
    st = _store(tmp_path)
    co = Coordinator(state=st)
    st.set_music_intent(YT, "audiobook")
    st.set_now_playing(sink="music", uri=YT, started_at=0.0,
                       content_type="audiobook")
    st.clear_now_playing("music")  # mimics Coordinator.after_speech
    assert co._content_type_for(YT) is ContentType.AUDIOBOOK
